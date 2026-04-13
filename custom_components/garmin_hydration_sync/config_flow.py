"""Config flow for Garmin Hydration Sync."""
from __future__ import annotations

import asyncio
import queue as queue_module
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    CONF_GARMIN_EMAIL,
    CONF_GARMIN_PASSWORD,
    CONF_WATER_SENSOR,
    CONF_WEIGHT_SENSOR,
    DOMAIN,
    LOGGER,
)
from .garmin_connect import async_login_only


# ── Schema helpers ───────────────────────────────────────────────────────────

def _user_schema(defaults: dict[str, Any], hass: HomeAssistant) -> vol.Schema:
    weight_default = defaults.get(CONF_WEIGHT_SENSOR)  # None if not set
    return vol.Schema(
        {
            vol.Required(
                CONF_GARMIN_EMAIL,
                default=defaults.get(CONF_GARMIN_EMAIL, ""),
            ): str,
            vol.Required(
                CONF_GARMIN_PASSWORD,
                default=defaults.get(CONF_GARMIN_PASSWORD, ""),
            ): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
            vol.Required(
                CONF_WATER_SENSOR,
                default=defaults.get(CONF_WATER_SENSOR, ""),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
            **(
                {
                    vol.Optional(
                        CONF_WEIGHT_SENSOR, default=weight_default
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor")
                    )
                }
                if weight_default
                else {
                    vol.Optional(
                        CONF_WEIGHT_SENSOR
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor")
                    )
                }
            ),
        }
    )


def _mfa_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required("code"): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
            ),
        }
    )


def _reauth_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Schema for the reauth_confirm step – only asks for a new password."""
    return vol.Schema(
        {
            vol.Required(
                CONF_GARMIN_PASSWORD,
                default=defaults.get(CONF_GARMIN_PASSWORD, ""),
            ): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
        }
    )


def _options_schema(defaults: dict[str, Any], hass: HomeAssistant) -> vol.Schema:
    weight_default = defaults.get(CONF_WEIGHT_SENSOR)  # None if not set
    return vol.Schema(
        {
            vol.Required(
                CONF_WATER_SENSOR,
                default=defaults.get(CONF_WATER_SENSOR, ""),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            ),
            **(
                {
                    vol.Optional(
                        CONF_WEIGHT_SENSOR, default=weight_default
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor")
                    )
                }
                if weight_default
                else {
                    vol.Optional(
                        CONF_WEIGHT_SENSOR
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor")
                    )
                }
            ),
        }
    )


# ── Config Flow ──────────────────────────────────────────────────────────────

class GarminHydrationSyncConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Garmin Hydration Sync.

    Step 1 – user:   email / password / sensor / interval
      → starts login in background executor
      → waits for either MFA-needed or login-done event
      If MFA needed → step 2
      If done (no MFA) → create entry

    Step 2 – mfa:   one-time code text entry
      → feeds code into the waiting executor thread via a Queue
      → waits for login to finish → create entry
    """

    VERSION = 1

    def __init__(self) -> None:
        self._stored_input: dict[str, Any] = {}
        self._mfa_queue: queue_module.Queue = queue_module.Queue()
        self._mfa_needed_event: asyncio.Event = asyncio.Event()
        self._login_done_event: asyncio.Event = asyncio.Event()
        self._login_error: str | None = None
        self._login_task: asyncio.Task | None = None
        self._is_reauth: bool = False

    # ── Step 1: credentials ──────────────────────────────────────────────────

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            sensor_id: str = user_input[CONF_WATER_SENSOR]
            if self.hass.states.get(sensor_id) is None:
                errors[CONF_WATER_SENSOR] = "entity_not_found"
            else:
                # Set unique ID early so duplicates are blocked
                await self.async_set_unique_id(user_input[CONF_GARMIN_EMAIL].lower())
                self._abort_if_unique_id_configured()

                self._stored_input = user_input

                # Reset sync events
                self._mfa_needed_event.clear()
                self._login_done_event.clear()
                self._login_error = None

                # Build the prompt_mfa callback that blocks the executor thread
                # until the user submits the MFA step form.
                mfa_queue = self._mfa_queue

                def _prompt_mfa() -> str:
                    """Called by garminconnect inside the executor thread."""
                    # Signal the event loop that MFA is required
                    asyncio.run_coroutine_threadsafe(
                        self._async_signal_mfa_needed(), self.hass.loop
                    ).result(timeout=5)
                    LOGGER.info("[GarminHydrationSync] Waiting for 2FA code from user…")
                    try:
                        code = mfa_queue.get(timeout=300)  # 5 min to enter code
                    except queue_module.Empty:
                        raise RuntimeError("Garmin 2FA timeout: no code entered within 5 minutes")
                    return code

                # Start login as a background task
                self._login_task = self.hass.async_create_task(
                    self._async_do_login(_prompt_mfa)
                )

                # Wait until either MFA is needed OR login finishes
                mfa_wait = asyncio.ensure_future(self._mfa_needed_event.wait())
                done_wait = asyncio.ensure_future(self._login_done_event.wait())
                done, pending = await asyncio.wait(
                    [mfa_wait, done_wait],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()

                if self._login_done_event.is_set() and not self._mfa_needed_event.is_set():
                    # Login succeeded (tokens were already cached) or failed
                    if self._login_error:
                        LOGGER.error("[GarminHydrationSync] Login failed: %s", self._login_error)
                        errors["base"] = "cannot_connect"
                    else:
                        if self._is_reauth:
                            return self._finish_reauth()
                        return self._create_entry()
                else:
                    # MFA code required – show MFA step
                    return await self.async_step_mfa()

        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema({}, self.hass),
            errors=errors,
        )

    # ── Step 2: 2FA code ─────────────────────────────────────────────────────

    async def async_step_mfa(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            code: str = user_input.get("code", "").strip()
            if not code:
                errors["code"] = "invalid_mfa_code"
            else:
                # Feed the code to the waiting executor thread
                self._mfa_queue.put(code)

                # Wait for login to finish
                await self._login_done_event.wait()

                if self._login_error:
                    LOGGER.error(
                        "[GarminHydrationSync] Login failed after MFA: %s",
                        self._login_error,
                    )
                    errors["base"] = "invalid_mfa_code"
                else:
                    if self._is_reauth:
                        return self._finish_reauth()
                    return self._create_entry()

        return self.async_show_form(
            step_id="mfa",
            data_schema=_mfa_schema(),
            errors=errors,
            description_placeholders={
                "email": self._stored_input.get(CONF_GARMIN_EMAIL, "")
            },
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _async_signal_mfa_needed(self) -> None:
        self._mfa_needed_event.set()

    async def _async_do_login(self, prompt_mfa) -> None:
        """Run the blocking login in an executor and capture any error."""
        try:
            await async_login_only(
                self.hass,
                self._stored_input[CONF_GARMIN_EMAIL],
                self._stored_input[CONF_GARMIN_PASSWORD],
                prompt_mfa=prompt_mfa,
            )
        except Exception as exc:  # noqa: BLE001
            self._login_error = str(exc)
        finally:
            self._login_done_event.set()

    def _create_entry(self) -> FlowResult:
        email: str = self._stored_input[CONF_GARMIN_EMAIL]
        data: dict[str, Any] = {
            CONF_GARMIN_EMAIL: email,
            CONF_GARMIN_PASSWORD: self._stored_input[CONF_GARMIN_PASSWORD],
            CONF_WATER_SENSOR: self._stored_input[CONF_WATER_SENSOR],
        }
        weight = self._stored_input.get(CONF_WEIGHT_SENSOR, "")
        if weight:
            data[CONF_WEIGHT_SENSOR] = weight
        return self.async_create_entry(title=f"Garmin \u2013 {email}", data=data)

    def _finish_reauth(self) -> FlowResult:
        """Update the existing config entry with a fresh password and reload it."""
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if entry is None:
            return self.async_abort(reason="unknown")
        updated = {**entry.data, CONF_GARMIN_PASSWORD: self._stored_input[CONF_GARMIN_PASSWORD]}
        self.hass.config_entries.async_update_entry(entry, data=updated)
        self.hass.async_create_task(
            self.hass.config_entries.async_reload(entry.entry_id)
        )
        return self.async_abort(reason="reauth_successful")

    # ── Reauth flow ───────────────────────────────────────────────────────────

    async def async_step_reauth(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Called by HA when async_start_reauth() is triggered."""
        self._is_reauth = True
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask the user to re-enter their Garmin password (and handle 2FA)."""
        errors: dict[str, str] = {}
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        entry_data = entry.data if entry else {}

        if user_input is not None:
            self._stored_input = {
                **entry_data,
                CONF_GARMIN_PASSWORD: user_input[CONF_GARMIN_PASSWORD],
            }
            self._mfa_queue = queue_module.Queue()
            self._mfa_needed_event.clear()
            self._login_done_event.clear()
            self._login_error = None

            mfa_queue = self._mfa_queue

            def _prompt_mfa() -> str:
                asyncio.run_coroutine_threadsafe(
                    self._async_signal_mfa_needed(), self.hass.loop
                ).result(timeout=5)
                LOGGER.info("[GarminSync] Reauth: waiting for 2FA code from user…")
                try:
                    code = mfa_queue.get(timeout=300)
                except queue_module.Empty:
                    raise RuntimeError("Garmin 2FA timeout")
                return code

            self._login_task = self.hass.async_create_task(
                self._async_do_login(_prompt_mfa)
            )

            mfa_wait = asyncio.ensure_future(self._mfa_needed_event.wait())
            done_wait = asyncio.ensure_future(self._login_done_event.wait())
            done, pending = await asyncio.wait(
                [mfa_wait, done_wait], return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()

            if self._login_done_event.is_set() and not self._mfa_needed_event.is_set():
                if self._login_error:
                    errors["base"] = "cannot_connect"
                else:
                    return self._finish_reauth()
            else:
                return await self.async_step_mfa()

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_reauth_schema(entry_data),
            errors=errors,
            description_placeholders={
                "email": entry_data.get(CONF_GARMIN_EMAIL, "")
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return GarminHydrationSyncOptionsFlow(config_entry)


# ── Options Flow ─────────────────────────────────────────────────────────────

class GarminHydrationSyncOptionsFlow(OptionsFlow):
    """Allow changing sensors without re-authenticating."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        current = {**self._entry.data, **self._entry.options}

        if user_input is not None:
            sensor_id: str = user_input[CONF_WATER_SENSOR]
            if self.hass.states.get(sensor_id) is None:
                errors[CONF_WATER_SENSOR] = "entity_not_found"
            else:
                out: dict[str, Any] = {CONF_WATER_SENSOR: sensor_id}
                weight = user_input.get(CONF_WEIGHT_SENSOR, "")
                if weight:
                    out[CONF_WEIGHT_SENSOR] = weight
                return self.async_create_entry(title="", data=out)

        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(current, self.hass),
            errors=errors,
        )

