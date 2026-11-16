"""Autonomic migraine forecast from Garmin data — pure logic, no Home Assistant imports.

A personal change detector, not a migraine detector: each morning it measures how far last
night's sleep / HRV / resting heart rate and yesterday's stress and Body Battery are from
this person's own normal days, and — once enough headaches are logged — tests whether
"far from normal" actually precedes their headaches.

Pipeline
  1. ``build_day_record``  raw Garmin payloads for one date  -> flat metrics
  2. ``day_features``      night of D + daytime of D-1       -> feature vector for morning D
  3. ``label_day``         headache log                      -> pre_headache / during / after /
                                                                normal / near_headache / pending
  4. ``compute_forecast``  rolling baseline of earlier normal days (median + MAD), robust
                           z-scores, deviation index, percentile vs earlier normal days,
                           and the "does it work for you" evaluation.

Everything is causal: a day is only compared with days whose "normal" label was already
knowable that morning. Research signal only — never a diagnosis.
"""
from __future__ import annotations

from datetime import datetime, timezone
from math import sqrt
from typing import Any, Callable

HOUR = 3600.0

HORIZON_HOURS = 24
NORMAL_CLEAR_BEFORE_HOURS = 24
NORMAL_CLEAR_AFTER_HOURS = 48
# A headache with no recorded end counts as ongoing for at most this long.
MAX_OPEN_HEADACHE_HOURS = 72

BASELINE_WINDOW = 30
MIN_BASELINE = 7
MIN_FEATURES = 5
Z_CLIP = 6.0
MIN_PERCENTILE_REFERENCE = 5
ALERT_PERCENTILE = 90
MIN_PRE_HEADACHE = 5
MIN_NORMAL = 15

STATUS_NO_DATA = "no_data"
STATUS_COLLECTING = "collecting_baseline"
STATUS_USUAL = "usual"
STATUS_ELEVATED = "elevated"

# (key, label, direction). Direction = the change the wearable literature reports before
# an attack (+1 up, -1 down, 0 none stated). It only feeds the directional index.
FEATURES: list[tuple[str, str, int]] = [
    ("hrv_night", "Overnight HRV", -1),
    ("hrv_5min_high", "HRV 5-min high", 0),
    ("hrv_change_3d", "HRV change vs previous 2 nights", -1),
    ("resting_hr", "Resting heart rate", 1),
    ("sleep_hours", "Sleep duration", -1),
    ("sleep_score", "Sleep score", -1),
    ("deep_pct", "Deep sleep share", 0),
    ("rem_pct", "REM sleep share", 0),
    ("awake_min", "Awake time during sleep", 1),
    ("sleep_stress", "Stress during sleep", 1),
    ("sleep_respiration", "Breathing rate during sleep", 1),
    ("sleep_midpoint", "Sleep timing (midpoint)", 0),
    ("restless", "Restless moments", 1),
    ("bb_at_wake", "Body Battery at wake", -1),
    ("spo2", "Blood oxygen during sleep", 0),
    ("prev_stress_avg", "Yesterday's average stress", 1),
    ("prev_bb_low", "Yesterday's lowest Body Battery", -1),
    ("prev_steps", "Yesterday's steps", 0),
    ("prev_intensity_min", "Yesterday's intensity minutes", 0),
]
FEATURE_LABELS = {key: label for key, label, _ in FEATURES}

# Metrics measured during the night of a date vs. over the whole calendar day.
NIGHT_KEYS = (
    "hrv_night", "hrv_5min_high", "resting_hr", "sleep_hours", "sleep_score", "deep_pct",
    "rem_pct", "awake_min", "sleep_stress", "sleep_respiration", "sleep_midpoint",
    "restless", "bb_at_wake", "spo2",
)


# ── Small numeric helpers ────────────────────────────────────────────────────

def _num(value: Any) -> float | None:
    """Return ``value`` as a finite float, or None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number == number and abs(number) != float("inf") else None


def _first(source: Any, *keys: str) -> float | None:
    if not isinstance(source, dict):
        return None
    for key in keys:
        number = _num(source.get(key))
        if number is not None:
            return number
    return None


def median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def robust_spread(values: list[float], center: float) -> float:
    """Scaled MAD; falls back to the standard deviation when the MAD collapses to zero
    (common for whole-number metrics such as resting heart rate)."""
    mad = 1.4826 * median([abs(v - center) for v in values])
    if mad > 0:
        return mad
    mean = sum(values) / len(values)
    return sqrt(sum((v - mean) ** 2 for v in values) / len(values))


def auc(positives: list[float], negatives: list[float]) -> float:
    """Chance that a random pre-headache value exceeds a random normal-day value."""
    wins = 0.0
    for p in positives:
        for n in negatives:
            wins += 1.0 if p > n else 0.5 if p == n else 0.0
    return wins / (len(positives) * len(negatives))


def _rms(values: list[float]) -> float:
    return sqrt(sum(v * v for v in values) / len(values))


# ── 1. Garmin payloads -> flat metrics ───────────────────────────────────────

def migraine_log_status(lifestyle: Any) -> str | None:
    """Read the "Migraines" entry of Garmin's Lifestyle Logging for one date.

    Returns "YES", "NO", or None when the question was not answered that day (or the
    behaviour is not tracked at all). Only "YES" means a migraine day; "NO" and
    unanswered are both treated as no migraine.
    """
    logs = lifestyle.get("dailyLogsReport") if isinstance(lifestyle, dict) else None
    if not isinstance(logs, list):
        return None
    for entry in logs:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").lower()
        if "migraine" in name or "headache" in name:
            status = str(entry.get("logStatus") or "").upper()
            return status if status in ("YES", "NO") else None
    return None


def build_day_record(sleep: Any, hrv: Any, stats: Any, lifestyle: Any = None) -> dict[str, Any]:
    """Flatten the Garmin Connect payloads of one calendar date.

    ``sleep`` = get_sleep_data, ``hrv`` = get_hrv_data, ``stats`` = get_stats. Any of them
    may be None or lack fields (watch not worn, metric unsupported): missing values stay
    None and are simply skipped by the score.
    """
    record: dict[str, Any] = {key: None for key in NIGHT_KEYS}
    record.update({"day_stress_avg": None, "day_bb_low": None, "day_steps": None,
                   "day_intensity_min": None, "sleep_end_ts": None,
                   "migraine_log": migraine_log_status(lifestyle)})

    dto = sleep.get("dailySleepDTO") if isinstance(sleep, dict) else None
    if isinstance(dto, dict):
        seconds = _first(dto, "sleepTimeSeconds")
        if seconds and seconds > 0:
            record["sleep_hours"] = seconds / 3600
            deep = _first(dto, "deepSleepSeconds")
            rem = _first(dto, "remSleepSeconds")
            record["deep_pct"] = deep / seconds * 100 if deep is not None else None
            record["rem_pct"] = rem / seconds * 100 if rem is not None else None
        awake = _first(dto, "awakeSleepSeconds")
        record["awake_min"] = awake / 60 if awake is not None else None
        record["sleep_stress"] = _first(dto, "avgSleepStress")
        record["sleep_respiration"] = _first(dto, "averageRespirationValue")
        record["spo2"] = _first(dto, "averageSpO2Value")
        scores = dto.get("sleepScores")
        if isinstance(scores, dict):
            record["sleep_score"] = _first(scores.get("overall"), "value")

        end_gmt = _first(dto, "sleepEndTimestampGMT")
        if end_gmt:
            record["sleep_end_ts"] = end_gmt / 1000
        start_local = _first(dto, "sleepStartTimestampLocal")
        end_local = _first(dto, "sleepEndTimestampLocal")
        if start_local and end_local:
            # "Local" timestamps are wall-clock time encoded as if it were UTC.
            mid = datetime.fromtimestamp((start_local + end_local) / 2000, tz=timezone.utc)
            hour = mid.hour + mid.minute / 60
            # Hours relative to midnight in (-12, 12], so 23:30 and 00:30 are neighbours.
            record["sleep_midpoint"] = hour - 24 if hour > 12 else hour

    if isinstance(sleep, dict):
        record["resting_hr"] = _first(sleep, "restingHeartRate")
        record["hrv_night"] = _first(sleep, "avgOvernightHrv")
        record["restless"] = _first(sleep, "restlessMomentsCount")

    summary = hrv.get("hrvSummary") if isinstance(hrv, dict) else None
    if isinstance(summary, dict):
        record["hrv_night"] = _first(summary, "lastNightAvg") or record["hrv_night"]
        record["hrv_5min_high"] = _first(summary, "lastNight5MinHigh")

    if isinstance(stats, dict):
        if record["resting_hr"] is None:
            record["resting_hr"] = _first(stats, "restingHeartRate")
        record["bb_at_wake"] = _first(stats, "bodyBatteryAtWakeTime")
        record["day_stress_avg"] = _first(stats, "averageStressLevel")
        record["day_bb_low"] = _first(stats, "bodyBatteryLowestValue")
        record["day_steps"] = _first(stats, "totalSteps")
        moderate = _first(stats, "moderateIntensityMinutes")
        vigorous = _first(stats, "vigorousIntensityMinutes")
        if moderate is not None or vigorous is not None:
            record["day_intensity_min"] = (moderate or 0) + 2 * (vigorous or 0)
        # Garmin reports -1 / -2 when stress could not be measured.
        if record["day_stress_avg"] is not None and record["day_stress_avg"] < 0:
            record["day_stress_avg"] = None
    return record


def record_has_night(record: dict[str, Any] | None) -> bool:
    """True when the record carries last night's sleep — i.e. the watch has synced."""
    return bool(record) and record.get("sleep_hours") is not None


# ── 2. Feature vector for the morning of a date ──────────────────────────────

def day_features(
    night: dict[str, Any], previous_day: dict[str, Any] | None, earlier_hrv: list[float],
) -> dict[str, float | None]:
    """Night of D + daytime of D-1. ``earlier_hrv`` = overnight HRV of D-1 and D-2."""
    features: dict[str, float | None] = {key: night.get(key) for key in NIGHT_KEYS}
    hrv = night.get("hrv_night")
    features["hrv_change_3d"] = (
        hrv - sum(earlier_hrv) / len(earlier_hrv) if hrv is not None and len(earlier_hrv) == 2 else None
    )
    previous_day = previous_day or {}
    features["prev_stress_avg"] = previous_day.get("day_stress_avg")
    features["prev_bb_low"] = previous_day.get("day_bb_low")
    features["prev_steps"] = previous_day.get("day_steps")
    features["prev_intensity_min"] = previous_day.get("day_intensity_min")
    return features


# ── 3. Labels from the headache log ──────────────────────────────────────────

def _headache_end(headache: dict[str, Any], now: float) -> float:
    end = headache.get("end")
    return end if end is not None else min(now, headache["onset"] + MAX_OPEN_HEADACHE_HOURS * HOUR)


def label_day(anchor: float, headaches: list[dict[str, Any]], now: float) -> str:
    """Label the morning ``anchor`` by what happened afterwards."""
    pre = after = near = False
    for headache in headaches:
        onset = headache["onset"]
        end = _headache_end(headache, now)
        if onset <= anchor <= end:
            return "during"
        if anchor < onset <= anchor + HORIZON_HOURS * HOUR:
            pre = True
        elif anchor < onset <= anchor + NORMAL_CLEAR_AFTER_HOURS * HOUR:
            near = True
        if anchor - NORMAL_CLEAR_BEFORE_HOURS * HOUR <= end < anchor:
            after = True
    if pre:
        return "pre_headache"
    if after:
        return "after"
    if near:
        return "near_headache"
    return "normal" if now >= anchor + NORMAL_CLEAR_AFTER_HOURS * HOUR else "pending"


def headaches_from_daily_log(
    records: dict[str, dict[str, Any]],
    day_start: Callable[[str], float],
    default_anchor: Callable[[str], float],
) -> list[dict[str, Any]]:
    """Turn Garmin's per-day "Migraines: yes" answers into headache intervals.

    The log has no clock time, so a migraine day is assumed to start in the afternoon -
    after that morning's wake-up - and last until midnight. That makes the morning of a
    migraine day the pre-headache morning. Consecutive "yes" days become one headache.
    """
    headaches: list[dict[str, Any]] = []
    previous: str | None = None
    for date in sorted(d for d, r in records.items() if r.get("migraine_log") == "YES"):
        end = day_start(date) + 24 * HOUR - 1
        if previous is not None and _previous_date(date) == previous:
            headaches[-1]["end"] = end
        else:
            start = day_start(date)
            wake = records[date].get("sleep_end_ts") or default_anchor(date)
            onset = min(max(start + 12 * HOUR, wake + HOUR), end)
            headaches.append({
                "id": f"garmin-{date}", "onset": onset, "end": end, "source": "garmin",
                "day_start": start, "intensity": None, "aura": False, "medication": None, "notes": None,
            })
        previous = date
    return headaches


def merge_headaches(
    manual: list[dict[str, Any]], from_log: list[dict[str, Any]], now: float,
) -> list[dict[str, Any]]:
    """Manual entries carry real times, so a logged Garmin day they overlap is dropped."""
    merged = list(manual)
    for headache in from_log:
        overlaps = any(
            m["onset"] <= headache["end"] and _headache_end(m, now) >= headache["day_start"]
            for m in manual
        )
        if not overlaps:
            merged.append(headache)
    return sorted(merged, key=lambda item: item["onset"])


# ── 4. Baseline, score, evaluation ───────────────────────────────────────────

def _previous_date(date: str, steps: int = 1) -> str:
    day = datetime.strptime(date, "%Y-%m-%d").toordinal() - steps
    return datetime.fromordinal(day).strftime("%Y-%m-%d")


def compute_forecast(
    records: dict[str, dict[str, Any]],
    headaches: list[dict[str, Any]],
    now: float,
    default_anchor: Callable[[str], float],
) -> dict[str, Any]:
    """Score every stored date. ``records`` = {"YYYY-MM-DD": build_day_record(...)};
    ``headaches`` = [{"onset": ts, "end": ts | None, ...}]; ``default_anchor(date)`` gives
    the morning timestamp to use when the wake-up time is unknown."""
    days: list[dict[str, Any]] = []
    for date in sorted(records):
        night = records[date]
        if not record_has_night(night):
            continue
        earlier_hrv = [
            value for value in (
                (records.get(_previous_date(date, step)) or {}).get("hrv_night") for step in (1, 2)
            ) if value is not None
        ]
        anchor = night.get("sleep_end_ts") or default_anchor(date)
        days.append({
            "date": date,
            "anchor": anchor,
            "features": day_features(night, records.get(_previous_date(date)), earlier_hrv),
            "label": label_day(anchor, headaches, now),
            "baseline_days": 0,
            "deviation_index": None,
            "directional_index": None,
            "percentile": None,
            "alert": False,
            "top_features": [],
            "z": {},
        })

    for index, day in enumerate(days):
        # Normal days whose label was already settled that morning.
        known_by = day["anchor"] - NORMAL_CLEAR_AFTER_HOURS * HOUR
        earlier = [d for d in days[:index] if d["anchor"] <= known_by]
        baseline = [d for d in earlier if d["label"] == "normal"][-BASELINE_WINDOW:]
        day["baseline_days"] = len(baseline)
        if len(baseline) < MIN_BASELINE:
            continue

        for key, _label, _direction in FEATURES:
            today = day["features"].get(key)
            if today is None:
                continue
            history = [d["features"][key] for d in baseline if d["features"].get(key) is not None]
            if len(history) < MIN_BASELINE:
                continue
            center = median(history)
            spread = robust_spread(history, center)
            if spread <= 0:
                continue  # a metric that never varied carries no information
            day["z"][key] = max(-Z_CLIP, min(Z_CLIP, (today - center) / spread))

        if len(day["z"]) < MIN_FEATURES:
            continue
        day["deviation_index"] = _rms(list(day["z"].values()))
        directional = [
            max(0.0, direction * day["z"][key])
            for key, _label, direction in FEATURES if direction and key in day["z"]
        ]
        day["directional_index"] = _rms(directional) if directional else None
        top = sorted(day["z"].items(), key=lambda item: abs(item[1]), reverse=True)[:3]
        day["top_features"] = [
            {"key": key, "label": FEATURE_LABELS[key], "z": round(z, 2)} for key, z in top
        ]

        reference = [
            d["deviation_index"] for d in earlier
            if d["label"] == "normal" and d["deviation_index"] is not None
        ][-BASELINE_WINDOW:]
        if len(reference) >= MIN_PERCENTILE_REFERENCE:
            below = sum(1 for value in reference if value < day["deviation_index"])
            day["percentile"] = below / len(reference) * 100
            day["alert"] = day["percentile"] >= ALERT_PERCENTILE

    return {
        "days": days,
        "evaluation": _evaluate(days, headaches),
        "normal_days": sum(1 for d in days if d["label"] == "normal"),
        "pre_headache_days": sum(1 for d in days if d["label"] == "pre_headache"),
    }


def _evaluate(days: list[dict[str, Any]], headaches: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Does the score separate this person's pre-headache mornings from normal ones?"""
    scored = [d for d in days if d["deviation_index"] is not None]
    pre = [d for d in scored if d["label"] == "pre_headache"]
    normal = [d for d in scored if d["label"] == "normal"]
    if len(pre) < MIN_PRE_HEADACHE or len(normal) < MIN_NORMAL:
        return None

    feature_auc = []
    for key, label, _direction in FEATURES:
        p = [d["z"][key] for d in pre if key in d["z"]]
        n = [d["z"][key] for d in normal if key in d["z"]]
        if len(p) >= MIN_PRE_HEADACHE and len(n) >= MIN_NORMAL:
            feature_auc.append({"key": key, "label": label, "auc": round(auc(p, n), 3)})
    feature_auc.sort(key=lambda item: abs(item["auc"] - 0.5), reverse=True)

    # Each day's alert threshold came only from earlier normal days, so counting alerts
    # here is already "threshold set on earlier weeks, tested on later ones".
    judged = [d for d in days if d["percentile"] is not None]
    with_day = alerted = 0
    lead_hours: list[float] = []
    for headache in headaches:
        onset = headache["onset"]
        before = [d for d in judged if onset - HORIZON_HOURS * HOUR <= d["anchor"] < onset]
        if not before:
            continue
        with_day += 1
        alerts = [d for d in before if d["alert"]]
        if alerts:
            alerted += 1
            lead_hours.append((onset - min(d["anchor"] for d in alerts)) / HOUR)

    judged_normal = [d for d in judged if d["label"] == "normal"]
    span_weeks = (judged[-1]["anchor"] - judged[0]["anchor"]) / (7 * 24 * HOUR) if len(judged) > 1 else 0
    false_per_week = (
        sum(1 for d in judged_normal if d["alert"]) / max(1.0, span_weeks)
        if judged_normal and span_weeks > 0 else None
    )
    directional_pre = [d["directional_index"] for d in pre if d["directional_index"] is not None]
    directional_normal = [d["directional_index"] for d in normal if d["directional_index"] is not None]
    return {
        "pre_headache_days": len(pre),
        "normal_days": len(normal),
        "deviation_index_auc": round(auc([d["deviation_index"] for d in pre], [d["deviation_index"] for d in normal]), 3),
        "directional_index_auc": (
            round(auc(directional_pre, directional_normal), 3) if directional_pre and directional_normal else None
        ),
        "top_features": feature_auc[:5],
        "alert_percentile": ALERT_PERCENTILE,
        "headaches_with_data": with_day,
        "headaches_alerted": alerted,
        "false_alerts_per_week": round(false_per_week, 2) if false_per_week is not None else None,
        "median_lead_hours": round(median(lead_hours), 1) if lead_hours else None,
    }


def day_status(day: dict[str, Any] | None) -> str:
    if day is None:
        return STATUS_NO_DATA
    if day["percentile"] is None:
        return STATUS_COLLECTING
    return STATUS_ELEVATED if day["alert"] else STATUS_USUAL
