"""An Assist API that ships the room a request came from, not the whole house."""

from __future__ import annotations

import json
import logging
from typing import Any, override

from homeassistant.components.homeassistant.llm import (
    DYNAMIC_CONTEXT_PROMPT,
    async_get_exposed_entities,
)
from homeassistant.components.intent.llm import DEVICE_CONTROL_TOOL_USAGE_PROMPT
from homeassistant.components.intent.timers import async_device_supports_timers
from homeassistant.components.llm import async_get_tools
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import llm
from homeassistant.util import yaml as yaml_util

from .confirm import async_wrap_tools
from .const import (
    ACTIONABLE_DOMAINS,
    API_ID,
    API_NAME,
    DEFAULT_CONFIRM_OVER,
    DEFAULT_MAX_MATCHES,
)
from .finder import TOOL_NAME, FindEntitiesTool
from .scope import Scope, async_entity_area_id, async_get_scope, async_is_ambient

_LOGGER = logging.getLogger(__name__)


class _LoggingAPIInstance(llm.APIInstance):
    """An APIInstance that records every tool call and its result.

    Nothing in the stack logs what the model actually did with a prompt: the
    conversation integrations log neither the tools they call nor the replies
    they produce, and the conversation trace lives in memory behind the
    websocket. Without this, a turn where the model searched, found three
    matches and then silently did nothing is indistinguishable from one where
    it never searched at all.
    """

    @override
    async def async_call_tool(self, tool_input: llm.ToolInput) -> Any:
        """Call the tool, recording the arguments and the outcome."""
        _LOGGER.debug(
            "tool call | %s(%s)",
            tool_input.tool_name,
            json.dumps(tool_input.tool_args, default=str),
        )
        try:
            result = await super().async_call_tool(tool_input)
        except Exception as err:
            _LOGGER.debug(
                "tool call | %s RAISED %s: %s",
                tool_input.tool_name,
                type(err).__name__,
                err,
            )
            raise
        rendered = json.dumps(result, default=str)
        if len(rendered) > 800:
            rendered = rendered[:800] + f"... [{len(rendered)} chars total]"
        _LOGGER.debug("tool result | %s -> %s", tool_input.tool_name, rendered)
        return result

def _roomless_dynamic_context() -> str:
    """Stock dynamic-context guidance, minus its appeal to the static list.

    The stock text tells the model to answer "do I have X?" from the static
    context below it. That holds when the list is the whole home. It does not
    hold here, where the list is house-wide devices only and is usually empty,
    and the instruction becomes "answer existence questions from nothing" --
    which reads as "no". Only this sentence is replaced; the rest is upstream's
    and is left alone. The room-scoped prompt keeps the stock text verbatim,
    because that is the wording the 30/30 benchmark measured.
    """
    stock = DYNAMIC_CONTEXT_PROMPT
    old = (
        'If the user asks about device existence/type (e.g., "Do I have lights in '
        'the bedroom?"): Answer\nfrom the static context below.'
    )
    new = (
        'If the user asks about device existence/type (e.g., "Do I have lights in '
        'the bedroom?"): call\n`' + TOOL_NAME + '` to check. The list below is not an '
        "inventory of the home."
    )
    if old not in stock:
        _LOGGER.warning(
            "upstream DYNAMIC_CONTEXT_PROMPT changed; existence guidance left as-is"
        )
        return stock
    return stock.replace(old, new)


class ScopedAssistAPI(llm.API):
    """Assist, with the entity list narrowed to the caller's neighbourhood.

    The stock API sends every exposed entity on every turn. Almost all of it is
    irrelevant to a command spoken in one room, and the bulk actively hurts:
    with hundreds of entities in context a small model picks the wrong
    "Ceiling Fan". This API sends the entities of the originating area and its
    acoustic neighbours, a taxonomy of areas and floors so whole-area and
    whole-floor commands still work, and a search tool for everything else.

    A caller with no room -- the web UI, the companion app -- gets the same
    treatment minus the room: the taxonomy, any house-wide ambient entities,
    and the search tool. It never gets the full dump, which would hand the
    largest prompt in the system to the request that can make least use of it.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        earshot_prefix: str,
        ambient_prefix: str,
        actionable_only: bool,
        max_matches: int = DEFAULT_MAX_MATCHES,
        confirm_over: int = DEFAULT_CONFIRM_OVER,
    ) -> None:
        """Init the class."""
        super().__init__(hass=hass, id=API_ID, name=API_NAME)
        self._earshot_prefix = earshot_prefix
        self._ambient_prefix = ambient_prefix
        self._actionable_only = actionable_only
        self._max_matches = max_matches
        self._confirm_over = confirm_over

    @override
    async def async_get_api_instance(
        self, llm_context: llm.LLMContext
    ) -> llm.APIInstance:
        """Return the instance of the API."""
        # Every built-in LLM tools platform answers only for the built-in api
        # id and returns nothing for any other, so ask under that id to borrow
        # the intent tools and GetLiveContext. Its prompt is discarded: the
        # whole point here is to replace the entity dump it carries.
        stock = await async_get_tools(self.hass, llm_context, llm.LLM_API_ASSIST)
        tools = async_wrap_tools(list(stock.tools), self._confirm_over)

        exposed_entities: dict[str, dict[str, Any]] | None = None
        if llm_context.assistant:
            exposed_entities = async_get_exposed_entities(
                self.hass, llm_context.assistant, include_state=False
            )

        # Nothing exposed at all: there is no list to scope and nothing for the
        # finder to find, so let the stock prompt say so in its own words.
        if not exposed_entities:
            return _LoggingAPIInstance(
                api=self,
                api_prompt=stock.prompt or "",
                llm_context=llm_context,
                tools=tools,
                custom_serializer=llm.selector_serializer,
            )

        scope = async_get_scope(self.hass, llm_context.device_id, self._earshot_prefix)
        if scope.area is None:
            self._log_roomless(llm_context)

        tools.append(FindEntitiesTool(exposed_entities, self._max_matches))

        return _LoggingAPIInstance(
            api=self,
            api_prompt=self._async_get_prompt(llm_context, exposed_entities, scope),
            llm_context=llm_context,
            tools=tools,
            custom_serializer=llm.selector_serializer,
        )

    @callback
    def _async_get_timer_prompt(self, llm_context: llm.LLMContext) -> str | None:
        """Warn when the caller cannot run timers.

        Carried over from the built-in prompt, which this one replaces wholesale.
        """
        if llm_context.device_id and async_device_supports_timers(
            self.hass, llm_context.device_id
        ):
            return None
        return "This device is not able to start timers."

    @callback
    def _log_roomless(self, llm_context: llm.LLMContext) -> None:
        """Say why a request has no room.

        Devices are frequently registered twice -- once by the integration that
        owns the hardware and once by whatever exposes its voice pipeline --
        and only one of the two carries the area. Without this line a satellite
        routed through the area-less twin is indistinguishable from the web UI.
        """
        device_id = llm_context.device_id
        if not device_id:
            reason = "no device_id on the request (UI or non-satellite caller)"
        elif (device := dr.async_get(self.hass).async_get(device_id)) is None:
            reason = f"device_id {device_id} is not in the device registry"
        elif not device.area_id:
            reason = (
                f"device {device.name_by_user or device.name!r} ({device_id}) "
                "has no area assigned"
            )
        else:
            reason = f"area_id {device.area_id} is not in the area registry"
        _LOGGER.debug("no room for this request: %s", reason)

    @callback
    def _async_get_prompt(
        self, llm_context: llm.LLMContext, exposed_entities: dict, scope: Scope
    ) -> str:
        """Assemble the prompt, with or without an originating room."""
        entities = exposed_entities
        list_lines, listed = self._async_get_entity_list(entities, scope)

        parts = [
            DEVICE_CONTROL_TOOL_USAGE_PROMPT,
            DYNAMIC_CONTEXT_PROMPT
            if scope.area
            else _roomless_dynamic_context(),
            "",
            *self._async_get_taxonomy(entities),
            "",
            *(
                self._async_get_room_lines(scope)
                if scope.area
                else self._async_get_roomless_lines()
            ),
            self._async_get_floor_disambiguation(),
            "",
            *list_lines,
            self._async_get_timer_prompt(llm_context),
        ]

        prompt = "\n".join(part for part in parts if part is not None)
        _LOGGER.debug(
            "prompt | room=%s | siblings=%s | listed=%d | chars=%d\n%s",
            scope.area.name if scope.area else "(none)",
            ", ".join(scope.sibling_names) or "none",
            listed,
            len(prompt),
            prompt,
        )
        return prompt

    @callback
    def _async_get_room_lines(self, scope: Scope) -> list[str]:
        """The situational lines for a request spoken in a known room."""
        room = scope.area.name
        siblings = scope.sibling_names
        return [
            f"This request came from the {room}.",
            "A command with no stated location refers to devices in the "
            f"{room}, then to the adjacent areas "
            f"({', '.join(siblings) if siblings else 'none'}).",
            "The device list below covers ONLY those nearby areas. The home has "
            "many more devices elsewhere. Never answer that a device does not "
            "exist merely because it is absent from that list.",
            f"{TOOL_NAME} locates devices that are not in the list below. It is "
            "read-only and cheap: if you are unsure whether a device is nearby, "
            "or where it is, just call it. It returns every match with its area; "
            "act on all of them. You may call it more than once in a turn.",
            "To act on an area or a floor as a whole, pass that area or floor "
            "name to the intent tool.",
        ]

    @callback
    def _async_get_roomless_lines(self) -> list[str]:
        """The situational lines for a request that came from no room.

        The room-scoped wording above resolves an unqualified command to the
        speaker's own area. Here there is no such area, so an unqualified
        command that matches in several places is genuinely ambiguous and the
        model is told to ask rather than guess.
        """
        return [
            "This request did not come from any room in the home, so no "
            "location is implied by where it was spoken.",
            "The device list below is NOT an overview of the home. It covers "
            "only devices that belong to no single room. The home has many more "
            "devices. Never answer that a device does not exist merely because "
            "it is absent from that list.",
            f"{TOOL_NAME} locates every other device in the home. It is "
            "read-only and cheap: call it for anything the user names that is "
            "not in the list below. It returns every match with its area; act "
            "on all of them. You may call it more than once in a turn.",
            f"If the user names a device and {TOOL_NAME} returns matches in "
            "several areas, ask which area they mean rather than guessing.",
            "To act on an area or a floor as a whole, pass that area or floor "
            "name to the intent tool.",
        ]

    @callback
    def _async_get_taxonomy(self, entities: dict[str, dict[str, Any]]) -> list[str]:
        """Name every area and floor, so off-scope targets are still addressable.

        The entity list is deliberately partial, so the model needs some other
        way to know that a Library exists. Names alone are cheap -- the whole
        block costs a few dozen tokens against the hundreds a full entity dump
        would.
        """
        area_reg = ar.async_get(self.hass)

        populated: set[str] = set()
        for entity_id in entities:
            if area_id := async_entity_area_id(self.hass, entity_id):
                populated.add(area_id)

        floors = self._async_sorted_floors()
        area_names = sorted(
            area.name
            for area_id in populated
            if (area := area_reg.async_get_area(area_id)) is not None
        )

        by_floor = []
        for floor in floors:
            names = sorted(
                area.name
                for area_id in populated
                if (area := area_reg.async_get_area(area_id)) is not None
                and area.floor_id == floor.floor_id
            )
            if names:
                by_floor.append(f"{floor.name}: {', '.join(names)}")

        return [
            "Known areas: " + ", ".join(area_names),
            "Known floors: " + ", ".join(floor.name for floor in floors),
            "Areas by floor: " + "; ".join(by_floor),
        ]

    @callback
    def _async_get_floor_disambiguation(self) -> str:
        """Spell out which names are floors.

        Floor names read like room names in speech ("turn off the lights
        upstairs"), and the matcher silently finds nothing when a floor is
        passed as an area.
        """
        floor_names = ", ".join(floor.name for floor in self._async_sorted_floors())
        return (
            "To act on somewhere else, pass its name to the intent tool: use the "
            "`area` argument for an area, and the `floor` argument for a floor. "
            f"{floor_names} are FLOORS, not areas - target them with floor=, "
            "never area=. Everything in 'Known areas' is an area."
        )

    @callback
    def _async_sorted_floors(self) -> list[fr.FloorEntry]:
        """Floors from the top down, the way people describe them."""
        return sorted(
            fr.async_get(self.hass).async_list_floors(),
            key=lambda floor: (floor.level is None, -(floor.level or 0), floor.name),
        )

    @callback
    def _async_get_entity_list(
        self, entities: dict[str, dict[str, Any]], scope: Scope
    ) -> tuple[list[str], int]:
        """Render the entities worth carrying in context.

        With a room that is the room, its earshot neighbours and anything
        house-wide. Without one it is the house-wide set alone, which may be
        empty -- an empty list plus the search tool beats several hundred
        entities the caller has no way to disambiguate between.
        """
        near_area_ids = scope.near_area_ids
        listed = []
        for entity_id, info in entities.items():
            if self._actionable_only and info["domain"] not in ACTIONABLE_DOMAINS:
                continue
            in_scope = (
                near_area_ids
                and async_entity_area_id(self.hass, entity_id) in near_area_ids
            )
            if in_scope or async_is_ambient(
                self.hass, entity_id, self._ambient_prefix
            ):
                listed.append(info)

        if not listed:
            where = f"the {scope.area.name} or its adjacent areas" if scope.area else (
                "no single room"
            )
            return (
                [
                    f"No devices are listed for {where}. Use {TOOL_NAME} for "
                    "anything the user asks for."
                ],
                0,
            )

        header = (
            f"Nearby devices (a PARTIAL list - only the {scope.area.name} and "
            "adjacent areas; the rest of the home is not shown):"
            if scope.area
            else "House-wide devices (a PARTIAL list - only devices that belong "
            "to no single room; the rest of the home is not shown):"
        )
        return ([header, yaml_util.dump(listed)], len(listed))
