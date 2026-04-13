# Garmin Sync — Home Assistant Custom Integration

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![HA min version](https://img.shields.io/badge/Home%20Assistant-2024.11%2B-blue)](https://www.home-assistant.io)

Automatically pushes **water intake** and **body weight** readings to [Garmin Connect](https://connect.garmin.com) whenever your sensor values change in Home Assistant.  
No schedules, no polling — syncs are triggered in real time by sensor state-change events.

---

## Features

- **Hydration sync** — tracks daily mL totals and sends only the increment since the last push, preserving any manual entries in the Garmin app
- **Weight sync** — each new weigh-in is uploaded as an independent entry to Garmin's scale history
- **Event-driven** — syncs fire immediately when a sensor changes, not on a timer
- **Graceful handling of unavailable sensors** — unknown / unavailable states are silently skipped
- **Token caching** — authenticates once; subsequent syncs restore the cached OAuth token with automatic refresh (no MFA re-prompts during normal use)
- **Re-auth UI** — if the Garmin session expires, Home Assistant shows a notification prompting you to re-authenticate

---

## Requirements

- Home Assistant **2024.11** or newer
- A [Garmin Connect](https://connect.garmin.com) account
- A sensor that reports today's cumulative water intake in **millilitres** (e.g. the [Water.io BLE integration](https://github.com/chepa92/waterio-ha))
- _(Optional)_ A sensor that reports body weight in **kg**

---

## Installation

### Via HACS (recommended)

1. In HACS → **Integrations**, click the three-dot menu → **Custom repositories**
2. Add `https://github.com/chepa92/ha-garmin-sync` as an **Integration**
3. Search for **Garmin Sync** and install
4. Restart Home Assistant

### Manual

Copy the `custom_components/garmin_hydration_sync/` folder into your HA `config/custom_components/` directory and restart Home Assistant.

---

## Setup

1. Go to **Settings → Devices & Services → Add Integration** and search for **Garmin Sync**
2. Enter your Garmin Connect e-mail and password
3. If two-factor authentication is required, check your e-mail and enter the code in the next step
4. Select your water intake sensor (required) and body weight sensor (optional)

---

## Entities

| Entity | Type | Description |
|--------|------|-------------|
| `sensor.garmin_sync_status` | Sensor | Last sync status (`ok` / `error` / `never`) |
| `sensor.garmin_last_synced_water` | Sensor | Last synced daily water total (mL) |
| `sensor.garmin_last_synced_weight` | Sensor | Last synced body weight (kg) |
| `button.garmin_sync_to_garmin` | Button | Trigger an immediate manual sync |

---

## Service

### `garmin_hydration_sync.sync_now`

Immediately pushes current sensor values to Garmin Connect.

```yaml
service: garmin_hydration_sync.sync_now
# Optional — target a specific account when multiple are configured:
data:
  entry_id: abc123
```

---

## How hydration delta-tracking works

The integration stores `{ "YYYY-MM-DD": mL_sent }` in persistent HA storage.  
When the sensor fires a new value, the integration computes:

```
delta = current_mL - already_sent_today
```

Only the delta is sent to Garmin — so if you manually log water in the Garmin app, those entries are preserved and not duplicated.  
The counter resets at midnight when your sensor resets to 0.

---

## Token management

- Tokens are stored in `<HA config>/.garmin_sync_tokens/garmin_tokens.json`
- Background syncs use `client.login(token_file)` which loads the cached token and auto-refreshes it if it's about to expire — no credentials are submitted to Garmin
- If the refresh token expires, the integration raises a re-auth notification in HA so you can re-enter your password

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `429 Too Many Requests` | Garmin rate-limits logins per IP | Wait a few minutes; do not restart HA repeatedly |
| Re-auth notification after a few months | Garmin refresh token expired | Click the notification and enter your password |
| Weight not syncing | Sensor value outside 20–300 kg range | Check sensor unit (must be kg) |
| Water not syncing | Sensor value 0 or unavailable | Sensor must report a positive cumulative mL value |
