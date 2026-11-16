"""Sensors of the migraine forecast (created only when the option is enabled).

Wording is deliberate: the state says how far this morning is from the person's own
normal days — never that a migraine is coming.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo

from .const import CONF_GARMIN_EMAIL, DOMAIN, SIGNAL_MIGRAINE_UPDATED
from .migraine import (
    ALERT_PERCENTILE,
    HORIZON_HOURS,
    MIN_BASELINE,
    MIN_NORMAL,
    MIN_PRE_HEADACHE,
    STATUS_COLLECTING,
    STATUS_ELEVATED,
    STATUS_NO_DATA,
    STATUS_USUAL,
)
from .migraine_manager import MigraineForecastManager


class _MigraineSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, manager: MigraineForecastManager, entry: ConfigEntry, suffix: str) -> None:
        self._manager = manager
        email: str = entry.data.get(CONF_GARMIN_EMAIL, "")
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_migraine_{suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"Garmin Hydration Sync ({email})",
            manufacturer="Garmin",
            model="Connect API",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_MIGRAINE_UPDATED.format(entry_id=self._manager.entry_id),
                self._handle_update,
            )
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()


class MigraineStatusSensor(_MigraineSensor):
    """no_data / collecting_baseline / usual / elevated, with the full picture as attributes."""

    _attr_name = "Migraine Forecast"
    _attr_icon = "mdi:head-heart-outline"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [STATUS_NO_DATA, STATUS_COLLECTING, STATUS_USUAL, STATUS_ELEVATED]

    def __init__(self, manager: MigraineForecastManager, entry: ConfigEntry) -> None:
        super().__init__(manager, entry, "status")

    @property
    def native_value(self) -> str:
        return self._manager.status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        manager = self._manager
        today = manager.today
        result = manager.result or {}
        attrs: dict[str, Any] = {
            "horizon_hours": HORIZON_HOURS,
            "alert_percentile": ALERT_PERCENTILE,
            "nights_stored": manager.nights_stored,
            "days_without_sleep": manager.days_without_sleep,
            "days_incomplete": manager.days_incomplete,
            "normal_days": result.get("normal_days"),
            "pre_headache_days": result.get("pre_headache_days"),
            "baseline_days_needed": MIN_BASELINE,
            "last_refresh": manager.last_refresh,
            "last_error": manager.last_error,
        }
        if today is None:
            attrs["summary"] = "Last night's sleep has not synced from the watch yet."
            return attrs
        attrs.update({
            "date": today["date"],
            "baseline_days": today["baseline_days"],
            "features_available": sum(1 for value in today["features"].values() if value is not None),
            "deviation_index": _round(today["deviation_index"]),
            "directional_index": _round(today["directional_index"]),
            "percentile": _round(today["percentile"], 0),
            "moved_most": today["top_features"],
        })
        if today["percentile"] is not None:
            attrs["summary"] = (
                f"Further from your usual than {today['percentile']:.0f}% of your normal days."
            )
        else:
            attrs["summary"] = (
                f"Building your baseline: {today['baseline_days']} of {MIN_BASELINE} normal days."
            )
        return attrs


class MigraineDeviationSensor(_MigraineSensor):
    """How far this morning is from the personal baseline (RMS of robust z-scores)."""

    _attr_name = "Migraine Deviation Index"
    _attr_icon = "mdi:chart-bell-curve"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2

    def __init__(self, manager: MigraineForecastManager, entry: ConfigEntry) -> None:
        super().__init__(manager, entry, "deviation_index")

    @property
    def native_value(self) -> float | None:
        today = self._manager.today
        return _round(today["deviation_index"]) if today else None


class MigrainePercentileSensor(_MigraineSensor):
    """Share of earlier normal days this morning is further from usual than."""

    _attr_name = "Migraine Deviation Percentile"
    _attr_icon = "mdi:percent-outline"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, manager: MigraineForecastManager, entry: ConfigEntry) -> None:
        super().__init__(manager, entry, "percentile")

    @property
    def native_value(self) -> float | None:
        today = self._manager.today
        return _round(today["percentile"], 0) if today else None


class MigraineEvaluationSensor(_MigraineSensor):
    """Does the score separate pre-headache mornings from normal ones? (AUC, 0.5 = no.)"""

    _attr_name = "Migraine Forecast Accuracy"
    _attr_icon = "mdi:check-decagram-outline"
    _attr_suggested_display_precision = 2

    def __init__(self, manager: MigraineForecastManager, entry: ConfigEntry) -> None:
        super().__init__(manager, entry, "evaluation")

    @property
    def native_value(self) -> float | None:
        evaluation = (self._manager.result or {}).get("evaluation")
        return evaluation["deviation_index_auc"] if evaluation else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        result = self._manager.result or {}
        evaluation = result.get("evaluation")
        if evaluation:
            return evaluation
        return {
            "summary": (
                f"Needs {MIN_PRE_HEADACHE} pre-headache and {MIN_NORMAL} normal days with a score; "
                f"labelled so far: {result.get('pre_headache_days', 0)} and {result.get('normal_days', 0)}."
            )
        }


class MigraineHeadacheLogSensor(_MigraineSensor):
    """Number of logged headaches; the most recent ones (with ids) as an attribute."""

    _attr_name = "Headache Log"
    _attr_icon = "mdi:notebook-heart-outline"

    def __init__(self, manager: MigraineForecastManager, entry: ConfigEntry) -> None:
        super().__init__(manager, entry, "headache_log")

    @property
    def native_value(self) -> int:
        return len(self._manager.all_headaches)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "open_headache": any(h.get("end") is None for h in self._manager.headaches),
            "manual_entries": len(self._manager.headaches),
            "garmin_migraine_days": self._manager.garmin_migraine_days,
            "headaches": self._manager.headaches_for_display(),
        }


def _round(value: float | None, digits: int = 2) -> float | None:
    return round(value, digits) if value is not None else None


def migraine_sensors(manager: MigraineForecastManager, entry: ConfigEntry) -> list[SensorEntity]:
    return [
        MigraineStatusSensor(manager, entry),
        MigraineDeviationSensor(manager, entry),
        MigrainePercentileSensor(manager, entry),
        MigraineEvaluationSensor(manager, entry),
        MigraineHeadacheLogSensor(manager, entry),
    ]
