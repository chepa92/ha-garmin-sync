"""Garmin Connect hydration upload helper.

Uses the ``garminconnect`` library (cyberjunky/python-garminconnect) which
implements the current Garmin mobile SSO auth flow.

2FA / MFA notes
───────────────
Garmin may require a one-time code on first login.  The caller supplies an
optional ``prompt_mfa`` callable; during the Config Flow the flow itself
provides this callback (blocking on a threading.Queue until the user submits
the MFA step).  During normal hourly syncs the tokens are already cached so
no MFA is expected.

Token cache location:
  <HA config>/.garmin_hydration_sync_tokens/garmin_tokens.json

Tokens auto-refresh indefinitely – 2FA is NOT required again as long as the
refresh token stays valid (typically until you log out from all devices or
Garmin invalidates it server-side).

Limitations:
  • The upload uses add_hydration_data() with a delta (current→target), so
  • existing manual entries in the Garmin app are preserved and not doubled.
"""
from __future__ import annotations

import os
from typing import Any, Callable

from homeassistant.core import HomeAssistant

from .const import LOGGER, TOKEN_SUBDIR


class GarminAuthExpiredError(Exception):
    """Raised when the cached Garmin token has expired and re-authentication is required.

    The integration catches this and initiates a reauth config-flow so the user
    can enter their password (and 2FA code if needed) again.
    """


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

    Args:
        hass:       HomeAssistant instance (provides config path + executor).
        email:      Garmin Connect account e-mail.
        password:   Garmin Connect account password.
        delta_ml:   Millilitres to add to Garmin's daily total for *date_str*.
        date_str:   Target date in ``YYYY-MM-DD`` format.
        prompt_mfa: Optional callable for 2FA.  Tokens are cached after setup
                    so this is ``None`` during normal hourly syncs.

    Returns:
        Response dict from the Garmin API.

    Raises:
        RuntimeError: on missing library, auth failure, or API error.
    """
    token_dir = hass.config.path(TOKEN_SUBDIR)
    return await hass.async_add_executor_job(
        _upload_blocking, token_dir, email, password, delta_ml, date_str, prompt_mfa
    )


async def async_login_only(
    hass: HomeAssistant,
    email: str,
    password: str,
    prompt_mfa: Callable[[], str] | None = None,
) -> None:
    """Authenticate with Garmin Connect and cache tokens (async).

    Used by the Config Flow to perform the initial login (and handle 2FA)
    before any data upload is needed.
    """
    token_dir = hass.config.path(TOKEN_SUBDIR)
    await hass.async_add_executor_job(
        _login_blocking, token_dir, email, password, prompt_mfa
    )


def _build_client_and_login(
    token_dir: str,
    email: str,
    password: str,
    prompt_mfa: Callable[[], str] | None,
):
    """Create a Garmin client and authenticate.

    Background mode (``prompt_mfa is None``)
    ─────────────────────────────────────────
    Calls ``client.login(token_file)`` which first tries ``client.load()`` to
    restore the cached token, auto-refreshes via the DI refresh token if almost
    expired, and only falls back to full SSO login if the cache is unusable.

    If the full SSO login reaches the MFA-code step (i.e. the refresh token has
    expired and a new session is required), our ``_background_mfa`` callback
    raises ``GarminAuthExpiredError`` instead of blocking.  The coordinator
    catches it and calls ``entry.async_start_reauth()`` so the user sees a
    notification in the HA UI.

    Important: even when Garmin sends an MFA email while re-attempting SSO, the
    user is now also prompted via the HA reauth flow, so the email is expected.

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
    token_file = os.path.join(token_dir, "garmin_tokens.json")

    if prompt_mfa is None:
        # Background mode — track whether garminconnect called our mfa callback
        # (which means the cached token is gone and a full re-login is needed).
        # We cannot raise GarminAuthExpiredError directly from inside the callback
        # because garminconnect's outer except-handler wraps all unknown exceptions
        # into GarminConnectConnectionError.  Instead we use a flag and re-raise
        # the right type AFTER client.login() returns.
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


def _login_blocking(
    token_dir: str,
    email: str,
    password: str,
    prompt_mfa: Callable[[], str] | None,
) -> None:
    """Blocking login – runs in the executor thread pool."""
    _build_client_and_login(token_dir, email, password, prompt_mfa)


def _upload_blocking(
    token_dir: str,
    email: str,
    password: str,
    delta_ml: float,
    date_str: str,
    prompt_mfa: Callable[[], str] | None,
) -> dict[str, Any]:
    """Blocking upload – runs in the executor thread pool."""
    client = _build_client_and_login(token_dir, email, password, prompt_mfa)

    LOGGER.debug("[GarminSync] %s: adding +%g mL to Garmin", date_str, delta_ml)
    result = client.add_hydration_data(delta_ml, cdate=date_str)
    LOGGER.debug("[GarminSync] Upload result for %s: %s", date_str, result)

    if isinstance(result, dict):
        return result
    return {"status": str(result) if result else "ok"}


# ── Weight upload ─────────────────────────────────────────────────────────────

async def async_upload_weight(
    hass: HomeAssistant,
    email: str,
    password: str,
    weight_kg: float,
    prompt_mfa: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Upload a body weight reading to Garmin Connect (async).

    Args:
        hass:       HomeAssistant instance.
        email:      Garmin Connect account e-mail.
        password:   Garmin Connect account password.
        weight_kg:  Body weight in kilograms.
        prompt_mfa: Optional 2FA callback (not needed after initial login).

    Returns:
        Response dict from the Garmin API.
    """
    token_dir = hass.config.path(TOKEN_SUBDIR)
    return await hass.async_add_executor_job(
        _upload_weight_blocking, token_dir, email, password, weight_kg, prompt_mfa
    )


def _upload_weight_blocking(
    token_dir: str,
    email: str,
    password: str,
    weight_kg: float,
    prompt_mfa: Callable[[], str] | None,
) -> dict[str, Any]:
    """Blocking weight upload – runs in the executor thread pool."""
    client = _build_client_and_login(token_dir, email, password, prompt_mfa)

    LOGGER.debug("[GarminSync] Uploading weight: %.2f kg", weight_kg)
    result = client.add_weigh_in(weight_kg, unitKey="kg")
    LOGGER.debug("[GarminSync] Weight upload result: %s", result)

    if isinstance(result, dict):
        return result
    return {"status": str(result) if result else "ok"}


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
    return await hass.async_add_executor_job(
        _upload_abwheel_blocking, token_dir, email, password,
        reps, calories, duration_sec, start_time, prompt_mfa,
    )


def _upload_abwheel_blocking(
    token_dir: str,
    email: str,
    password: str,
    reps: int,
    calories: int,
    duration_sec: int,
    start_time: str,
    prompt_mfa: Callable[[], str] | None,
) -> dict[str, Any]:
    """Blocking Ab Wheel workout upload – runs in the executor thread pool."""
    from datetime import datetime as _dt

    client = _build_client_and_login(token_dir, email, password, prompt_mfa)

    # Parse start_time (could be ISO string from HA timestamp sensor or unix ts)
    try:
        if start_time.replace(".", "").replace("-", "").isdigit():
            start_dt = _dt.fromtimestamp(int(float(start_time)))
        else:
            start_dt = _dt.fromisoformat(start_time)
    except (ValueError, OSError):
        start_dt = _dt.now()

    activity_name = f"Ab Wheel – {reps} reps"
    duration_min = max(int(duration_sec // 60), 1)

    LOGGER.info(
        "[GarminSync] Uploading Ab Wheel workout: %d reps, %d cal, %ds at %s",
        reps, calories, duration_sec, start_dt.strftime("%Y-%m-%d %H:%M"),
    )

    result = client.create_manual_activity(
        activity_name=activity_name,
        start_datetime=start_dt.strftime("%Y-%m-%dT%H:%M:%S.000"),
        time_zone="Europe/Moscow",
        type_key="strength_training",
        distance_km=0,
        duration_min=duration_min,
    )

    LOGGER.debug("[GarminSync] Ab Wheel upload result: %s", result)

    if isinstance(result, dict):
        return result
    return {"status": str(result) if result else "ok"}
