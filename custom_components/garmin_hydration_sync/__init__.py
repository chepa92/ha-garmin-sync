"""Garmin Sync – Home Assistant integration.

Pushes hydration, body-weight and Ab Wheel workout data to Garmin Connect
whenever the corresponding HA sensors / events fire.  No polling, no recorder
history.

Hydration
---------
The water sensor (e.g. Water.io) is a cumulative "today" counter that resets
at midnight.  We track how many mL we have already sent for each calendar day
in a persistent Store and only push the *increment* since the last sync.
All hydration pushes are serialised by a lock and the increment is computed
inside it, so rapid sensor updates can never double-count.

Weight
------
Whenever the weight sensor produces a new valid reading we push it to Garmin
Connect via add_weigh_in().  The last pushed value is persisted, and a reading
identical to the last one pushed on the same day is skipped, so HA restarts,
Bluetooth reconnects and attribute-only updates don't create duplicate
weigh-ins.

Invalid states
--------------
Any state value of "unknown", "unavailable", or that cannot be parsed as a
number is silently ignored – this covers the case where the device is out of
Bluetooth range or the integration is restarting.
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
from datetime import timedelta
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_GARMIN_EMAIL,
    CONF_GARMIN_PASSWORD,
    CONF_WATER_SENSOR,
    CONF_WEIGHT_SENSOR,
    CONF_ABWHEEL_SYNC,
    DOMAIN,
    KEY_LAST_SYNC_DAYS,
    KEY_LAST_SYNC_FAILED,
    KEY_LAST_SYNC_ML,
    KEY_LAST_SYNC_STATUS,
    KEY_LAST_SYNC_TIME,
    KEY_LAST_SYNC_WEIGHT_KG,
    KEY_LAST_SYNC_ABWHEEL,
    LOGGER,
    SERVICE_SYNC_NOW,
    STATUS_ERROR,
    STATUS_IDLE,
    STATUS_OK,
)
from .garmin_connect import (
    GarminAuthExpiredError,
    async_upload_hydration,
    async_upload_weight,
    async_upload_abwheel_workout,
    parse_workout_start,
)

PLATFORMS: list[Platform] = [Platform.BUTTON, Platform.SENSOR]

# Sanity cap – ignore any daily water reading above this value
_MAX_DAILY_ML = 9000
# Sanity range for body weight (kg)
_MIN_WEIGHT_KG = 20.0
_MAX_WEIGHT_KG = 300.0
# Two weight readings closer than this are considered identical
_WEIGHT_EPSILON_KG = 0.01
# How many days of per-day hydration totals to keep in the Store
_HYDRATION_KEEP_DAYS = 14
# Persistent storage version
_STORE_VERSION = 1

# States that mean "no real value"
_BAD_STATES = {STATE_UNAVAILABLE, STATE_UNKNOWN, "unknown", "unavailable", "", None}


def _parse_positive_float(state_value: str | None) -> float | None:
    """Return a positive float from a state string, or None if invalid."""
    if state_value in _BAD_STATES:
        return None
    try:
        v = float(state_value)
        return v if v > 0 else None
    except (ValueError, TypeError):
        return None


def _to_int(value: Any, default: int = 0) -> int:
    """Best-effort int conversion for event / journal payload values."""
    try:
        return int(float(value))
    except (ValueError, TypeError):
        return default


def _now_iso() -> str:
    return dt_util.now().isoformat(timespec="seconds")


class GarminSyncCoordinator(DataUpdateCoordinator):
    """Coordinator that pushes data to Garmin on sensor state changes.

    ``update_interval`` is None (no polling).  Sensors are refreshed by
    calling ``async_set_updated_data()`` after each successful or failed push.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=None,   # event-driven only
            config_entry=entry,
        )
        self.entry = entry
        self.data: dict[str, Any] = {
            KEY_LAST_SYNC_STATUS:    STATUS_IDLE,
            KEY_LAST_SYNC_TIME:      None,
            KEY_LAST_SYNC_ML:        None,
            KEY_LAST_SYNC_WEIGHT_KG: None,
            KEY_LAST_SYNC_DAYS:      None,
            KEY_LAST_SYNC_FAILED:    None,
            KEY_LAST_SYNC_ABWHEEL:   None,
        }
        # Persistent store: {"YYYY-MM-DD": mL_already_sent_to_Garmin}
        self._store: Store = Store(hass, _STORE_VERSION, f"{DOMAIN}.{entry.entry_id}.sent")
        self._sent_hydration: dict[str, float] = {}
        self._hydration_lock = asyncio.Lock()
        # Persistent last pushed weight: {"kg": float, "date": "YYYY-MM-DD"}
        self._weight_store: Store = Store(hass, _STORE_VERSION, f"{DOMAIN}.{entry.entry_id}.weight")
        self._last_weight: dict[str, Any] = {}
        self._weight_lock = asyncio.Lock()
        # Persistent set of synced abwheel workout start_times (dedup key)
        self._abwheel_store: Store = Store(hass, _STORE_VERSION, f"{DOMAIN}.{entry.entry_id}.abwheel")
        self._synced_workouts: set[str] = set()
        self._pending_workouts: set[str] = set()  # in-flight uploads (race guard)
        # JSON history file path
        self._abwheel_history_path: str = hass.config.path(f"{DOMAIN}_abwheel_history.json")
        self._unsubs: list[Any] = []

    # ── Persistence ──────────────────────────────────────────────────────────

    async def async_load_store(self) -> None:
        stored = await self._store.async_load()
        if isinstance(stored, dict):
            self._sent_hydration = {
                k: float(v) for k, v in stored.items()
                if isinstance(v, (int, float))
            }
        weight_stored = await self._weight_store.async_load()
        if isinstance(weight_stored, dict):
            self._last_weight = weight_stored
        # Load synced abwheel workout keys
        abwheel_stored = await self._abwheel_store.async_load()
        if isinstance(abwheel_stored, list):
            self._synced_workouts = {str(x) for x in abwheel_stored}

    async def async_save_store(self) -> None:
        # Prune old days so the store doesn't grow forever
        cutoff = (dt_util.now() - timedelta(days=_HYDRATION_KEEP_DAYS)).strftime("%Y-%m-%d")
        self._sent_hydration = {
            k: v for k, v in self._sent_hydration.items() if k >= cutoff
        }
        await self._store.async_save(self._sent_hydration)

    async def _save_abwheel_store(self) -> None:
        await self._abwheel_store.async_save(sorted(self._synced_workouts))

    # ── Config helper ─────────────────────────────────────────────────────────

    def get_config(self) -> dict[str, Any]:
        return {**self.entry.data, **self.entry.options}

    # ── Status helpers ────────────────────────────────────────────────────────

    def _set_status(self, status: str, **extra: Any) -> None:
        self.async_set_updated_data({
            **self.data,
            KEY_LAST_SYNC_STATUS: status,
            KEY_LAST_SYNC_TIME:   _now_iso(),
            **extra,
        })

    def _handle_auth_expired(self) -> None:
        LOGGER.warning("[GarminSync] Garmin token expired – starting re-authentication flow")
        self._set_status(STATUS_ERROR)
        self.entry.async_start_reauth(self.hass)

    # ── State-change listeners setup ──────────────────────────────────────────

    def setup_listeners(self) -> None:
        cfg = self.get_config()
        water_id: str | None = cfg.get(CONF_WATER_SENSOR)
        weight_id: str | None = cfg.get(CONF_WEIGHT_SENSOR)

        if water_id:
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass, [water_id], self._handle_hydration_change
                )
            )
            LOGGER.debug("[GarminSync] Listening to hydration sensor: %s", water_id)

        if weight_id:
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass, [weight_id], self._handle_weight_change
                )
            )
            LOGGER.debug("[GarminSync] Listening to weight sensor: %s", weight_id)

        if cfg.get(CONF_ABWHEEL_SYNC, False):
            for event_type in (
                "xiaomi_abwheel_workout_completed",
                "xiaomi_abwheel_offline_workout",
            ):
                self._unsubs.append(
                    self.hass.bus.async_listen(event_type, self._handle_abwheel_event)
                )
            LOGGER.debug("[GarminSync] Listening for Ab Wheel workout events")

    def teardown_listeners(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    # ── Hydration ─────────────────────────────────────────────────────────────

    @callback
    def _handle_hydration_change(self, event: Event) -> None:
        new_state = event.data.get("new_state")
        if new_state is None:
            return

        ml = _parse_positive_float(new_state.state)
        if ml is None:
            LOGGER.debug(
                "[GarminSync] Hydration sensor state '%s' is invalid/unavailable – skipping",
                new_state.state,
            )
            return

        date_str: str = dt_util.as_local(new_state.last_changed).strftime("%Y-%m-%d")
        self.hass.async_create_task(
            self._sync_hydration(ml, date_str), eager_start=False
        )

    async def _sync_hydration(self, total_ml: float, date_str: str) -> bool:
        """Push the increment for ``date_str``.  Returns True if something was pushed.

        The delta is computed under a lock, after any in-flight upload has
        finished and updated ``_sent_hydration``, so concurrent triggers can
        never send the same millilitres twice.
        """
        if total_ml > _MAX_DAILY_ML:
            LOGGER.warning(
                "[GarminSync] Hydration value %.0f mL exceeds sanity cap %d – skipping",
                total_ml, _MAX_DAILY_ML,
            )
            return False

        async with self._hydration_lock:
            already_sent: float = self._sent_hydration.get(date_str, 0.0)
            delta = total_ml - already_sent
            if delta < 1:
                LOGGER.debug(
                    "[GarminSync] Hydration %s: no increment (%.0f mL, already sent %.0f mL)",
                    date_str, total_ml, already_sent,
                )
                return False

            LOGGER.info(
                "[GarminSync] Hydration %s: +%.0f mL (total %.0f, prev %.0f)",
                date_str, delta, total_ml, already_sent,
            )
            cfg = self.get_config()
            try:
                await async_upload_hydration(
                    self.hass,
                    cfg[CONF_GARMIN_EMAIL],
                    cfg[CONF_GARMIN_PASSWORD],
                    delta,
                    date_str,
                    prompt_mfa=None,
                )
            except GarminAuthExpiredError:
                self._handle_auth_expired()
                return False
            except Exception as exc:  # noqa: BLE001
                LOGGER.error("[GarminSync] Hydration %s failed: %s", date_str, exc)
                self._set_status(STATUS_ERROR)
                return False

            self._sent_hydration[date_str] = total_ml
            await self.async_save_store()

        self._set_status(STATUS_OK, **{KEY_LAST_SYNC_ML: int(total_ml)})
        LOGGER.info("[GarminSync] Hydration %s: %.0f mL synced OK", date_str, total_ml)
        return True

    # ── Weight ────────────────────────────────────────────────────────────────

    @callback
    def _handle_weight_change(self, event: Event) -> None:
        new_state = event.data.get("new_state")
        if new_state is None:
            return

        old_state = event.data.get("old_state")
        if old_state is not None and old_state.state == new_state.state:
            # Attribute-only update – not a new reading
            return

        kg = _parse_positive_float(new_state.state)
        if kg is None:
            LOGGER.debug(
                "[GarminSync] Weight sensor state '%s' is invalid/unavailable – skipping",
                new_state.state,
            )
            return

        self.hass.async_create_task(self._sync_weight(kg), eager_start=False)

    async def _sync_weight(self, weight_kg: float) -> bool:
        """Push a weigh-in unless it duplicates today's last pushed value."""
        if not (_MIN_WEIGHT_KG <= weight_kg <= _MAX_WEIGHT_KG):
            LOGGER.warning(
                "[GarminSync] Weight %.2f kg is outside valid range %.0f–%.0f kg – skipping",
                weight_kg, _MIN_WEIGHT_KG, _MAX_WEIGHT_KG,
            )
            return False

        async with self._weight_lock:
            today = dt_util.now().strftime("%Y-%m-%d")
            last_kg = self._last_weight.get("kg")
            if (
                isinstance(last_kg, (int, float))
                and self._last_weight.get("date") == today
                and abs(last_kg - weight_kg) < _WEIGHT_EPSILON_KG
            ):
                LOGGER.debug(
                    "[GarminSync] Weight %.2f kg already pushed today – skipping", weight_kg
                )
                return False

            LOGGER.info("[GarminSync] Weight %.2f kg – pushing to Garmin", weight_kg)
            cfg = self.get_config()
            try:
                await async_upload_weight(
                    self.hass,
                    cfg[CONF_GARMIN_EMAIL],
                    cfg[CONF_GARMIN_PASSWORD],
                    weight_kg,
                    prompt_mfa=None,
                )
            except GarminAuthExpiredError:
                self._handle_auth_expired()
                return False
            except Exception as exc:  # noqa: BLE001
                LOGGER.error("[GarminSync] Weight sync failed: %s", exc)
                self._set_status(STATUS_ERROR)
                return False

            self._last_weight = {"kg": round(weight_kg, 2), "date": today}
            await self._weight_store.async_save(self._last_weight)

        self._set_status(STATUS_OK, **{KEY_LAST_SYNC_WEIGHT_KG: round(weight_kg, 2)})
        LOGGER.info("[GarminSync] Weight %.2f kg synced OK", weight_kg)
        return True

    # ── Ab Wheel event handler ─────────────────────────────────────────────────

    @callback
    def _handle_abwheel_event(self, event: Event) -> None:
        """Handle xiaomi_abwheel_workout_completed / xiaomi_abwheel_offline_workout events."""
        data = event.data
        reps = _to_int(data.get("reps"))
        if reps < 1:
            return

        start_time = str(data.get("start_time") or "").strip()
        if not start_time:
            # No start time → use the event time so the workout still gets a
            # unique dedup key (and a sensible activity start time).
            start_time = str(int(event.time_fired.timestamp()))

        # Dedup: skip if this workout was already synced
        if start_time in self._synced_workouts:
            LOGGER.debug("[GarminSync] Ab Wheel workout %s already synced – skipping", start_time)
            return

        calories = _to_int(data.get("calories"))
        duration = _to_int(data.get("duration"))

        LOGGER.info(
            "[GarminSync] Ab Wheel workout: %d reps, %d cal, %ds (start=%s) – pushing to Garmin",
            reps, calories, duration, start_time,
        )
        self.hass.async_create_task(
            self._push_abwheel_workout(
                reps=reps,
                calories=calories,
                duration=duration,
                start_time=start_time,
                end_time=str(data.get("end_time") or ""),
                avg_freq=_to_int(data.get("avg_freq")),
                max_freq=_to_int(data.get("max_freq")),
            ),
            eager_start=False,
        )

    async def _push_abwheel_workout(
        self,
        reps: int,
        calories: int,
        duration: int,
        start_time: str,
        end_time: str,
        avg_freq: int,
        max_freq: int,
    ) -> None:
        """Upload Ab Wheel workout to Garmin Connect and write to history JSON."""
        # Guard against concurrent pushes of the same workout
        if start_time in self._synced_workouts or start_time in self._pending_workouts:
            LOGGER.debug("[GarminSync] Ab Wheel workout %s already synced/pending – skipping", start_time)
            return
        self._pending_workouts.add(start_time)
        cfg = self.get_config()
        try:
            try:
                result = await async_upload_abwheel_workout(
                    self.hass,
                    cfg[CONF_GARMIN_EMAIL],
                    cfg[CONF_GARMIN_PASSWORD],
                    reps=reps,
                    calories=calories,
                    duration_sec=duration,
                    start_time=start_time,
                    prompt_mfa=None,
                )
            except GarminAuthExpiredError:
                self._handle_auth_expired()
                return
            except Exception as exc:  # noqa: BLE001
                LOGGER.error("[GarminSync] Ab Wheel sync failed: %s", exc)
                self._set_status(STATUS_ERROR)
                return

            activity_id = result.get("activityId") if isinstance(result, dict) else None

            # Mark as synced (dedup) – upload succeeded, so do this before
            # anything else that could fail.
            self._synced_workouts.add(start_time)
            await self._save_abwheel_store()

            # Write to JSON history file (append, no duplicates).  A failure
            # here must not flag the (successful) upload as an error.
            try:
                await self.hass.async_add_executor_job(
                    self._append_abwheel_history,
                    start_time, end_time, reps, calories, duration,
                    avg_freq, max_freq, activity_id,
                )
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("[GarminSync] Could not write Ab Wheel history: %s", exc)

            summary = f"{reps} reps, {calories} cal, {duration}s"
            self._set_status(STATUS_OK, **{KEY_LAST_SYNC_ABWHEEL: summary})
            LOGGER.info(
                "[GarminSync] Ab Wheel workout synced OK: %s (activity %s)",
                summary, activity_id,
            )
        finally:
            self._pending_workouts.discard(start_time)

    def _append_abwheel_history(
        self,
        start_time: str,
        end_time: str,
        reps: int,
        calories: int,
        duration: int,
        avg_freq: int,
        max_freq: int,
        activity_id: str | None,
    ) -> None:
        """Append a workout record to the JSON history file (blocking, runs in executor)."""
        training_dt = parse_workout_start(start_time, dt_util.get_default_time_zone())
        if training_dt is not None:
            training_date = training_dt.strftime("%Y-%m-%d")
            training_time = training_dt.strftime("%H:%M:%S")
        else:
            training_date = dt_util.now().strftime("%Y-%m-%d")
            training_time = ""

        record = {
            "training_date": training_date,
            "training_time": training_time,
            "start_time": start_time,
            "end_time": end_time,
            "reps": reps,
            "calories": calories,
            "duration_sec": duration,
            "avg_freq": avg_freq,
            "max_freq": max_freq,
            "garmin_activity_id": activity_id,
            "synced_at": _now_iso(),
        }

        # Load existing history, check for duplicate, append
        history: list[dict] = []
        if os.path.exists(self._abwheel_history_path):
            try:
                with open(self._abwheel_history_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, list):
                    history = loaded
            except (json.JSONDecodeError, OSError):
                history = []

        # Don't duplicate: check by start_time
        existing_keys = {str(r.get("start_time")) for r in history if isinstance(r, dict)}
        if start_time in existing_keys:
            return

        history.append(record)
        with open(self._abwheel_history_path, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2, ensure_ascii=False)
        LOGGER.debug("[GarminSync] Wrote abwheel history: %s", self._abwheel_history_path)

    # ── Retry failed Ab Wheel uploads from journal ─────────────────────────

    async def async_retry_abwheel_from_journal(self) -> None:
        """Read abwheel journal files and push any workouts not yet synced."""
        cfg = self.get_config()
        if not cfg.get(CONF_ABWHEEL_SYNC, False):
            return
        pattern = self.hass.config.path("xiaomi_abwheel_journal_*.json")
        journal_files = await self.hass.async_add_executor_job(glob.glob, pattern)
        for path in journal_files:
            try:
                raw = await self.hass.async_add_executor_job(self._read_json_file, path)
                if not isinstance(raw, list):
                    continue
                for item in raw:
                    if not isinstance(item, dict):
                        continue
                    st = str(item.get("start_time") or "").strip()
                    if not st or st in self._synced_workouts:
                        continue
                    reps = _to_int(item.get("reps"))
                    if reps < 1:
                        continue
                    LOGGER.info(
                        "[GarminSync] Retrying unsynced Ab Wheel workout from journal: %d reps, start=%s",
                        reps, st,
                    )
                    await self._push_abwheel_workout(
                        reps=reps,
                        calories=_to_int(item.get("calories")),
                        duration=_to_int(item.get("duration_sec", item.get("duration"))),
                        start_time=st,
                        end_time=str(item.get("end_time") or ""),
                        avg_freq=_to_int(item.get("avg_freq")),
                        max_freq=_to_int(item.get("max_freq")),
                    )
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("[GarminSync] Error reading journal %s: %s", path, exc)

    @staticmethod
    def _read_json_file(path: str) -> Any:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    # ── Manual sync (sync_now service / button) ───────────────────────────────

    async def async_sync_now(self) -> None:
        """Push current sensor values to Garmin immediately (skipping duplicates)."""
        cfg = self.get_config()
        pushed_any = False

        water_id: str | None = cfg.get(CONF_WATER_SENSOR)
        if water_id:
            state = self.hass.states.get(water_id)
            ml = _parse_positive_float(state.state if state else None)
            if ml is not None:
                date_str = dt_util.now().strftime("%Y-%m-%d")
                pushed_any |= await self._sync_hydration(ml, date_str)

        weight_id: str | None = cfg.get(CONF_WEIGHT_SENSOR)
        if weight_id:
            state = self.hass.states.get(weight_id)
            kg = _parse_positive_float(state.state if state else None)
            if kg is not None:
                pushed_any |= await self._sync_weight(kg)

        if not pushed_any:
            LOGGER.info("[GarminSync] sync_now: nothing to push (sensors unavailable or no change)")

    # ── DataUpdateCoordinator override ───────────────────────────────────────

    async def _async_update_data(self) -> dict[str, Any]:
        """Not used for polling; data is pushed via async_set_updated_data()."""
        return self.data


# ── Entry setup / teardown ───────────────────────────────────────────────────

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = GarminSyncCoordinator(hass, entry)
    await coordinator.async_load_store()

    # Seed coordinator data so sensors show "never" immediately on load
    coordinator.async_set_updated_data(coordinator.data)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    coordinator.setup_listeners()
    entry.async_on_unload(coordinator.teardown_listeners)

    # Retry any failed Ab Wheel uploads from the journal (cancelled on unload)
    entry.async_create_background_task(
        hass,
        coordinator.async_retry_abwheel_from_journal(),
        f"{DOMAIN}_abwheel_journal_retry_{entry.entry_id}",
    )

    async def _handle_sync_now(call: ServiceCall) -> None:
        # Always look coordinators up at call time – never capture one in the
        # closure, or a reloaded entry would leave the service pointing at a
        # stale, torn-down coordinator.
        coordinators: dict[str, GarminSyncCoordinator] = hass.data.get(DOMAIN, {})
        entry_id: str | None = call.data.get("entry_id")
        if entry_id:
            coord = coordinators.get(entry_id)
            if coord is None:
                LOGGER.warning("[GarminSync] sync_now: unknown entry_id %s", entry_id)
                return
            targets = [coord]
        else:
            targets = list(coordinators.values())
        for coord in targets:
            await coord.async_sync_now()

    if not hass.services.has_service(DOMAIN, SERVICE_SYNC_NOW):
        hass.services.async_register(
            DOMAIN, SERVICE_SYNC_NOW, _handle_sync_now,
            schema=vol.Schema({vol.Optional("entry_id"): cv.string}),
        )

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_SYNC_NOW)
    return unload_ok
