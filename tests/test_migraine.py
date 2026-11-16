"""Tests for the pure migraine-forecast logic (no Home Assistant needed).

Run:  python -m pytest tests
"""
import importlib.util
import pathlib
import sys
from datetime import datetime, timedelta, timezone

_PATH = pathlib.Path(__file__).parents[1] / "custom_components" / "garmin_hydration_sync" / "migraine.py"
_spec = importlib.util.spec_from_file_location("migraine", _PATH)
m = importlib.util.module_from_spec(_spec)
sys.modules["migraine"] = m
_spec.loader.exec_module(m)

H = 3600.0
START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def anchor(date: str) -> float:
    return datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() + 8 * H


def date_of(i: int) -> str:
    return (START + timedelta(days=i)).strftime("%Y-%m-%d")


def record(i: int, bad: bool = False) -> dict:
    wobble = (i * 7) % 5
    return {
        "hrv_night": (30 if bad else 50) + wobble, "hrv_5min_high": 70 + wobble,
        "resting_hr": (62 if bad else 53) + wobble % 3, "sleep_hours": (4.5 if bad else 7.5) + wobble / 10,
        "sleep_score": (45 if bad else 80) + wobble, "deep_pct": 18 + wobble, "rem_pct": 20 + wobble,
        "awake_min": (60 if bad else 15) + wobble, "sleep_stress": (35 if bad else 15) + wobble,
        "sleep_respiration": 13 + wobble / 5, "sleep_midpoint": 3 + wobble / 4, "restless": 30 + wobble,
        "bb_at_wake": (40 if bad else 85) + wobble, "spo2": 95 + wobble / 5,
        "day_stress_avg": 28 + wobble, "day_bb_low": 30 + wobble, "day_steps": 6000 + 300 * wobble,
        "day_intensity_min": 10 * wobble, "sleep_end_ts": None,
    }


def test_build_day_record_reads_garmin_payloads():
    sleep = {
        "dailySleepDTO": {
            "sleepTimeSeconds": 27000, "deepSleepSeconds": 5400, "remSleepSeconds": 6750,
            "awakeSleepSeconds": 900, "avgSleepStress": 17.0, "averageRespirationValue": 13.0,
            "sleepEndTimestampGMT": 1_763_190_000_000,
            "sleepStartTimestampLocal": 1_763_163_000_000, "sleepEndTimestampLocal": 1_763_190_000_000,
            "sleepScores": {"overall": {"value": 81}},
        },
        "restingHeartRate": 55, "avgOvernightHrv": 47.0, "restlessMomentsCount": 33,
    }
    hrv = {"hrvSummary": {"lastNightAvg": 48, "lastNight5MinHigh": 69}}
    stats = {"averageStressLevel": 26, "bodyBatteryLowestValue": 36, "bodyBatteryAtWakeTime": 90,
             "totalSteps": 1196, "moderateIntensityMinutes": 10, "vigorousIntensityMinutes": 5}
    rec = m.build_day_record(sleep, hrv, stats)
    assert rec["sleep_hours"] == 7.5
    assert rec["deep_pct"] == 20
    assert rec["hrv_night"] == 48 and rec["hrv_5min_high"] == 69
    assert rec["resting_hr"] == 55 and rec["sleep_score"] == 81
    assert rec["day_intensity_min"] == 20 and rec["sleep_end_ts"] == 1_763_190_000
    assert -12 < rec["sleep_midpoint"] <= 12
    assert m.record_has_night(rec)


def test_build_day_record_tolerates_missing_data():
    rec = m.build_day_record(None, None, {"averageStressLevel": -1})
    assert not m.record_has_night(rec)
    assert rec["day_stress_avg"] is None


def test_labels():
    headaches = [{"onset": 100 * H, "end": 110 * H}]
    now = 1000 * H
    assert m.label_day(90 * H, headaches, now) == "pre_headache"
    assert m.label_day(105 * H, headaches, now) == "during"
    assert m.label_day(120 * H, headaches, now) == "after"
    assert m.label_day(60 * H, headaches, now) == "near_headache"
    assert m.label_day(10 * H, headaches, now) == "normal"
    assert m.label_day(990 * H, headaches, now) == "pending"
    assert m.label_day(120 * H, [{"onset": 100 * H, "end": None}], now) == "during"


def test_no_score_before_baseline_then_scores():
    records = {date_of(i): record(i) for i in range(20)}
    result = m.compute_forecast(records, [], anchor(date_of(30)), anchor)
    days = result["days"]
    assert days[0]["deviation_index"] is None
    assert days[7]["deviation_index"] is None  # only 6 normal days settled 48h earlier
    assert days[-1]["deviation_index"] is not None
    assert m.day_status(days[2]) == m.STATUS_COLLECTING
    assert m.day_status(None) == m.STATUS_NO_DATA


def test_bad_night_stands_out_and_evaluation_separates():
    records, headaches = {}, []
    for i in range(120):
        bad = i > 30 and i % 12 == 0
        records[date_of(i)] = record(i, bad)
        if bad:
            onset = anchor(date_of(i)) + 6 * H
            headaches.append({"onset": onset, "end": onset + 5 * H})
    result = m.compute_forecast(records, headaches, anchor(date_of(125)), anchor)
    pre = [d for d in result["days"] if d["label"] == "pre_headache" and d["deviation_index"] is not None]
    normal = [d for d in result["days"] if d["label"] == "normal" and d["deviation_index"] is not None]
    assert len(pre) >= 5
    assert min(d["deviation_index"] for d in pre) > max(d["deviation_index"] for d in normal)
    assert all(d["alert"] for d in pre if d["percentile"] is not None)
    assert m.day_status(pre[-1]) == m.STATUS_ELEVATED
    evaluation = result["evaluation"]
    assert evaluation is not None and evaluation["deviation_index_auc"] == 1.0
    assert evaluation["headaches_alerted"] == evaluation["headaches_with_data"] > 0
    assert evaluation["median_lead_hours"] == 6.0


def test_baseline_never_uses_later_days():
    records = {date_of(i): record(i) for i in range(40)}
    full = m.compute_forecast(records, [], anchor(date_of(60)), anchor)
    truncated = m.compute_forecast({d: r for d, r in records.items() if d <= date_of(25)}, [], anchor(date_of(60)), anchor)
    assert full["days"][25]["deviation_index"] == truncated["days"][25]["deviation_index"]


def day_start(date: str) -> float:
    return datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()


def test_migraine_log_status_reads_garmin_lifestyle_log():
    def log(status=None):
        entry = {"behaviourId": 38, "name": "Migraines", "category": "LIFE_STATUS"}
        if status:
            entry["logStatus"] = status
        return {"dailyLogsReport": [entry]}

    assert m.migraine_log_status(log("YES")) == "YES"
    assert m.migraine_log_status(log("NO")) == "NO"
    assert m.migraine_log_status(log()) is None  # not answered
    assert m.migraine_log_status(None) is None
    assert m.build_day_record(None, None, None, log("YES"))["migraine_log"] == "YES"


def test_garmin_migraine_day_labels_that_morning_pre_headache():
    records = {date_of(i): record(i) for i in range(10)}
    records[date_of(5)]["migraine_log"] = "YES"
    records[date_of(6)]["migraine_log"] = "YES"  # consecutive day: same headache
    records[date_of(8)]["migraine_log"] = "NO"
    headaches = m.headaches_from_daily_log(records, day_start, anchor)
    assert len(headaches) == 1
    assert headaches[0]["onset"] == day_start(date_of(5)) + 12 * H
    assert headaches[0]["end"] == day_start(date_of(6)) + 24 * H - 1

    result = m.compute_forecast(records, headaches, anchor(date_of(30)), anchor)
    labels = {d["date"]: d["label"] for d in result["days"]}
    assert labels[date_of(5)] == "pre_headache"
    assert labels[date_of(6)] == "during"
    assert labels[date_of(7)] == "after"
    assert labels[date_of(4)] == "near_headache"
    assert labels[date_of(2)] == "normal"


def test_manual_entry_replaces_overlapping_garmin_day():
    records = {date_of(5): {**record(5), "migraine_log": "YES"}}
    from_log = m.headaches_from_daily_log(records, day_start, anchor)
    manual = [{"id": "a", "onset": day_start(date_of(5)) + 9 * H, "end": day_start(date_of(5)) + 15 * H}]
    merged = m.merge_headaches(manual, from_log, anchor(date_of(30)))
    assert [h["id"] for h in merged] == ["a"]
    assert len(m.merge_headaches([], from_log, anchor(date_of(30)))) == 1


def test_72h_view_catches_an_unusual_night_two_days_before():
    records, headaches = {}, []
    for i in range(140):
        # The unusual night comes two mornings before each headache; the night right
        # before it is ordinary, so the single-morning score cannot see it.
        records[date_of(i)] = record(i, bad=(i > 30 and i % 14 == 0))
        if i > 30 and i % 14 == 2:
            onset = anchor(date_of(i)) + 6 * H
            headaches.append({"onset": onset, "end": onset + 5 * H})
    result = m.compute_forecast(records, headaches, anchor(date_of(150)), anchor)
    by_date = {d["date"]: d for d in result["days"]}
    morning = by_date[date_of(58)]  # headache day; the bad night was day 56
    assert morning["label"] == "pre_headache" and not morning["alert"]
    assert morning["label_72h"] == "pre_headache" and morning["alert_3d"]
    assert by_date[date_of(56)]["label_72h"] == "pre_headache"

    long_view = result["evaluation_72h"]
    assert long_view is not None
    assert long_view["attacks_alerted"] == long_view["attacks_with_data"] >= 5
    assert long_view["alerted_by_chance_pct"] < 20
    assert long_view["index_3d_auc"] > 0.9
    # The single-morning view only sees the echo of that night (HRV change), not the night.
    assert result["evaluation"]["headaches_alerted"] < long_view["attacks_alerted"]


def test_72h_label_needs_a_longer_clear_period():
    headaches = [{"onset": 100 * H, "end": 110 * H}]
    now = 1000 * H
    assert m.label_day(40 * H, headaches, now, 72) == "pre_headache"   # 60 h before
    assert m.label_day(10 * H, headaches, now, 72) == "near_headache"  # 90 h before
    assert m.label_day(10 * H, headaches, now) == "normal"             # fine on the 24 h view
