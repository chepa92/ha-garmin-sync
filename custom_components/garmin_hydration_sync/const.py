"""Constants for the Garmin Hydration Sync integration."""
import logging

LOGGER = logging.getLogger("custom_components.garmin_hydration_sync")

DOMAIN = "garmin_hydration_sync"

# ── Config entry keys ────────────────────────────────────────────────────────
CONF_GARMIN_EMAIL    = "garmin_email"
CONF_GARMIN_PASSWORD = "garmin_password"
# HA entity_id of the sensor reporting today's water intake in mL
CONF_WATER_SENSOR    = "water_sensor_entity"
# HA entity_id of the sensor reporting body weight in kg (optional)
CONF_WEIGHT_SENSOR   = "weight_sensor_entity"
# Sync Ab Wheel workouts to Garmin (listens for xiaomi_abwheel HA bus events)
CONF_ABWHEEL_SYNC    = "abwheel_sync"
# Sync Jump Rope sessions to Garmin (listens for xiaomi_jump_rope session events)
CONF_JUMPROPE_SYNC   = "jumprope_sync"

# ── Services ─────────────────────────────────────────────────────────────────
SERVICE_SYNC_NOW = "sync_now"

# ── Storage ───────────────────────────────────────────────────────────────────
TOKEN_SUBDIR = ".garmin_sync_tokens"

# ── Coordinator data keys ────────────────────────────────────────────────────
KEY_LAST_SYNC_TIME       = "last_sync_time"
KEY_LAST_SYNC_STATUS     = "last_sync_status"
KEY_LAST_SYNC_ML         = "last_sync_ml"
KEY_LAST_SYNC_WEIGHT_KG  = "last_sync_weight_kg"
KEY_LAST_SYNC_DAYS       = "last_sync_days"
KEY_LAST_SYNC_FAILED     = "last_sync_failed"
KEY_LAST_SYNC_ABWHEEL    = "last_sync_abwheel"
KEY_LAST_SYNC_JUMPROPE   = "last_sync_jumprope"

STATUS_OK    = "ok"
STATUS_ERROR = "error"
STATUS_IDLE  = "never"
