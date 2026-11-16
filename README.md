# Garmin Sync — Home Assistant Custom Integration

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![HA min version](https://img.shields.io/badge/Home%20Assistant-2024.11%2B-blue)](https://www.home-assistant.io)

Automatically pushes **water intake** and **body weight** readings to [Garmin Connect](https://connect.garmin.com) whenever your sensor values change in Home Assistant.  
No schedules, no polling — syncs are triggered in real time by sensor state-change events.

---

## Features

- **Jump Rope sync** — each finished workout of the [Xiaomi Smart Jump Rope](https://github.com/chepa92/ha-xiaomi-jump-rope) becomes one Garmin *Jump Rope* activity (start time, duration, calories, jump count in the title); duplicates are skipped by start time

- **Hydration sync** — tracks daily mL totals and sends only the increment since the last push, preserving any manual entries in the Garmin app
- **Weight sync** — each new weigh-in is uploaded as an independent entry to Garmin's scale history
- **Migraine forecast (research)** — a daily score of how far last night's sleep, HRV and stress are from your own normal days, checked against your headache log
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

## Jump Rope

Enable **Sync Jump Rope sessions to Garmin** in the setup form or in the integration's options. It needs the
[ha-xiaomi-jump-rope](https://github.com/chepa92/ha-xiaomi-jump-rope) integration, which fires one
`xiaomi_jump_rope_session_completed` event per workout stored on the rope.

* One Garmin activity (type *Jump Rope*) per workout, never per jump: start time, duration, calories, and the jump count in the title.
* Already-uploaded workouts are remembered by start time, so repeating an upload is harmless.
* To **backfill** workouts already stored on the rope, press **Resend stored workouts** on the jump rope device.

---

## Migraine forecast (research)

Enable **Migraine forecast** in the setup form or the options. Every morning the integration reads last night's
sleep, HRV and resting heart rate plus yesterday's stress and Body Battery from Garmin, and measures how far
they are from **your own normal days**. It is a personal change detector — not a diagnosis, and it never says a
migraine is coming.

How it works:

1. **Data** — about 20 numbers per morning (overnight HRV, resting HR, sleep duration/score/stages, stress and
   breathing during sleep, Body Battery at wake, yesterday's stress, steps and intensity minutes). The first
   run downloads 60 days of history; after that it refreshes every 2 hours.
2. **Headache log** — days you mark **Migraines: yes** in Garmin Connect's Lifestyle Logging are read
   automatically ("no" and unanswered both mean no migraine). Garmin stores no clock time, so a migraine day is
   assumed to start that afternoon; consecutive "yes" days count as one headache. For exact times you can also press **Headache Started** / **Headache Ended**, or call `log_headache` (past headaches
   too). Each morning is then labelled by what happened afterwards: pre-headache (a headache began within 24 h),
   during, after, or normal day (no headache 24 h before or 48 h after).
3. **Baseline** — median and spread of your last 30 normal days (at least 7), using only days that were already
   known to be normal that morning.
4. **Score** — each number becomes a robust z-score; the *deviation index* is their root-mean-square. The
   percentile says "further from your usual than X% of your normal days"; at 90% the status is `elevated`.
5. **Does it work for you?** — once 5 pre-headache and 15 normal mornings have a score, the accuracy sensor
   reports how well the index separates them (AUC; 0.5 = not at all), how many headaches were preceded by an
   `elevated` morning, false alerts per week and the typical lead time.

| Entity | Description |
|--------|-------------|
| `sensor.…_migraine_forecast` | `no_data` / `collecting_baseline` / `usual` / `elevated`; attributes hold the summary, indices and the three measures that moved most |
| `sensor.…_migraine_deviation_index` | Today's deviation index |
| `sensor.…_migraine_deviation_percentile` | Today's percentile against earlier normal days |
| `sensor.…_migraine_forecast_accuracy` | AUC once there is enough data; details as attributes |
| `sensor.…_headache_log` | Number of logged headaches; recent ones (with ids) as an attribute |
| `button.…_headache_started` / `…_headache_ended` | One-tap logging |

Services: `log_headache`, `end_headache`, `delete_headache`, `refresh_migraine_forecast` (`backfill_days` up to 365).

Limits: history downloaded before you logged any headaches is treated as normal days, so log past headaches
if you remember them. Until the accuracy sensor has a value, an `elevated` morning only means "unusual night",
not "headache likely". `features_available` on the forecast sensor shows how many measures your watch provides.

---

## Entities

| Entity | Type | Description |
|--------|------|-------------|
| `sensor.garmin_sync_status` | Sensor | Last sync status (`ok` / `error` / `never`) |
| `sensor.garmin_last_synced_water` | Sensor | Last synced daily water total (mL) |
| `sensor.garmin_last_synced_weight` | Sensor | Last synced body weight (kg) |
| `button.garmin_sync_to_garmin` | Button | Trigger an immediate manual sync |
| `sensor.garmin_last_synced_jump_rope_session` | Sensor | Last Jump Rope workout pushed to Garmin |

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
