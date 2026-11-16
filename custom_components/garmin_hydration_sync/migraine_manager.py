"""Home Assistant side of the migraine forecast: Garmin download, storage, headache log.

The scoring itself lives in ``migraine.py`` (pure, testable). This class keeps a per-day
store of the flattened Garmin metrics and the headache log, refreshes it a few times a
day, and publishes the latest result to the sensors through a dispatcher signal.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_GARMIN_EMAIL,
    CONF_GARMIN_PASSWORD,
    DOMAIN,
    LOGGER,
    SIGNAL_MIGRAINE_UPDATED,
)
from .garmin_connect import GarminAuthExpiredError, async_fetch_health_days
from .migraine import (
    build_day_record,
    compute_forecast,
    day_status,
    headaches_from_daily_log,
    merge_headaches,
    record_has_night,
)

if TYPE_CHECKING:
    from . import GarminSyncCoordinator

_STORE_VERSION = 1
# First run downloads this much history so a baseline exists from day one.
_BACKFILL_DAYS = 60
_MAX_BACKFILL_DAYS = 365
_KEEP_DAYS = 400
# Dates per Garmin session; the account's token lock is held for one batch.
_BATCH_DAYS = 7
_REFRESH_INTERVAL = timedelta(hours=2)
# Recent days re-downloaded on every refresh.
_RECHECK_DAYS = 3
# Morning used when the wake-up time of a night is unknown.
_DEFAULT_ANCHOR_HOUR = 8


def _timestamp(value: datetime | None) -> float:
    """Unix time of ``value`` (now when None). A time without a zone, as typed into a
    service call, is read in Home Assistant's configured time zone."""
    if value is None:
        return dt_util.utcnow().timestamp()
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
    return value.timestamp()


def _needs_retry(record: dict[str, Any]) -> bool:
    """A day whose download failed part-way. Records saved by 1.4.0 carry no flag, so a
    night-less one is checked once more (it may have hit Garmin's rate limit)."""
    if "incomplete" in record:
        return bool(record["incomplete"])
    return not record_has_night(record)


class MigraineForecastManager:
    """Owns the stored days + headache log of one Garmin account."""

    def __init__(self, hass: HomeAssistant, coordinator: GarminSyncCoordinator) -> None:
        self.hass = hass
        self._coordinator = coordinator
        self.entry_id: str = coordinator.entry.entry_id
        self._store: Store = Store(hass, _STORE_VERSION, f"{DOMAIN}.{self.entry_id}.migraine")
        # {"YYYY-MM-DD": {...metrics..., "fetched_on": "YYYY-MM-DD"}}
        self._records: dict[str, dict[str, Any]] = {}
        # [{"id", "onset", "end", "intensity", "aura", "medication", "notes"}], unix seconds
        self.headaches: list[dict[str, Any]] = []
        self.result: dict[str, Any] | None = None
        self.last_refresh: str | None = None
        self.last_error: str | None = None
        self._lock = asyncio.Lock()
        self._unsub_interval: Any = None

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def async_load(self) -> None:
        stored = await self._store.async_load()
        if isinstance(stored, dict):
            records = stored.get("records")
            headaches = stored.get("headaches")
            if isinstance(records, dict):
                self._records = records
            if isinstance(headaches, list):
                self.headaches = headaches
        self._recompute()

    def start(self) -> None:
        self._unsub_interval = async_track_time_interval(
            self.hass, self._handle_interval, _REFRESH_INTERVAL
        )

    def stop(self) -> None:
        if self._unsub_interval:
            self._unsub_interval()
            self._unsub_interval = None

    async def _handle_interval(self, _now: datetime) -> None:
        await self.async_refresh()

    async def _save(self) -> None:
        cutoff = (dt_util.now() - timedelta(days=_KEEP_DAYS)).strftime("%Y-%m-%d")
        self._records = {d: r for d, r in self._records.items() if d >= cutoff}
        await self._store.async_save({"records": self._records, "headaches": self.headaches})

    # ── Garmin download ──────────────────────────────────────────────────────

    def _dates_to_fetch(self, backfill_days: int, force: bool = False) -> list[str]:
        today = dt_util.now().date()
        dates: list[str] = []
        for offset in range(backfill_days, -1, -1):
            date_str = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
            record = self._records.get(date_str)
            # A day is final once it was downloaded on a later date. Downloaded again: days
            # fetched while still in progress, the last few days (a migraine is often
            # logged in Garmin a day or two late), records from before the lifestyle log
            # was read, and everything when a backfill was asked for explicitly.
            if (
                force
                or record is None
                or offset <= _RECHECK_DAYS
                or record.get("fetched_on", "") <= date_str
                or "migraine_log" not in record
                or _needs_retry(record)
            ):
                dates.append(date_str)
        return dates

    @property
    def refreshing(self) -> bool:
        return self._lock.locked()

    async def async_resync(self) -> None:
        """Download the whole history window again (the Resync button)."""
        await self.async_refresh(_BACKFILL_DAYS)

    async def async_refresh(self, backfill_days: int | None = None) -> None:
        """Download missing / unfinished days and recompute the forecast."""
        async with self._lock:
            # Days from before the lifestyle log was read, or whose download failed
            # part-way (Garmin rate limit), are picked up again across the whole window.
            stale = any(
                "migraine_log" not in record or _needs_retry(record)
                for record in self._records.values()
            )
            days = backfill_days if backfill_days is not None else (
                _BACKFILL_DAYS if not self._records or stale else _RECHECK_DAYS
            )
            days = max(1, min(_MAX_BACKFILL_DAYS, int(days)))
            dates = self._dates_to_fetch(days, force=backfill_days is not None)
            cfg = self._coordinator.get_config()
            today_str = dt_util.now().strftime("%Y-%m-%d")
            self.last_error = None
            failed = 0
            try:
                for start in range(0, len(dates), _BATCH_DAYS):
                    batch = dates[start:start + _BATCH_DAYS]
                    fetched = await async_fetch_health_days(
                        self.hass, cfg[CONF_GARMIN_EMAIL], cfg[CONF_GARMIN_PASSWORD], batch
                    )
                    for date_str, payload in (fetched.get("days") or {}).items():
                        if not any(payload.get(name) for name in ("sleep", "hrv", "stats", "lifestyle")):
                            continue  # Garmin unreachable for this date: keep what we had
                        record = build_day_record(
                            payload.get("sleep"), payload.get("hrv"), payload.get("stats"),
                            payload.get("lifestyle"),
                        )
                        record["fetched_on"] = today_str
                        errors = payload.get("errors") or []
                        record["incomplete"] = bool(errors)
                        if errors:
                            failed += 1
                            previous = self._records.get(date_str)
                            # Never replace a night we already have with a failed download.
                            if record_has_night(previous) and not record_has_night(record):
                                previous["incomplete"] = True
                                continue
                        self._records[date_str] = record
                    await self._save()
            except GarminAuthExpiredError:
                self.last_error = "auth_expired"
                self._coordinator.entry.async_start_reauth(self.hass)
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                LOGGER.error("[GarminSync] Migraine forecast refresh failed: %s", exc)
            if failed and self.last_error is None:
                self.last_error = f"{failed} day(s) only partly downloaded - will retry"
                LOGGER.warning(
                    "[GarminSync] Migraine forecast: %d day(s) only partly downloaded "
                    "(Garmin rate limit?) - they are retried on the next refresh", failed,
                )
            self.last_refresh = dt_util.utcnow().isoformat()
            self._recompute()

    # ── Scoring ──────────────────────────────────────────────────────────────

    def _default_anchor(self, date_str: str) -> float:
        day = datetime.strptime(date_str, "%Y-%m-%d").replace(
            hour=_DEFAULT_ANCHOR_HOUR, tzinfo=dt_util.DEFAULT_TIME_ZONE
        )
        return day.timestamp()

    def _day_start(self, date_str: str) -> float:
        day = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
        return day.timestamp()

    @property
    def all_headaches(self) -> list[dict[str, Any]]:
        """Manual log plus the days answered "Migraines: yes" in Garmin Lifestyle Logging."""
        from_log = headaches_from_daily_log(self._records, self._day_start, self._default_anchor)
        return merge_headaches(self.headaches, from_log, dt_util.utcnow().timestamp())

    @property
    def days_incomplete(self) -> int:
        return sum(1 for record in self._records.values() if _needs_retry(record))

    @property
    def days_without_sleep(self) -> int:
        return sum(1 for record in self._records.values() if not record_has_night(record))

    @property
    def garmin_migraine_days(self) -> int:
        return sum(1 for record in self._records.values() if record.get("migraine_log") == "YES")

    @callback
    def _recompute(self) -> None:
        self.result = compute_forecast(
            self._records, self.all_headaches, dt_util.utcnow().timestamp(), self._default_anchor
        )
        async_dispatcher_send(self.hass, SIGNAL_MIGRAINE_UPDATED.format(entry_id=self.entry_id))

    @property
    def today(self) -> dict[str, Any] | None:
        """Today's scored day, or None until last night's sleep has synced."""
        if not self.result:
            return None
        today_str = dt_util.now().strftime("%Y-%m-%d")
        for day in reversed(self.result["days"]):
            if day["date"] == today_str:
                return day
        return None

    @property
    def status(self) -> str:
        return day_status(self.today)

    @property
    def nights_stored(self) -> int:
        return sum(1 for record in self._records.values() if record_has_night(record))

    # ── Headache log ─────────────────────────────────────────────────────────

    async def async_log_headache(
        self,
        onset: datetime | None = None,
        end: datetime | None = None,
        intensity: int | None = None,
        aura: bool = False,
        medication: str | None = None,
        notes: str | None = None,
    ) -> str:
        onset_ts = _timestamp(onset)
        end_ts = _timestamp(end) if end else None
        if end_ts is not None and end_ts < onset_ts:
            raise ValueError("Headache end is before its onset")
        headache = {
            "id": uuid.uuid4().hex[:8],
            "onset": onset_ts,
            "end": end_ts,
            "intensity": intensity,
            "aura": bool(aura),
            "medication": medication or None,
            "notes": notes or None,
        }
        self.headaches.append(headache)
        self.headaches.sort(key=lambda item: item["onset"])
        await self._save()
        self._recompute()
        return headache["id"]

    async def async_end_headache(self, end: datetime | None = None, headache_id: str | None = None) -> bool:
        """Set the end of ``headache_id``, or of the most recent headache still open."""
        candidates = [
            h for h in self.headaches
            if (h["id"] == headache_id if headache_id else h.get("end") is None)
        ]
        if not candidates:
            return False
        headache = candidates[-1]
        end_ts = _timestamp(end)
        if end_ts < headache["onset"]:
            raise ValueError("Headache end is before its onset")
        headache["end"] = end_ts
        await self._save()
        self._recompute()
        return True

    async def async_delete_headache(self, headache_id: str) -> bool:
        remaining = [h for h in self.headaches if h["id"] != headache_id]
        if len(remaining) == len(self.headaches):
            return False
        self.headaches = remaining
        await self._save()
        self._recompute()
        return True

    def headaches_for_display(self, limit: int = 15) -> list[dict[str, Any]]:
        def iso(ts: float | None) -> str | None:
            return dt_util.as_local(dt_util.utc_from_timestamp(ts)).isoformat(timespec="minutes") if ts else None

        return [
            {
                "id": h["id"], "source": h.get("source", "manual"),
                "onset": iso(h["onset"]), "end": iso(h.get("end")),
                "intensity": h.get("intensity"), "aura": h.get("aura", False),
                "medication": h.get("medication"), "notes": h.get("notes"),
            }
            for h in reversed(self.all_headaches[-limit:])
        ]
