"""Garmin Connect upload helpers.

Uses the ``garminconnect`` library (cyberjunky/python-garminconnect) which
implements the current Garmin mobile SSO auth flow.

2FA / MFA notes
───────────────
Garmin may require a one-time code on first login.  The caller supplies an
optional ``prompt_mfa`` callable; during the Config Flow the flow itself
provides this callback (blocking on a threading.Queue until the user submits
the MFA step).  During normal event-driven syncs the tokens are already
cached so no MFA is expected.

Token cache location (one file per Garmin account):
  <HA config>/.garmin_sync_tokens/garmin_tokens_<email>.json

Tokens auto-refresh indefinitely – 2FA is NOT required again as long as the
refresh token stays valid (typically until you log out from all devices or
Garmin invalidates it server-side).

Hydration uses add_hydration_data() with a delta, so existing manual entries
in the Garmin app are preserved and not doubled.
"""
from __future__ import annotations

import os
import re
import threading
from datetime import datetime, timezone, tzinfo
from typing import Any, Callable
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant

from .const import LOGGER, TOKEN_SUBDIR

# Legacy single token file shared by all accounts (pre-1.2.1)
_LEGACY_TOKEN_FILE = "garmin_tokens.json"

# Serialises background logins/uploads per token file so two concurrent
# executor jobs never refresh / rewrite the same token file at once.
_TOKEN_LOCKS: dict[str, threading.Lock] = {}
_TOKEN_LOCKS_GUARD = threading.Lock()


class GarminAuthExpiredError(Exception):
    """Raised when the cached Garmin token has expired and re-authentication is required.

    The integration catches this and initiates a reauth config-flow so the user
    can enter their password (and 2FA code if needed) again.
    """


# ── Helpers ──────────────────────────────────────────────────────────────────

def _token_file(token_dir: str, email: str) -> str:
    """Return the per-account token file path."""
    slug = re.sub(r"[^a-z0-9]+", "_", email.strip().lower()).strip("_") or "default"
    return os.path.join(token_dir, f"garmin_tokens_{slug}.json")


def _token_lock(token_file: str) -> threading.Lock:
    with _TOKEN_LOCKS_GUARD:
        return _TOKEN_LOCKS.setdefault(token_file, threading.Lock())


def _migrate_legacy_token(token_dir: str, token_file: str) -> None:
    """Move the old shared token file to the per-account location (once).

    The file is *moved*, not copied, so at most one account can inherit it.
    """
    legacy = os.path.join(token_dir, _LEGACY_TOKEN_FILE)
    if not os.path.exists(token_file) and os.path.isfile(legacy):
        try:
            os.replace(legacy, token_file)
            LOGGER.info("[GarminSync] Migrated legacy token cache to %s", token_file)
        except OSError as exc:
            LOGGER.debug("[GarminSync] Could not migrate legacy token file: %s", exc)


def parse_workout_start(start_time: str, tz: tzinfo) -> datetime | None:
    """Parse a workout start time (unix timestamp or ISO string) into ``tz``.

    Naive ISO strings are interpreted as local time in ``tz``.  Returns None
    if the value can't be parsed.
    """
    value = (start_time or "").strip()
    if not value:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).astimezone(tz)
    except (ValueError, OverflowError, OSError):
        pass
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def _build_client_and_login(
    token_dir: str,
    email: str,
    password: str,
    prompt_mfa: Callable[[], str] | None,
):
    """Create a Garmin client and authenticate.

    Background mode (``prompt_mfa is None``)
    ─────────────────────────────────────────
    Calls ``client.login(token_file)`` which first tries to restore the cached
    token, auto-refreshes via the DI refresh token if almost expired, and only
    falls back to full SSO login if the cache is unusable.

    If the full SSO login reaches the MFA-code step (i.e. the refresh token has
    expired and a new session is required), our ``_background_mfa`` callback
    records it and aborts; we then raise ``GarminAuthExpiredError``.  The
    coordinator catches it and calls ``entry.async_start_reauth()`` so the user
    sees a notification in the HA UI.

    Interactive mode (``prompt_mfa`` provided)
    ──────────────────────────────────────────
    Passes the caller's ``prompt_mfa`` directly — used by the config/reauth
    flow to interactively handle the 2FA step.
    """
    try:
        from garminconnect import Garmin
    except ImportError as exc:
        raise RuntimeError(
            "The 'garminconnect' library is required. "
            "Make sure 'garminconnect>=0.3.0' is in manifest.json requirements "
            "and that Home Assistant has restarted to install it."
        ) from exc

    os.makedirs(token_dir, exist_ok=True)
    token_file = _token_file(token_dir, email)
    _migrate_legacy_token(token_dir, token_file)

    if prompt_mfa is None:
        # We cannot raise GarminAuthExpiredError directly from inside the
        # callback because garminconnect wraps unknown exceptions into
        # GarminConnectConnectionError.  Use a flag and re-raise the right
        # type AFTER client.login() returns.
        mfa_triggered: list[bool] = []

        def _background_mfa() -> str:
            mfa_triggered.append(True)
            raise RuntimeError("mfa_required_in_background")

        client = Garmin(email, password, prompt_mfa=_background_mfa)
        try:
            client.login(token_file)
        except Exception as exc:
            if mfa_triggered:
                raise GarminAuthExpiredError(
                    "Garmin session expired – re-authentication is required"
                ) from exc
            # Rate-limit, network error, etc — re-raise so the push handler
            # logs it as a transient error without triggering the reauth flow.
            raise
        return client

    # Interactive mode (config flow / reauth flow)
    client = Garmin(email, password, prompt_mfa=prompt_mfa)
    client.login(token_file)
    return client


def _run_with_client(
    token_dir: str,
    email: str,
    password: str,
    prompt_mfa: Callable[[], str] | None,
    action: Callable[[Any], Any],
) -> dict[str, Any]:
    """Log in and run ``action(client)``, serialised per account."""
    with _token_lock(_token_file(token_dir, email)):
        client = _build_client_and_login(token_dir, email, password, prompt_mfa)
        result = action(client)
    if isinstance(result, dict):
        return result
    return {"status": str(result) if result else "ok"}


# ── Login (config flow) ──────────────────────────────────────────────────────

async def async_login_only(
    hass: HomeAssistant,
    email: str,
    password: str,
    prompt_mfa: Callable[[], str] | None = None,
) -> None:
    """Authenticate with Garmin Connect and cache tokens (async).

    Used by the Config Flow to perform the initial login (and handle 2FA)
    before any data upload is needed.  Not serialised with the upload lock,
    because it may block for minutes waiting on the user's 2FA code.
    """
    token_dir = hass.config.path(TOKEN_SUBDIR)
    await hass.async_add_executor_job(
        _build_client_and_login, token_dir, email, password, prompt_mfa
    )


# ── Hydration upload ─────────────────────────────────────────────────────────

async def async_upload_hydration(
    hass: HomeAssistant,
    email: str,
    password: str,
    delta_ml: float,
    date_str: str,
    prompt_mfa: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Add ``delta_ml`` millilitres to Garmin Connect for ``date_str``.

    ``delta_ml`` is the *increment* to push (positive only – callers must
    ensure it is > 0 before calling this function).
    """
    token_dir = hass.config.path(TOKEN_SUBDIR)

    def _action(client: Any) -> Any:
        LOGGER.debug("[GarminSync] %s: adding +%g mL to Garmin", date_str, delta_ml)
        result = client.add_hydration_data(delta_ml, cdate=date_str)
        LOGGER.debug("[GarminSync] Upload result for %s: %s", date_str, result)
        return result

    return await hass.async_add_executor_job(
        _run_with_client, token_dir, email, password, prompt_mfa, _action
    )


# ── Weight upload ─────────────────────────────────────────────────────────────

async def async_upload_weight(
    hass: HomeAssistant,
    email: str,
    password: str,
    weight_kg: float,
    prompt_mfa: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Upload a body weight reading (kg) to Garmin Connect."""
    token_dir = hass.config.path(TOKEN_SUBDIR)

    def _action(client: Any) -> Any:
        LOGGER.debug("[GarminSync] Uploading weight: %.2f kg", weight_kg)
        result = client.add_weigh_in(weight_kg, unitKey="kg")
        LOGGER.debug("[GarminSync] Weight upload result: %s", result)
        return result

    return await hass.async_add_executor_job(
        _run_with_client, token_dir, email, password, prompt_mfa, _action
    )


# ── Ab Wheel workout upload ──────────────────────────────────────────────────

async def async_upload_abwheel_workout(
    hass: HomeAssistant,
    email: str,
    password: str,
    reps: int,
    calories: int,
    duration_sec: int,
    start_time: str,
    prompt_mfa: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Create a strength_training activity on Garmin Connect for an Ab Wheel workout."""
    token_dir = hass.config.path(TOKEN_SUBDIR)
    tz_name: str = hass.config.time_zone or "UTC"
    tz = await hass.async_add_executor_job(ZoneInfo, tz_name)

    start_dt = parse_workout_start(start_time, tz)
    if start_dt is None:
        LOGGER.warning(
            "[GarminSync] Could not parse Ab Wheel start_time %r – using now", start_time
        )
        start_dt = datetime.now(tz)

    duration = max(int(duration_sec), 60)
    payload: dict[str, Any] = {
        "activityTypeDTO": {"typeKey": "strength_training"},
        "accessControlRuleDTO": {"typeId": 2, "typeKey": "private"},
        "timeZoneUnitDTO": {"unitKey": tz_name},
        "activityName": f"Ab Wheel – {reps} reps",
        "metadataDTO": {"autoCalcCalories": calories <= 0},
        "summaryDTO": {
            "startTimeLocal": start_dt.strftime("%Y-%m-%dT%H:%M:%S.000"),
            "distance": 0,
            "duration": duration,
        },
    }
    if calories > 0:
        payload["summaryDTO"]["calories"] = calories

    def _action(client: Any) -> Any:
        LOGGER.info(
            "[GarminSync] Uploading Ab Wheel workout: %d reps, %d cal, %ds at %s (%s)",
            reps, calories, duration_sec, start_dt.strftime("%Y-%m-%d %H:%M"), tz_name,
        )
        if hasattr(client, "create_manual_activity_from_json"):
            result = client.create_manual_activity_from_json(payload)
        else:  # older garminconnect: minutes only, no calories
            result = client.create_manual_activity(
                start_datetime=payload["summaryDTO"]["startTimeLocal"],
                time_zone=tz_name,
                type_key="strength_training",
                distance_km=0,
                duration_min=max(round(duration / 60), 1),
                activity_name=payload["activityName"],
            )
        LOGGER.debug("[GarminSync] Ab Wheel upload result: %s", result)
        return result

    return await hass.async_add_executor_job(
        _run_with_client, token_dir, email, password, prompt_mfa, _action
    )


# ── Jump Rope workout upload ─────────────────────────────────────────────────

async def async_upload_jumprope_workout(
    hass: HomeAssistant,
    email: str,
    password: str,
    jumps: int,
    calories: int,
    duration_sec: int,
    start_time: str,
    prompt_mfa: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Create a jump_rope activity on Garmin Connect for one finished workout."""
    token_dir = hass.config.path(TOKEN_SUBDIR)
    tz_name: str = hass.config.time_zone or "UTC"
    tz = await hass.async_add_executor_job(ZoneInfo, tz_name)

    start_dt = parse_workout_start(start_time, tz)
    if start_dt is None:
        LOGGER.warning(
            "[GarminSync] Could not parse Jump Rope start_time %r – using now", start_time
        )
        start_dt = datetime.now(tz)

    duration = max(int(duration_sec), 60)
    payload: dict[str, Any] = {
        "activityTypeDTO": {"typeKey": "jump_rope"},
        "accessControlRuleDTO": {"typeId": 2, "typeKey": "private"},
        "timeZoneUnitDTO": {"unitKey": tz_name},
        "activityName": f"Jump Rope – {jumps} jumps",
        "metadataDTO": {"autoCalcCalories": calories <= 0},
        "summaryDTO": {
            "startTimeLocal": start_dt.strftime("%Y-%m-%dT%H:%M:%S.000"),
            "distance": 0,
            "duration": duration,
        },
    }
    if calories > 0:
        payload["summaryDTO"]["calories"] = calories

    def _action(client: Any) -> Any:
        LOGGER.info(
            "[GarminSync] Uploading Jump Rope workout: %d jumps, %d cal, %ds at %s (%s)",
            jumps, calories, duration_sec, start_dt.strftime("%Y-%m-%d %H:%M"), tz_name,
        )
        if hasattr(client, "create_manual_activity_from_json"):
            result = client.create_manual_activity_from_json(payload)
        else:  # older garminconnect: minutes only, no calories
            result = client.create_manual_activity(
                start_datetime=payload["summaryDTO"]["startTimeLocal"],
                time_zone=tz_name,
                type_key="jump_rope",
                distance_km=0,
                duration_min=max(round(duration / 60), 1),
                activity_name=payload["activityName"],
            )
        LOGGER.debug("[GarminSync] Jump Rope upload result: %s", result)
        return result

    return await hass.async_add_executor_job(
        _run_with_client, token_dir, email, password, prompt_mfa, _action
    )
