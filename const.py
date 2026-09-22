"""Constants for Scoped Assist."""

DOMAIN = "scoped_assist"

API_ID = "scoped_assist"
API_NAME = "Assist (room-scoped)"

CONF_EARSHOT_PREFIX = "earshot_label_prefix"
CONF_AMBIENT_PREFIX = "ambient_label_prefix"
CONF_ACTIONABLE_ONLY = "actionable_only"
CONF_MAX_MATCHES = "max_matches"
CONF_CONFIRM_OVER = "confirm_over"

DEFAULT_EARSHOT_PREFIX = "earshot"
DEFAULT_AMBIENT_PREFIX = "ambient"
DEFAULT_ACTIONABLE_ONLY = True
DEFAULT_MAX_MATCHES = 20
# Above this many distinct devices, an action is described and confirmed
# before it runs. 0 disables the gate.
DEFAULT_CONFIRM_OVER = 6

# Domains a voice command can plausibly act on. Sensors and diagnostics are
# omitted from the nearby list: state questions go through GetLiveContext,
# which reads them on demand rather than carrying them in every prompt.
ACTIONABLE_DOMAINS = frozenset(
    {
        "climate",
        "cover",
        "fan",
        "humidifier",
        "input_boolean",
        "light",
        "lock",
        "media_player",
        "scene",
        "script",
        "switch",
        "vacuum",
        "valve",
        "water_heater",
    }
)
