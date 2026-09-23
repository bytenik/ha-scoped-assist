# Scoped Assist

A replacement Assist API for Home Assistant that tells the model **where the
request came from**, and sends it the devices in that room instead of every
device in the house.

## The problem

Home Assistant's built-in Assist API puts every exposed entity into the prompt
on every voice turn, and never says which satellite was spoken to. On a house
with 514 exposed entities that is a 36,858-character prompt — about 10,500
tokens — repeated on every "turn on the lights".

The cost is the obvious part. The accuracy is the worse part. Six rooms with a
ceiling fan produce six entities called `Ceiling Fan`, distinguishable only by
an area the model was never told, so the fan that comes on is the one that
happened to sort first.

Scoped Assist sends the originating room, the rooms within earshot of it, and
a name-only index of everywhere else, with a search tool for anything not in
front of it.

| | Built-in Assist | Scoped Assist |
|---|---|---|
| Prompt, request from a room | 36,858 chars | ~4,600 chars |
| Prompt, request with no room | 36,858 chars | ~3,200 chars |
| Entities listed | all 514 | the room's and its neighbours' |
| Knows where it is | no | yes |
| Can find a device it wasn't sent | no | yes |

Benchmarked before it was built, against a captured catalog of those 514
entities on `gpt-oss-20b` across nine command shapes: the flat prompt scored
15/30, the scoped one 30/30. **Prompt shape, not model size, was the lever** —
the same small model went from unusable to correct on the same hardware.

## Installation

### HACS

Add `https://github.com/bytenik/ha-scoped-assist` as a custom repository of
type *Integration*, install, and restart Home Assistant.

### Manual

Copy this repository into `custom_components/scoped_assist/` in your Home
Assistant configuration directory and restart.

## Configuration

Add to `configuration.yaml`:

```yaml
scoped_assist:
```

Then select **Assist (room-scoped)** in place of **Assist** on your
conversation agent: *Settings → Devices & Services → your agent → Configure →
Control Home Assistant*. Select only one — picking both merges the two APIs and
sends the entity list twice.

The built-in API is left registered and untouched, so switching back is the
same dropdown.

All options, with their defaults:

```yaml
scoped_assist:
  earshot_label_prefix: earshot   # label prefix marking areas within earshot
  ambient_label_prefix: ambient   # label prefix marking house-wide devices
  actionable_only: true           # omit sensors from the device list
  max_matches: 20                 # cap on search results returned
  confirm_over: 6                 # confirm actions affecting more devices than this
```

## Areas within earshot

**This is the part that needs setting up, and nothing else will hint that it is
missing.** Without it every room reports no neighbours and the integration
works, but only for the room you are standing in.

Panels in adjacent rooms hear each other. A living room and a kitchen seven
feet apart will both pick up "turn on the ceiling fan", and whichever answers
needs to consider both rooms' fans, not just its own.

Adjacency lives in Home Assistant's **area labels** rather than in this
configuration, so it is editable in the UI and cannot drift out of step as
rooms are added or panels move.

1. *Settings → Areas, labels & zones → Labels* → create a label named
   `earshot-main` (any name beginning with `earshot` works).
2. Apply it to each area in that acoustic group.

Two areas are treated as within earshot when they share any `earshot-*` label.
An area can carry several labels, which is how a hallway bridging two groups is
expressed. A label with only one area on it does nothing.

## House-wide devices

Some devices belong to no room — outdoor lighting, an HVAC system, anything you
would ask for from anywhere. Label them `ambient` (or any name starting with
`ambient`) and they are always in context.

The label is checked on the **entity, its device, and its area**, so it can be
applied at whichever level fits: one thermostat, a whole outdoor lighting
device, or every entity in the Driveway at once.

## What the model receives

**From a voice satellite with an area:** the devices of that area and of every
area within earshot; a list of all area and floor names; a statement of which
room the request came from and which rooms are adjacent; and the search tool.

**From anything with no area** — the web UI, the companion app, a satellite
nobody assigned to a room — the same, minus the room: the taxonomy, any
`ambient` devices, and the search tool. Deliberately *not* the full entity
dump, which would hand the largest prompt in the system to the caller least able
to use it. Where a room-scoped request resolves an unqualified command to the
speaker's own area, this one is told to ask which area is meant rather than
guess.

With `actionable_only` (the default), sensors and other read-only entities are
left out of the list; state questions are answered by `GetLiveContext`, which
reads them on demand.

## FindEntities

A read-only search tool for everything not in the prompt. It searches by name,
widening only when the narrower attempt finds nothing:

| | requested domain and its neighbours | any domain |
|---|---|---|
| **exact** (Home Assistant's own matcher) | 1 | 4 |
| **substring** | 2 | 5 |
| **fuzzy** (per-word Levenshtein) | 3 | 6 |

Every step exists because a real request failed without it.

**Exact matching is not enough** because Home Assistant's matcher accepts only
whole registered names and aliases. "The flood lights" matches nothing at all
when the entities are named `Front Flood Lights`.

**Spacing cannot be trusted** because speech-to-text splits compounds
inconsistently: the same fitting arrives as "flood lights" on one turn and
"floodlights" on the next, and an entity registered either way should match
both. Names are compared with the spacing removed as well as as written, and
the fuzzy pass builds each name's adjacent word-runs as candidates, so one
spoken word can reach a name that spells it as several.

**Substring matching is not enough** because speech-to-text is approximate, and
the errors are usually a single phoneme. A request for the flood lights arrived
as "food lights", found nothing, and was answered "those don't exist". Fuzzy
matching is per word rather than whole string, because the query is normally a
fragment of a longer name. The edit budget scales with word length: none at
three characters or fewer, where `fan`, `can` and `man` are all one edit apart
and acting on the wrong device is worse than finding none; one up to six; two
beyond. The whole query is also compared against those word-runs, which is what
catches a mishearing and a compound at once. Fuzzy results are flagged as
approximate, with instructions to name the device acted on so the user can
correct it.

**The domain is a hint, not a filter.** The model infers a domain from the
user's words, and the user's words describe the world, not how Home Assistant
models it. A driveway floodlight on a relay is a `switch`; asking for a "light"
excluded it entirely. Lights, fans and switches are searched together for that
reason. Widening stops there — going further would put sensors and diagnostics
into a list the model is told to act on.

Matches are deduplicated by name and area, preferring the requested domain, so
a relay exposed as both a light and a switch is one result rather than two.

## Confirming broad actions

Actions resolving to more than `confirm_over` distinct devices are described
back and confirmed before they run. Counted per device rather than per entity,
so a ceiling fan exposing a fan and two lights counts once.

This is enforced in the tool layer, not asked of the model in the prompt: the
model cannot know that an area holds fourteen bulbs until it has already
switched them. A `confirm` that arrives in the same turn as the question is
refused, since that is the model agreeing with itself — genuine agreement comes
back as a new turn.

**Limitation.** If your assist pipeline has *prefer local intents* enabled, a
sentence the built-in templates recognise ("turn off all the lights upstairs")
is matched and executed by the default agent without ever reaching a tool call,
and the gate does not see it. What this protects against is therefore a model
inferring too broad a target, not breadth as such. Covering both paths means
either turning off *prefer local intents* so every command routes through the
model, or moving the count into replacement intent handlers.

## Compatibility

Tested against **Home Assistant 2026.7.2**.

This integration calls `homeassistant.helpers.llm._get_exposed_entities`, a
private API, to build the entity list exactly as the built-in Assist API does.
Reimplementing it would mean duplicating around a hundred lines of upstream
name, alias and area resolution and letting them drift. The trade is that a
Home Assistant upgrade can break this integration with no deprecation warning.

It also subclasses `llm.AssistAPI` and reuses its tool assembly, and wraps
`llm.APIInstance.async_call_tool`. These are public, but not contracts upstream
has promised to hold.

Pin your Home Assistant version or read the release notes before upgrading.

## Logging

For diagnosing a turn end to end:

```yaml
logger:
  logs:
    custom_components.scoped_assist: debug
    homeassistant.components.conversation: debug
```

This records the transcript received, the assembled prompt and its size, every
tool call with its arguments, and every result. Nothing else in the stack logs
what the model does with a prompt — the conversation integrations log neither
the tools they call nor the replies they produce, and the conversation trace
lives in memory behind the websocket.

It writes the whole prompt on every turn. Useful while tuning, not something to
leave on.

## License

MIT.
