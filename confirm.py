"""Hold back an action that would sweep up more of the house than expected."""

from __future__ import annotations

import json
import logging
from typing import Any, override

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import intent, llm
from homeassistant.util.json import JsonObjectType

_LOGGER = logging.getLogger(__name__)

# Slots through which a single call can fan out across the house. A tool with
# none of them acts on one fixed thing and needs no counting.
_TARGETING_SLOTS = frozenset({"name", "area", "floor", "domain", "device_class"})


def _slot_names(tool: llm.Tool) -> set[str]:
    """The argument names a tool accepts."""
    schema = getattr(tool.parameters, "schema", None)
    if not isinstance(schema, dict):
        return set()
    return {str(getattr(key, "schema", key)) for key in schema}


def _as_list(value: Any) -> list[str] | None:
    """Slots arrive as either a bare string or a list."""
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    return list(value)


class ConfirmingTool(llm.Tool):
    """Wraps an intent tool, counting its targets before letting it run.

    Voice makes broad commands cheap to say and expensive to get wrong: "turn
    off the lights" with no area is one syllable away from "turn off the
    kitchen lights", and the difference is the whole house going dark. The
    count is taken here rather than asked of the model, which cannot know that
    an area holds fourteen bulbs until it has already switched them.
    """

    def __init__(self, tool: llm.Tool, threshold: int, asked: set[str]) -> None:
        """Wrap `tool`, sharing the per-turn record of what has been asked."""
        self._tool = tool
        self._threshold = threshold
        self._asked = asked

        self.name = tool.name
        self.description = tool.description
        schema = dict(getattr(tool.parameters, "schema", None) or {})
        schema[vol.Optional("confirm")] = bool
        self.parameters = vol.Schema(schema)

    @override
    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Run the tool, unless it would affect too much without agreement."""
        args = dict(tool_input.tool_args)
        confirmed = bool(args.pop("confirm", False))
        targets = self._targets(hass, args, llm_context)

        if len(targets) > self._threshold:
            signature = json.dumps(args, sort_keys=True, default=str)
            # A confirmation that arrives in the same turn as the question is
            # the model answering itself. Real agreement comes back as a new
            # turn, which builds a new API instance and an empty `asked`.
            if not confirmed or signature in self._asked:
                self._asked.add(signature)
                _LOGGER.debug(
                    "held back | %s would affect %d devices (limit %d)%s",
                    self.name,
                    len(targets),
                    self._threshold,
                    ", model self-confirmed in the same turn" if confirmed else "",
                )
                return {
                    "executed": False,
                    "needs_confirmation": True,
                    "affected_devices": len(targets),
                    "devices": sorted(targets)[:12],
                    "note": (
                        f"NOT DONE. This would affect {len(targets)} devices, "
                        f"more than the {self._threshold} that may be changed "
                        "without asking. Tell the user what it would affect and "
                        "ask them to confirm. Do not call this tool again in "
                        "this reply. If they agree, call it again with "
                        "confirm=true."
                    ),
                }

        return await self._tool.async_call(
            hass,
            llm.ToolInput(
                tool_name=self._tool.name,
                tool_args=args,
                id=tool_input.id,
                external=tool_input.external,
            ),
            llm_context,
        )

    def _targets(
        self, hass: HomeAssistant, args: dict[str, Any], llm_context: llm.LLMContext
    ) -> set[str]:
        """Names of the distinct devices this call would act on.

        Counted per device rather than per entity: one ceiling fan exposing a
        fan and two lights is one thing the user would recognise being told
        about, not three.
        """
        result = intent.async_match_targets(
            hass,
            intent.MatchTargetsConstraints(
                name=args.get("name"),
                area_name=args.get("area"),
                floor_name=args.get("floor"),
                domains=_as_list(args.get("domain")),
                device_classes=_as_list(args.get("device_class")),
                assistant=llm_context.assistant,
                allow_duplicate_names=True,
            ),
        )
        if not result.is_match:
            return set()

        entity_reg = er.async_get(hass)
        by_device: dict[str, str] = {}
        for state in result.states:
            entry = entity_reg.async_get(state.entity_id)
            key = entry.device_id if entry and entry.device_id else state.entity_id
            by_device.setdefault(key, state.name)
        return set(by_device.values())


def async_wrap_tools(tools: list[llm.Tool], threshold: int) -> list[llm.Tool]:
    """Put a confirmation gate in front of every tool that can fan out.

    The `asked` set is created here, so it lives exactly as long as the list of
    tools does -- one conversation turn.
    """
    if threshold <= 0:
        return tools
    asked: set[str] = set()
    return [
        ConfirmingTool(tool, threshold, asked)
        if isinstance(tool, llm.IntentTool) and _TARGETING_SLOTS & _slot_names(tool)
        else tool
        for tool in tools
    ]
