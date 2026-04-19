"""Sensor platform - last sync status for Garmin Hydration Sync."""
from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_GARMIN_EMAIL,
    DOMAIN,
    KEY_LAST_SYNC_DAYS,
    KEY_LAST_SYNC_FAILED,
    KEY_LAST_SYNC_ML,
    KEY_LAST_SYNC_STATUS,
    KEY_LAST_SYNC_TIME,
    KEY_LAST_SYNC_WEIGHT_KG,
    KEY_LAST_SYNC_ABWHEEL,
)
from . import GarminSyncCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: GarminSyncCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        GarminLastSyncStatusSensor(coordinator, entry),
        GarminLastSyncMlSensor(coordinator, entry),
        GarminLastSyncWeightSensor(coordinator, entry),
        GarminLastSyncAbWheelSensor(coordinator, entry),
    ]
    async_add_entities(entities)


class _GarminSensorBase(CoordinatorEntity, SensorEntity):
    """Shared base - auto-updates whenever the coordinator refreshes."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: GarminSyncCoordinator,
        entry: ConfigEntry,
        key: str,
        unique_suffix: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._key = key
        email: str = entry.data.get(CONF_GARMIN_EMAIL, "")
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_{unique_suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"Garmin Hydration Sync ({email})",
            manufacturer="Garmin",
            model="Connect API",
        )

    @property
    def native_value(self):
        return self.coordinator.data.get(self._key) if self.coordinator.data else None


class GarminLastSyncStatusSensor(_GarminSensorBase):
    """Shows the status of the last sync (ok / error / never)."""

    _attr_icon = "mdi:cloud-check"
    _attr_name = "Last Sync Status"

    def __init__(
        self, coordinator: GarminSyncCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry, KEY_LAST_SYNC_STATUS, "last_sync_status")

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        return {
            "last_sync_time": data.get(KEY_LAST_SYNC_TIME),
            "last_sync_ml": data.get(KEY_LAST_SYNC_ML),
            "days_uploaded": data.get(KEY_LAST_SYNC_DAYS),
            "days_failed": data.get(KEY_LAST_SYNC_FAILED),
        }


class GarminLastSyncMlSensor(_GarminSensorBase):
    """Shows the amount of water (mL) uploaded in the last sync run."""

    _attr_icon = "mdi:water"
    _attr_name = "Last Synced Water"
    _attr_native_unit_of_measurement = "mL"

    def __init__(
        self, coordinator: GarminSyncCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry, KEY_LAST_SYNC_ML, "last_sync_ml")


class GarminLastSyncWeightSensor(_GarminSensorBase):
    """Shows the last body weight (kg) pushed to Garmin."""

    _attr_icon = "mdi:scale-bathroom"
    _attr_name = "Last Synced Weight"
    _attr_native_unit_of_measurement = "kg"

    def __init__(
        self, coordinator: GarminSyncCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry, KEY_LAST_SYNC_WEIGHT_KG, "last_sync_weight")


class GarminLastSyncAbWheelSensor(_GarminSensorBase):
    """Shows the last Ab Wheel workout summary pushed to Garmin."""

    _attr_icon = "mdi:dumbbell"
    _attr_name = "Last Synced Workout"

    def __init__(
        self, coordinator: GarminSyncCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry, KEY_LAST_SYNC_ABWHEEL, "last_sync_abwheel")

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        return {
            "last_sync_time": data.get(KEY_LAST_SYNC_TIME),
        }