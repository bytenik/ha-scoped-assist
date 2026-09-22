"""Work out which part of the home a voice request came from."""

from __future__ import annotations

from dataclasses import dataclass, field

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import label_registry as lr


@dataclass(slots=True)
class Scope:
    """The acoustic neighbourhood a request originated in."""

    area: ar.AreaEntry | None = None
    floor: fr.FloorEntry | None = None
    siblings: list[ar.AreaEntry] = field(default_factory=list)

    @property
    def near_area_ids(self) -> set[str]:
        """Area ids whose entities belong in the nearby list."""
        ids = {sibling.id for sibling in self.siblings}
        if self.area is not None:
            ids.add(self.area.id)
        return ids

    @property
    def sibling_names(self) -> list[str]:
        """Sibling area names, for the prompt."""
        return [sibling.name for sibling in self.siblings]


@callback
def async_get_scope(
    hass: HomeAssistant, device_id: str | None, earshot_prefix: str
) -> Scope:
    """Resolve the originating area, its floor and its acoustic siblings.

    Siblings come from area labels: two areas are within earshot of each other
    when they share a label whose id starts with `earshot_prefix`. That keeps
    adjacency in the registry, editable from Settings > Areas, instead of in a
    table here that would drift as rooms are added.
    """
    if not device_id:
        return Scope()

    device = dr.async_get(hass).async_get(device_id)
    if device is None or not device.area_id:
        return Scope()

    area_reg = ar.async_get(hass)
    area = area_reg.async_get_area(device.area_id)
    if area is None:
        return Scope()

    floor = None
    if area.floor_id:
        floor = fr.async_get(hass).async_get_floor(area.floor_id)

    label_reg = lr.async_get(hass)
    earshot_labels = {
        label_id
        for label_id in area.labels
        if (label := label_reg.async_get_label(label_id)) is not None
        and label_id.startswith(earshot_prefix)
    }

    siblings = []
    if earshot_labels:
        siblings = sorted(
            (
                other
                for other in area_reg.async_list_areas()
                if other.id != area.id and earshot_labels & set(other.labels)
            ),
            key=lambda entry: entry.name,
        )

    return Scope(area=area, floor=floor, siblings=siblings)


@callback
def async_entity_area_id(hass: HomeAssistant, entity_id: str) -> str | None:
    """Return the area an entity resolves to, directly or via its device.

    Mirrors the resolution order Assist itself uses when it labels an entity
    with an area in the prompt.
    """
    entry = er.async_get(hass).async_get(entity_id)
    if entry is None:
        return None
    if entry.area_id:
        return entry.area_id
    if entry.device_id and (device := dr.async_get(hass).async_get(entry.device_id)):
        return device.area_id
    return None


@callback
def async_is_ambient(hass: HomeAssistant, entity_id: str, prefix: str) -> bool:
    """True if an entity belongs to the whole house rather than one room.

    Checked on the entity, its device and its area, because a label can
    sensibly be applied at any of the three: one thermostat, a whole outdoor
    lighting device, or every entity in the Driveway.
    """
    entry = er.async_get(hass).async_get(entity_id)
    if entry is None:
        return False
    if any(label.startswith(prefix) for label in entry.labels):
        return True

    area_id = entry.area_id
    if entry.device_id and (device := dr.async_get(hass).async_get(entry.device_id)):
        if any(label.startswith(prefix) for label in device.labels):
            return True
        area_id = area_id or device.area_id

    if area_id and (area := ar.async_get(hass).async_get_area(area_id)):
        return any(label.startswith(prefix) for label in area.labels)
    return False
