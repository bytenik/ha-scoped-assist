"""Exercise the installed integration against the supported Home Assistant."""

import importlib.util
from pathlib import Path
import sys
import unittest

# Home Assistant installs its validation compatibility layer during startup.
# Import it first, as the integration loader does in a running instance.
import homeassistant  # noqa: F401
import voluptuous as vol


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "custom_components.scoped_assist",
    ROOT / "__init__.py",
    submodule_search_locations=[str(ROOT)],
)
INTEGRATION = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = INTEGRATION
SPEC.loader.exec_module(INTEGRATION)


class ConfigurationTest(unittest.TestCase):
    """Catch import failures and invalid or incorrectly defaulted YAML options."""

    def test_defaults(self):
        config = INTEGRATION.CONFIG_SCHEMA({"scoped_assist": {}})
        self.assertEqual(
            config["scoped_assist"],
            {
                "earshot_label_prefix": "earshot",
                "ambient_label_prefix": "ambient",
                "actionable_only": True,
                "max_matches": 20,
                "confirm_over": 6,
            },
        )

    def test_custom_options_and_other_integrations(self):
        options = {
            "earshot_label_prefix": "nearby",
            "ambient_label_prefix": "everywhere",
            "actionable_only": False,
            "max_matches": 5,
            "confirm_over": 0,
        }
        config = INTEGRATION.CONFIG_SCHEMA(
            {"scoped_assist": options, "logger": {"default": "warning"}}
        )
        self.assertEqual(config["scoped_assist"], options)
        self.assertEqual(config["logger"], {"default": "warning"})

    def test_invalid_options(self):
        for option, value in (
            ("earshot_label_prefix", []),
            ("ambient_label_prefix", []),
            ("max_matches", -1),
            ("confirm_over", -1),
            ("unknown_option", True),
        ):
            with self.subTest(option=option), self.assertRaises(vol.Invalid):
                INTEGRATION.CONFIG_SCHEMA({"scoped_assist": {option: value}})
