"""Button platform – "Sync to Garmin" button for Garmin Hydration Sync."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
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
    entities: list[ButtonEntity] = [GarminSyncButton(coordinator, entry)]
    if coordinator.migraine is not None:
        entities += [
            HeadacheButton(coordinator, entry, "headache_start", "Headache Started", "mdi:head-alert-outline"),
            HeadacheButton(coordinator, entry, "headache_end", "Headache Ended", "mdi:head-check-outline"),
        ]
    async_add_entities(entities)


class HeadacheButton(ButtonEntity):
    """One-tap headache log: "started now" / "ended now" (details via the services)."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: GarminSyncCoordinator, entry: ConfigEntry, kind: str, name: str, icon: str
    ) -> None:
        self._coordinator = coordinator
        self._kind = kind
        self._attr_name = name
        self._attr_icon = icon
        email: str = entry.data.get(CONF_GARMIN_EMAIL, "")
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_{kind}_button"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=f"Garmin Hydration Sync ({email})",
            manufacturer="Garmin",
            model="Connect API",
        )

    async def async_press(self) -> None:
        manager = self._coordinator.migraine
        if manager is None:
            return
        if self._kind == "headache_start":
            # A second press while one is still open would create a duplicate.
            if any(h.get("end") is None for h in manager.headaches):
                raise HomeAssistantError("A headache is already open - press Headache Ended first.")
            await manager.async_log_headache()
        elif not await manager.async_end_headache():
            raise HomeAssistantError("No open headache to end.")


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
        )

    async def async_press(self) -> None:
        """Handle button press – trigger an immediate sync."""
        LOGGER.debug("[GarminSync] Manual sync triggered via button")
        await self._coordinator.async_sync_now()

