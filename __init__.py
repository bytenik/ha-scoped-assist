"""Scoped Assist: a room-aware Assist API for voice satellites."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import llm
from homeassistant.helpers.typing import ConfigType

from .api import ScopedAssistAPI
from .const import (
    CONF_ACTIONABLE_ONLY,
    CONF_AMBIENT_PREFIX,
    CONF_CONFIRM_OVER,
    CONF_EARSHOT_PREFIX,
    CONF_MAX_MATCHES,
    DEFAULT_ACTIONABLE_ONLY,
    DEFAULT_AMBIENT_PREFIX,
    DEFAULT_CONFIRM_OVER,
    DEFAULT_EARSHOT_PREFIX,
    DEFAULT_MAX_MATCHES,
    DOMAIN,
)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional(
                    CONF_EARSHOT_PREFIX, default=DEFAULT_EARSHOT_PREFIX
                ): cv.string,
                vol.Optional(
                    CONF_AMBIENT_PREFIX,
    CONF_CONFIRM_OVER, default=DEFAULT_AMBIENT_PREFIX
                ): cv.string,
                vol.Optional(
                    CONF_ACTIONABLE_ONLY, default=DEFAULT_ACTIONABLE_ONLY
                ): cv.boolean,
                vol.Optional(
                    CONF_MAX_MATCHES, default=DEFAULT_MAX_MATCHES
                ): cv.positive_int,
                vol.Optional(
                    CONF_CONFIRM_OVER, default=DEFAULT_CONFIRM_OVER
                ): vol.All(int, vol.Range(min=0)),
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the scoped API so it can be picked per conversation agent."""
    conf = config.get(DOMAIN) or {}

    llm.async_register_api(
        hass,
        ScopedAssistAPI(
            hass,
            earshot_prefix=conf.get(CONF_EARSHOT_PREFIX, DEFAULT_EARSHOT_PREFIX),
            ambient_prefix=conf.get(CONF_AMBIENT_PREFIX, DEFAULT_AMBIENT_PREFIX),
            actionable_only=conf.get(CONF_ACTIONABLE_ONLY, DEFAULT_ACTIONABLE_ONLY),
            max_matches=conf.get(CONF_MAX_MATCHES, DEFAULT_MAX_MATCHES),
            confirm_over=conf.get(CONF_CONFIRM_OVER, DEFAULT_CONFIRM_OVER),
        ),
    )
    return True
