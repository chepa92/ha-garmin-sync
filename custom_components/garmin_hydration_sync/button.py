"""Button platform – "Sync to Garmin" button for Garmin Hydration Sync."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_GARMIN_EMAIL, DOMAIN, LOGGER
from . import GarminSyncCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: GarminSyncCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([GarminSyncButton(coordinator, entry)])


class GarminSyncButton(ButtonEntity):
    """A button that triggers an immediate hydration sync to Garmin Connect."""

    _attr_icon = "mdi:cloud-upload"
    _attr_has_entity_name = True
    _attr_name = "Sync to Garmin"

    def __init__(
        self, coordinator: GarminSyncCoordinator, entry: ConfigEntry
    ) -> None:
        self._coordinator = coordinator
        self._entry = entry
        email: str = entry.data.get(CONF_GARMIN_EMAIL, "")
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_sync_button"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"Garmin Hydration Sync ({email})",
            manufacturer="Garmin",
            model="Connect API",
            entry_type=None,
        )

    async def async_press(self) -> None:
        """Handle button press – trigger an immediate sync."""
        LOGGER.debug("[GarminSync] Manual sync triggered via button")
        await self._coordinator.async_sync_now()

