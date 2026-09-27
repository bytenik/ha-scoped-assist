"""A read-only lookup tool for devices outside the nearby list."""

from __future__ import annotations

import logging
from collections.abc import Collection
from typing import Any, override

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import intent, llm
from homeassistant.util.json import JsonObjectType

from .const import DEFAULT_MAX_MATCHES
from .scope import async_entity_area_id

_LOGGER = logging.getLogger(__name__)

# Prefixed with the integration domain, matching homeassistant__GetLiveContext
# and intent__HassTurnOn alongside it. Home Assistant reports unprefixed tool
# names and breaks them in 2027.3.
TOOL_NAME = "scoped_assist__FindEntities"

DESCRIPTION = (
    "Find devices anywhere in the home by name. Returns every match with the "
    "area it is in. Read-only and cheap. Call it for one device at a time; "
    "call it again for each further device in the same request. Afterwards "
    "act with the normal intent tools, passing the name and area returned."
)

# Home Assistant models plenty of lights and fans as switches: a relay behind a
# floodlight, a wall switch driving a ceiling fan. Speech draws no such
# distinction, so a request for a "light" has to consider the switch that is
# one. Restricted to these three -- widening to every domain would drag sensors
# and diagnostics into a list the model is told to act on.
_DOMAIN_NEIGHBOURS = {
    "light": ("light", "switch"),
    "fan": ("fan", "switch"),
    "switch": ("switch", "light", "fan"),
}


def _neighbours(domain: str) -> tuple[str, ...]:
    """The domains worth searching when the model asks for this one."""
    return _DOMAIN_NEIGHBOURS.get(domain, (domain,))


def _dedupe(
    matches: list[dict[str, str]], preferred: str | None
) -> list[dict[str, str]]:
    """Collapse one device surfaced under two domains into a single match.

    A relay exposed as both a light and a switch is one thing to the person
    speaking, and acting on it twice is at best wasteful. The entity in the
    domain the model asked for wins.
    """
    best: dict[tuple[str, str], dict[str, str]] = {}
    for match in matches:
        key = (match["name"].casefold(), match["area"].casefold())
        current = best.get(key)
        if current is None or (
            preferred
            and current["domain"] != preferred
            and match["domain"] == preferred
        ):
            best[key] = match
    return sorted(best.values(), key=lambda m: (m["area"], m["name"]))


# Words that carry no identity, stripped before matching so that "all the flood
# lights" still matches an entity named "Front Flood Lights".
_FILLER = frozenset(
    {"the", "a", "an", "all", "my", "our", "some", "any", "every", "both"}
)


def _squash(text: str) -> str:
    """Letters and digits only, lowercased.

    Speech-to-text is inconsistent about compounds: the same floodlight comes
    back as "flood lights" one turn and "floodlights" the next, and an entity
    registered either way should match both.
    """
    return "".join(c for c in text.casefold() if c.isalnum())


def _variants(tokens: list[str]) -> list[str]:
    """Each token, plus every run of adjacent tokens joined up.

    Lets a single spoken word reach a name that spells it as several, and the
    other way round, without giving short tokens a larger edit budget than
    they have earned.
    """
    out = set(tokens)
    for size in (2, 3):
        for start in range(len(tokens) - size + 1):
            out.add("".join(tokens[start : start + size]))
    return sorted(out)


def _tokens(text: str) -> list[str]:
    """Split a name into comparable lowercase word tokens."""
    cleaned = "".join(c if c.isalnum() or c.isspace() else " " for c in text)
    return [t for t in cleaned.casefold().split() if t and t not in _FILLER]


def _edit_distance(a: str, b: str, cap: int) -> int:
    """Levenshtein distance, abandoned once it exceeds cap."""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb))
            )
        if min(current) > cap:
            return cap + 1
        previous = current
    return previous[-1]


def _budget(token: str) -> int:
    """How many edits a token of this length may absorb.

    Short tokens get none: at three characters "fan" and "can" are one edit
    apart, and acting on the wrong device is worse than finding none. The
    allowance opens up with length, where a single mis-heard phoneme is
    unlikely to collide with a different real word -- "food" for "flood" being
    the case that prompted this.
    """
    length = len(token)
    if length <= 3:
        return 0
    if length <= 6:
        return 1
    return 2


def _fuzzy_match(query_tokens: list[str], name_tokens: list[str]) -> bool:
    """True if the query is within its edit budget of the name.

    Satisfied either word by word, or by the whole query against a run of the
    name's words: "foodlights" is a mishearing of a compound that the name
    spells as two words, and neither comparison alone catches it.
    """
    if not query_tokens:
        return False
    name_variants = _variants(name_tokens)
    if all(
        any(_edit_distance(qt, nv, _budget(qt)) <= _budget(qt) for nv in name_variants)
        for qt in query_tokens
    ):
        return True
    whole = "".join(query_tokens)
    return any(
        _edit_distance(whole, nv, _budget(whole)) <= _budget(whole)
        for nv in name_variants
    )


class FindEntitiesTool(llm.Tool):
    """Locate exposed entities by name, anywhere in the home."""

    name = TOOL_NAME
    description = DESCRIPTION
    parameters = vol.Schema(
        {
            vol.Required("name"): str,
            vol.Optional("domain"): str,
            vol.Optional("area"): str,
        }
    )

    def __init__(
        self,
        exposed_entities: dict[str, dict[str, Any]],
        max_matches: int = DEFAULT_MAX_MATCHES,
    ) -> None:
        """Keep the exposed set for the name-based passes."""
        self._exposed = exposed_entities
        self._max_matches = max_matches

    @override
    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Search by name, widening the search until something matches.

        The ladder is exact, then substring, then fuzzy, run first over the
        requested domain and its neighbours and then over everything. Both
        widenings exist because the request is a transcript of speech: the words
        are approximate, and the domain the model infers from them is a guess
        about how Home Assistant happens to model the device. "Turn on the flood
        lights" yields domain=light, but one of the four is a switch -- and
        filtering it out is how a complete answer becomes a partial one.
        """
        query: str = tool_input.tool_args["name"]
        domain: str | None = tool_input.tool_args.get("domain")
        area: str | None = tool_input.tool_args.get("area")

        matches: list[dict[str, str]] = []
        how = "none"

        # Stage one keeps the model's domain guess, widened to the domains that
        # can hold the same physical device. Stage two abandons it entirely.
        stages: tuple[Collection[str] | None, ...] = (
            (_neighbours(domain), None) if domain else (None,)
        )
        for domains in stages:
            for pass_name in ("exact", "substring", "fuzzy"):
                if pass_name == "exact":
                    matches = self._match_exact(
                        hass, query, domains, area, llm_context.assistant
                    )
                elif pass_name == "substring":
                    matches = self._match_substring(query, domains, area)
                else:
                    matches = self._match_fuzzy(query, domains, area)
                if matches:
                    how = pass_name
                    break
            if matches:
                break

        matches = _dedupe(matches, domain)
        widened = sorted(
            {m["domain"] for m in matches if domain and m["domain"] != domain}
        )

        _LOGGER.debug(
            "FindEntities | name=%r domain=%r area=%r -> %d match(es) via %s%s: %s",
            query,
            domain,
            area,
            len(matches),
            how,
            f" (+{'/'.join(widened)})" if widened else "",
            [f"{m['name']} ({m['area']})" for m in matches[:10]],
        )

        if not matches:
            return {
                "matches": [],
                "note": (
                    f"Nothing exposed to voice matches '{query}', even allowing "
                    "for misheard words. Do not retry the same search; tell the "
                    "user you could not find it."
                ),
            }

        notes = []
        if how == "fuzzy":
            notes.append(
                f"These are APPROXIMATE matches: nothing is named exactly "
                f"'{query}', so these are the closest names and the request was "
                "probably misheard. Name the device you act on in your reply so "
                "the user can correct you."
            )
        if widened:
            notes.append(
                f"Some matches are a {' or '.join(widened)} rather than a "
                f"{domain}: Home Assistant models some lights and fans as "
                "switches. They are still the right devices; act on them."
            )
        if len(matches) > self._max_matches:
            notes.append(f"Showing the first {self._max_matches} of {len(matches)}.")

        result: JsonObjectType = {
            "matches": matches[: self._max_matches],
            "count": len(matches),
            "match_type": how,
        }
        if notes:
            result["note"] = " ".join(notes)
        return result

    def _match_exact(
        self,
        hass: HomeAssistant,
        query: str,
        domains: Collection[str] | None,
        area: str | None,
        assistant: str | None,
    ) -> list[dict[str, str]]:
        """Use Assist's own matcher, which understands names and aliases."""
        result = intent.async_match_targets(
            hass,
            intent.MatchTargetsConstraints(
                name=query,
                area_name=area,
                domains=list(domains) if domains else None,
                assistant=assistant,
                allow_duplicate_names=True,
            ),
        )
        if not result.is_match:
            return []

        area_reg = ar.async_get(hass)
        matches = []
        for state in result.states:
            area_id = async_entity_area_id(hass, state.entity_id)
            area_entry = area_reg.async_get_area(area_id) if area_id else None
            matches.append(
                {
                    "name": state.name,
                    "domain": state.domain,
                    "area": area_entry.name if area_entry else "unassigned",
                }
            )
        return sorted(matches, key=lambda m: (m["area"], m["name"]))

    def _candidates(
        self, domains: Collection[str] | None, area: str | None
    ) -> list[tuple[list[str], dict[str, str]]]:
        """Exposed entities passing the domain and area filters, with their names."""
        out = []
        for info in self._exposed.values():
            if domains and info.get("domain") not in domains:
                continue
            areas = info.get("areas") or ""
            if area and area.casefold() not in areas.casefold():
                continue
            names = [n.strip() for n in info.get("names", "").split(",") if n.strip()]
            if not names:
                continue
            out.append(
                (
                    names,
                    {
                        "name": names[0],
                        "domain": info.get("domain", ""),
                        "area": areas.split(",")[0].strip() or "unassigned",
                    },
                )
            )
        return out

    def _match_substring(
        self, query: str, domains: Collection[str] | None, area: str | None
    ) -> list[dict[str, str]]:
        """Fall back to substring matching.

        The exact matcher only accepts whole names and aliases, so a request for
        "the flood lights" finds nothing even when three entities are named
        "Front Flood Lights". Voice phrasing is rarely the registered name.

        Compared both as written and with the spacing removed, because
        speech-to-text splits compounds inconsistently and "front floodlights"
        is otherwise not a substring of "Front Flood Lights".
        """
        needle = query.casefold().strip()
        squashed = _squash(query)
        matches = [
            row
            for names, row in self._candidates(domains, area)
            if any(
                needle in name.casefold()
                or (squashed and squashed in _squash(name))
                for name in names
            )
        ]
        return sorted(matches, key=lambda m: (m["area"], m["name"]))

    def _match_fuzzy(
        self, query: str, domains: Collection[str] | None, area: str | None
    ) -> list[dict[str, str]]:
        """Last resort: match per word, tolerating a mis-heard character or two.

        Speech-to-text errors are the common cause of a failed lookup, and they
        are usually a single phoneme: "food lights" for "flood lights" cost a
        whole turn. Whole-string distance is useless here because the query is
        normally a fragment of a longer name, so each query word is matched
        against the name's words independently.
        """
        query_tokens = _tokens(query)
        matches = [
            row
            for names, row in self._candidates(domains, area)
            if any(_fuzzy_match(query_tokens, _tokens(name)) for name in names)
        ]
        return sorted(matches, key=lambda m: (m["area"], m["name"]))
