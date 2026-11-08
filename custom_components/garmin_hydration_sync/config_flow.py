"""Config flow for Garmin Hydration Sync."""
from __future__ import annotations

import asyncio
import queue as queue_module
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    CONF_GARMIN_EMAIL,
    CONF_GARMIN_PASSWORD,
    CONF_WATER_SENSOR,
    CONF_WEIGHT_SENSOR,
    CONF_ABWHEEL_SYNC,
    CONF_JUMPROPE_SYNC,
    DOMAIN,
    LOGGER,
)
from .garmin_connect import async_login_only

# How long the executor thread waits for the user to type the 2FA code
_MFA_TIMEOUT_SEC = 300

# Results of a login attempt
_LOGIN_OK = "ok"
_LOGIN_MFA = "mfa"
_LOGIN_ERROR = "error"


# ── Schema helpers ───────────────────────────────────────────────────────────

def _optional_entity(key: str, defaults: dict[str, Any]) -> dict:
    """Build an optional, clearable entity selector field.

    Uses ``suggested_value`` instead of ``default``: with a voluptuous default,
    clearing the field in the UI would silently re-insert the old value.
    """
    val = defaults.get(key)
    return {
        vol.Optional(
            key, description={"suggested_value": val} if val else None
        ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
    }


def _user_schema(defaults: dict[str, Any]) -> vol.Schema:
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
            **_optional_entity(CONF_WATER_SENSOR, defaults),
            **_optional_entity(CONF_WEIGHT_SENSOR, defaults),
            vol.Optional(
                CONF_ABWHEEL_SYNC,
                default=defaults.get(CONF_ABWHEEL_SYNC, False),
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_JUMPROPE_SYNC,
                default=defaults.get(CONF_JUMPROPE_SYNC, False),
            ): selector.BooleanSelector(),
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


def _reauth_schema() -> vol.Schema:
    """Schema for the reauth_confirm step – only asks for a new password."""
    return vol.Schema(
        {
            vol.Required(CONF_GARMIN_PASSWORD): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
        }
    )


def _options_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            **_optional_entity(CONF_WATER_SENSOR, defaults),
            **_optional_entity(CONF_WEIGHT_SENSOR, defaults),
            vol.Optional(
                CONF_ABWHEEL_SYNC,
                default=bool(defaults.get(CONF_ABWHEEL_SYNC, False)),
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_JUMPROPE_SYNC,
                default=bool(defaults.get(CONF_JUMPROPE_SYNC, False)),
            ): selector.BooleanSelector(),
        }
    )


def _has_any_source(user_input: dict[str, Any]) -> bool:
    return any([
        user_input.get(CONF_WATER_SENSOR),
        user_input.get(CONF_WEIGHT_SENSOR),
        user_input.get(CONF_ABWHEEL_SYNC, False),
        user_input.get(CONF_JUMPROPE_SYNC, False),
    ])


# ── Config Flow ──────────────────────────────────────────────────────────────

class GarminHydrationSyncConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Garmin Hydration Sync.

    Step 1 – user (or reauth_confirm): credentials / sensors
      → starts login in a background executor job
      → waits for either MFA-needed or login-done
      If MFA needed → step 2, if done → create entry / finish reauth

    Step 2 – mfa: one-time code text entry
      → feeds code into the waiting executor thread via a Queue
      → waits for login to finish (or for Garmin to ask for a code again)
      → on failure a fresh login is started so the user can retry
    """

    VERSION = 1

    def __init__(self) -> None:
        self._stored_input: dict[str, Any] = {}
        self._mfa_queue: queue_module.Queue = queue_module.Queue()
        self._mfa_needed_event: asyncio.Event = asyncio.Event()
        self._login_done_event: asyncio.Event = asyncio.Event()
        self._login_error: str | None = None
        self._login_task: asyncio.Task | None = None
        self._reauth_entry: ConfigEntry | None = None

    # ── Login orchestration ──────────────────────────────────────────────────

    async def _async_wait_login_progress(self) -> str:
        """Wait until login finishes or Garmin asks for an MFA code."""
        mfa_wait = asyncio.ensure_future(self._mfa_needed_event.wait())
        done_wait = asyncio.ensure_future(self._login_done_event.wait())
        try:
            await asyncio.wait([mfa_wait, done_wait], return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (mfa_wait, done_wait):
                t.cancel()

        if self._login_done_event.is_set():
            if self._login_error:
                LOGGER.error("[GarminSync] Login failed: %s", self._login_error)
                return _LOGIN_ERROR
            return _LOGIN_OK
        return _LOGIN_MFA

    async def _async_start_login(self) -> str:
        """Start a fresh login with ``self._stored_input`` and wait for progress."""
        # Fresh queue + events so leftovers from a previous attempt can't leak in
        self._mfa_queue = queue_module.Queue()
        self._mfa_needed_event.clear()
        self._login_done_event.clear()
        self._login_error = None

        mfa_queue = self._mfa_queue
        loop = self.hass.loop

        def _prompt_mfa() -> str:
            """Called by garminconnect inside the executor thread."""
            asyncio.run_coroutine_threadsafe(
                self._async_signal_mfa_needed(), loop
            ).result(timeout=5)
            LOGGER.info("[GarminSync] Waiting for 2FA code from user…")
            try:
                return mfa_queue.get(timeout=_MFA_TIMEOUT_SEC)
            except queue_module.Empty:
                raise RuntimeError("Garmin 2FA timeout: no code entered in time") from None

        self._login_task = self.hass.async_create_task(self._async_do_login(_prompt_mfa))
        return await self._async_wait_login_progress()

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
            self._login_error = str(exc) or type(exc).__name__
        finally:
            self._login_done_event.set()

    def _finish(self) -> FlowResult:
        if self._reauth_entry is not None:
            return self._finish_reauth()
        return self._create_entry()

    # ── Step 1: credentials ──────────────────────────────────────────────────

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            if not _has_any_source(user_input):
                errors["base"] = "no_sensors"
            else:
                # Set unique ID early so duplicates are blocked
                await self.async_set_unique_id(user_input[CONF_GARMIN_EMAIL].strip().lower())
                self._abort_if_unique_id_configured()

                self._stored_input = {
                    **user_input,
                    CONF_GARMIN_EMAIL: user_input[CONF_GARMIN_EMAIL].strip(),
                }
                result = await self._async_start_login()
                if result == _LOGIN_OK:
                    return self._finish()
                if result == _LOGIN_MFA:
                    return await self.async_step_mfa()
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(user_input or {}),
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
            elif self._login_done_event.is_set():
                # The previous login attempt already ended (e.g. timed out):
                # start a fresh one, which makes Garmin send a new code.
                result = await self._async_start_login()
                if result == _LOGIN_OK:
                    return self._finish()
                if result == _LOGIN_ERROR:
                    return self._show_initial_form_error("cannot_connect")
                errors["base"] = "invalid_mfa_code_resent"
            else:
                # Feed the code to the waiting executor thread, then wait for
                # the login to finish or for Garmin to ask for a code again.
                self._mfa_needed_event.clear()
                self._mfa_queue.put(code)
                result = await self._async_wait_login_progress()
                if result == _LOGIN_OK:
                    return self._finish()
                if result == _LOGIN_MFA:
                    # Same login is still alive and asking again → wrong code
                    errors["base"] = "invalid_mfa_code"
                else:
                    # Login ended with an error → start over so the user can
                    # retry with a freshly sent code.
                    result = await self._async_start_login()
                    if result == _LOGIN_OK:
                        return self._finish()
                    if result == _LOGIN_ERROR:
                        return self._show_initial_form_error("cannot_connect")
                    errors["base"] = "invalid_mfa_code_resent"

        return self.async_show_form(
            step_id="mfa",
            data_schema=_mfa_schema(),
            errors=errors,
            description_placeholders={
                "email": self._stored_input.get(CONF_GARMIN_EMAIL, "")
            },
        )

    def _show_initial_form_error(self, error: str) -> FlowResult:
        """Return to the first step (user or reauth_confirm) with an error."""
        if self._reauth_entry is not None:
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=_reauth_schema(),
                errors={"base": error},
                description_placeholders={
                    "email": self._reauth_entry.data.get(CONF_GARMIN_EMAIL, "")
                },
            )
        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(self._stored_input),
            errors={"base": error},
        )

    # ── Entry creation ────────────────────────────────────────────────────────

    def _create_entry(self) -> FlowResult:
        email: str = self._stored_input[CONF_GARMIN_EMAIL]
        data: dict[str, Any] = {
            CONF_GARMIN_EMAIL: email,
            CONF_GARMIN_PASSWORD: self._stored_input[CONF_GARMIN_PASSWORD],
        }
        for k in (CONF_WATER_SENSOR, CONF_WEIGHT_SENSOR):
            v = self._stored_input.get(k, "")
            if v:
                data[k] = v
        if self._stored_input.get(CONF_ABWHEEL_SYNC, False):
            data[CONF_ABWHEEL_SYNC] = True
        if self._stored_input.get(CONF_JUMPROPE_SYNC, False):
            data[CONF_JUMPROPE_SYNC] = True
        return self.async_create_entry(title=f"Garmin – {email}", data=data)

    def _finish_reauth(self) -> FlowResult:
        """Store the new password; the entry's update listener reloads it.

        Reloading here as well would reload the entry twice.  If the password
        is unchanged no reload is needed – fresh tokens are already on disk
        and every push re-reads them.
        """
        entry = self._reauth_entry
        if entry is None:
            return self.async_abort(reason="unknown")
        updated = {**entry.data, CONF_GARMIN_PASSWORD: self._stored_input[CONF_GARMIN_PASSWORD]}
        self.hass.config_entries.async_update_entry(entry, data=updated)
        return self.async_abort(reason="reauth_successful")

    # ── Reauth flow ───────────────────────────────────────────────────────────

    async def async_step_reauth(
        self, entry_data: dict[str, Any] | None = None
    ) -> FlowResult:
        """Called by HA when async_start_reauth() is triggered."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        if self._reauth_entry is None:
            return self.async_abort(reason="unknown")
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask the user to re-enter their Garmin password (and handle 2FA)."""
        errors: dict[str, str] = {}
        entry = self._reauth_entry
        if entry is None:
            return self.async_abort(reason="unknown")

        if user_input is not None:
            self._stored_input = {
                **entry.data,
                CONF_GARMIN_PASSWORD: user_input[CONF_GARMIN_PASSWORD],
            }
            result = await self._async_start_login()
            if result == _LOGIN_OK:
                return self._finish_reauth()
            if result == _LOGIN_MFA:
                return await self.async_step_mfa()
            errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_reauth_schema(),
            errors=errors,
            description_placeholders={
                "email": entry.data.get(CONF_GARMIN_EMAIL, "")
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
            if not _has_any_source(user_input):
                errors["base"] = "no_sensors"
            else:
                # Write every key explicitly (None / False when cleared):
                # options are merged over entry.data, so an omitted key would
                # let the value from initial setup silently come back.
                out: dict[str, Any] = {
                    CONF_WATER_SENSOR: user_input.get(CONF_WATER_SENSOR) or None,
                    CONF_WEIGHT_SENSOR: user_input.get(CONF_WEIGHT_SENSOR) or None,
                    CONF_ABWHEEL_SYNC: bool(user_input.get(CONF_ABWHEEL_SYNC, False)),
                    CONF_JUMPROPE_SYNC: bool(user_input.get(CONF_JUMPROPE_SYNC, False)),
                }
                return self.async_create_entry(title="", data=out)
            current = {**current, **user_input}

        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(current),
            errors=errors,
        )
