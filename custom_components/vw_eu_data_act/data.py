"""Pure-Python data layer: dataset parsing, value typing, curated registry.

This module intentionally has **no Home Assistant imports** so the parsing and
mapping logic can be unit-tested offline. Platform modules translate the plain
string device-class / unit values here into HA enums.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

# ---------------------------------------------------------------------------
# Data dictionary (generated from the PDF by tools/parse_dictionary.py)
# ---------------------------------------------------------------------------

_DICT_PATH = Path(__file__).parent / "data_dictionary.json"


@lru_cache(maxsize=1)
def load_dictionary() -> dict[str, dict[str, str]]:
    """Return { key-uuid: {name, description, unit, type, cluster} }."""
    try:
        return json.loads(_DICT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------------------
# Dataset format detection
# ---------------------------------------------------------------------------


def detect_dataset_format(points: dict[str, "DataPoint"]) -> str:
    """Detect whether dataset uses dotted (ID.x) or flat (eGolf) naming.

    Returns "dotted" if any field name contains a dot, otherwise "flat".
    ID.x/MEB cars use dotted names (battery_state_report.soc, mileage.value),
    while pre-ID.x cars use flat names (state_of_charge, mileage).
    """
    return "dotted" if any("." in dp.field_name for dp in points.values()) else "flat"


# ---------------------------------------------------------------------------
# Value typing
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(r"^(-?\d+(?:\.\d+)?)\s*s$", re.I)
_INT_RE = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?\d+\.\d+$")


def parse_duration_seconds(raw: str) -> float | None:
    """Parse values like "0s" / "1800s" into seconds."""
    m = _DURATION_RE.match(raw.strip())
    return float(m.group(1)) if m else None


def sticky(previous, current):
    """Keep the last known value when an update omits a field.

    The portal's snapshots don't include every field every cycle; a missing
    field means "no fresh reading", not "unavailable", so we fall back to the
    previous value instead of reporting unknown.
    """
    return current if current is not None else previous


def parse_value(raw: str | None, type_hint: str | None = None):
    """Coerce a raw string value into a typed Python value.

    ``type_hint`` comes from the data dictionary ("int", "float", "boolean",
    "enum", "string"). Falls back to structural detection so it works even
    without a dictionary entry.
    """
    if raw is None:
        return None
    s = raw.strip()
    if s == "":
        return None

    hint = (type_hint or "").lower()

    if hint == "boolean" or s.lower() in ("true", "false"):
        return s.lower() == "true"

    if hint in ("int", "integer") and _INT_RE.match(s):
        return int(s)
    if hint == "float":
        try:
            return float(s)
        except ValueError:
            return s

    # duration shorthand ("0s")
    dur = parse_duration_seconds(s)
    if dur is not None:
        return dur

    # structural fallbacks
    if _INT_RE.match(s):
        return int(s)
    if _FLOAT_RE.match(s):
        return float(s)

    return s  # enums, ISO timestamps, free text stay as strings


# ---------------------------------------------------------------------------
# Enum + naming helpers
# ---------------------------------------------------------------------------

_ENUM_TOKEN_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Bare field names that are meaningless on their own; for these we name the
# entity from the dictionary description instead.
_GENERIC_FIELD_NAMES = {"value", "state", "unit", "is_set", "type", "id"}


def enum_members(description: str | None) -> list[str]:
    """Parse an ordered enum member list out of a dictionary description.

    Enum fields document their members as a comma-separated, index-ordered list
    (e.g. "IMMEDIATE_ACTION_STATE_INVALID, ..."). PDF extraction injects stray
    spaces inside the tokens, so whitespace is stripped before checking each
    token looks like an UPPER_SNAKE enum label. Returns [] for prose / non-enum
    descriptions.
    """
    if not description:
        return []
    members = [re.sub(r"\s+", "", part) for part in description.split(",")]
    members = [m for m in members if _ENUM_TOKEN_RE.match(m)]
    return members if len(members) >= 2 else []


def friendly_name(field_name: str, description: str | None = None) -> str:
    """Entity name for a raw data point.

    Dotted field names are descriptive enough as-is, but some are bare and
    meaningless ("value", "state", ...). For those, fall back to the dictionary
    description (first sentence, trimmed).
    """
    if field_name.lower() in _GENERIC_FIELD_NAMES and description:
        text = description.strip().split(".")[0].strip()
        if text:
            return text[:60]
    return field_name


# ---------------------------------------------------------------------------
# Dataset model
# ---------------------------------------------------------------------------


@dataclass
class DataPoint:
    key: str
    field_name: str
    raw_value: str
    type_hint: str | None = None
    unit: str | None = None
    description: str | None = None
    cluster: str | None = None
    timestamp_utc: str | None = None

    @property
    def value(self):
        v = parse_value(self.raw_value, self.type_hint)
        # Enum fields occasionally deliver the raw protobuf integer index instead
        # of the label; resolve it back to the string using the documented members.
        if self.type_hint == "enum" and isinstance(v, int) and not isinstance(v, bool):
            members = enum_members(self.description)
            if 0 <= v < len(members):
                return members[v]
        return v

    @property
    def timestamp(self) -> datetime | None:
        """Parse the timestampUtc field into a datetime object."""
        return _parse_timestamp(self.timestamp_utc) if self.timestamp_utc else None


@dataclass
class Dataset:
    """A parsed dataset JSON, enriched from the data dictionary."""

    vin: str
    user_id: str | None
    points: dict[str, DataPoint] = field(default_factory=dict)  # by key
    captured_at: datetime | None = None

    @classmethod
    def from_json(cls, payload: dict) -> "Dataset":
        dictionary = load_dictionary()
        points: dict[str, DataPoint] = {}
        captured: list[datetime] = []
        for item in payload.get("Data", []):
            key = item.get("key")
            if not key:
                continue
            meta = dictionary.get(key, {})
            field_name = item.get("dataFieldName") or meta.get("name") or key
            dp = DataPoint(
                key=key,
                field_name=field_name,
                raw_value=item.get("value", ""),
                type_hint=meta.get("type") or None,
                unit=meta.get("unit") or None,
                description=meta.get("description") or None,
                cluster=meta.get("cluster") or None,
                timestamp_utc=item.get("timestampUtc") or None,
            )
            points[key] = dp
            if field_name == "car_captured_time":
                ts = _parse_timestamp(dp.raw_value)
                if ts:
                    captured.append(ts)
        return cls(
            vin=payload.get("vin", ""),
            user_id=payload.get("user_id"),
            points=points,
            captured_at=max(captured) if captured else None,
        )

    def by_field(self, field_name: str) -> DataPoint | None:
        """Return a single data point for a (possibly duplicated) field name.

        The portal merges several report snapshots into one flat array with no
        ordering guarantee and no way to tell which value is "live", so a field
        like ``charging_state_report.current_charge_state`` can appear several
        times under different UUIDs with conflicting values. We pick the entry
        with the smallest ``key`` (UUID): an arbitrary but *stable* choice, so a
        curated sensor consistently tracks the same data point across refreshes
        instead of flip-flopping when the portal reshuffles the array.
        """
        matches = [dp for dp in self.points.values() if dp.field_name == field_name]
        return min(matches, key=lambda dp: dp.key) if matches else None


def _parse_timestamp(raw: str) -> datetime | None:
    """Parse the various timestamp encodings seen in datasets."""
    s = (raw or "").strip()
    if not s:
        return None
    # epoch millis
    if _INT_RE.match(s) and len(s) >= 12:
        try:
            return datetime.fromtimestamp(int(s) / 1000, tz=timezone.utc)
        except (ValueError, OSError):
            return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Curated entity registry  (plain strings -> translated to HA enums in platforms)
# ---------------------------------------------------------------------------


# Distance unit enums (e.g. mileage.unit) -> HA unit. The portal reports
# mileage/range in either miles or kilometres depending on the vehicle, so the
# unit must not be hardcoded; it is read from a companion "*.unit" field.
DISTANCE_UNIT_BY_ENUM: dict[str, str] = {
    "MILES": "mi",
    "MILE": "mi",
    "KM": "km",
    "KILOMETER": "km",
    "KILOMETERS": "km",
    "KILOMETRE": "km",
    "KILOMETRES": "km",
}


def resolve_distance_unit(enum_value, default: str | None = None) -> str | None:
    """Map a distance-unit enum value (e.g. "MILES") to an HA unit ("mi")."""
    if isinstance(enum_value, str):
        return DISTANCE_UNIT_BY_ENUM.get(enum_value.strip().upper(), default)
    return default


# Charge-rate unit enums (battery_state_report.charge_rate_unit) -> HA unit.
# The charge rate is expressed as range gained over time and the unit (km vs
# miles, per hour vs per minute) varies by vehicle/region, so it is read from
# the companion charge_rate_unit field rather than hardcoded.
CHARGE_RATE_UNIT_BY_ENUM: dict[str, str] = {
    "CHARGE_RATE_UNIT_KM_PER_H": "km/h",
    "CHARGE_RATE_UNIT_KM_PER_MIN": "km/min",
    "CHARGE_RATE_UNIT_MILES_PER_H": "mi/h",
    "CHARGE_RATE_UNIT_MILES_PER_MIN": "mi/min",
}


def resolve_charge_rate_unit(enum_value, default: str | None = None) -> str | None:
    """Map a charge-rate-unit enum (e.g. "CHARGE_RATE_UNIT_KM_PER_H") to "km/h"."""
    if isinstance(enum_value, str):
        return CHARGE_RATE_UNIT_BY_ENUM.get(enum_value.strip().upper(), default)
    return default


def decikelvin_to_celsius(raw: str) -> float | None:
    """Convert deci-Kelvin (e.g., "2921") to Celsius.

    Outside temperature is reported in deci-Kelvin (dK):
    - 2921 dK = 292.1 K = 19.06°C
    """
    try:
        return round((float(raw) / 10) - 273.15, 1)
    except (ValueError, TypeError):
        return None


def abs_value(value) -> int | float | None:
    """Return absolute value, handling negative maintenance intervals.

    Maintenance intervals can be negative (overdue). Take absolute value
    for display, as the sign indicates past-due status.
    """
    try:
        abs_val = abs(float(value))
        return int(abs_val) if abs_val == int(abs_val) else abs_val
    except (ValueError, TypeError):
        return None


def fuel_consumption_l_per_1000km_to_l_per_100km(value) -> float | None:
    """Convert fuel consumption from L/1000km to L/100km.

    The API reports fuel consumption in L/1000km (e.g., 168 L/1000km).
    Convert to standard L/100km by dividing by 10 (e.g., 16.8 L/100km).
    """
    try:
        return round(float(value) / 10, 1)
    except (ValueError, TypeError):
        return None


# Named unit resolvers selectable per curated sensor via ``unit_resolver``.
UNIT_RESOLVERS = {
    "distance": resolve_distance_unit,
    "charge_rate": resolve_charge_rate_unit,
}


@dataclass(frozen=True)
class CuratedSensor:
    field_name: str
    name: str
    device_class: str | None = None
    unit: str | None = None
    state_class: str | None = None
    icon: str | None = None
    transform: str | None = None
    unit_field: str | None = None
    unit_resolver: str = "distance"
    suggested_display_precision: int | None = None


@dataclass(frozen=True)
class CuratedBinary:
    field_name: str
    name: str
    device_class: str | None = None
    invert: bool = False  # is_on = (value is False) when True
    icon: str | None = None


# ---------------------------------------------------------------------------
# Curated sensors for ID.x/MEB vehicles (dotted field names)
# ---------------------------------------------------------------------------

CURATED_SENSORS_DOTTED: tuple[CuratedSensor, ...] = (
    # === Charging & Battery ===
    CuratedSensor("battery_state_report.soc", "Battery", "battery", "%", "measurement"),
    CuratedSensor(
        "settings.target_soc",
        "Target charge level",
        None,
        "%",
        "measurement",
        icon="mdi:battery-charging-80",
    ),
    CuratedSensor(
        "battery_state_report.charge_bulk_threshold",
        "Charge bulk threshold",
        None,
        "%",
        "measurement",
        icon="mdi:battery-charging-100",
    ),
    CuratedSensor(
        "battery_state_report.charge_power",
        "Charge power",
        "power",
        "kW",
        "measurement",
    ),
    CuratedSensor(
        "battery_state_report.charge_rate",
        "Charge rate",
        None,
        "km/h",
        "measurement",
        icon="mdi:speedometer",
        unit_field="battery_state_report.charge_rate_unit",
        unit_resolver="charge_rate",
    ),
    CuratedSensor(
        "battery_state_report.charge_energy",
        "Charged energy",
        "energy",
        "kWh",
        "total_increasing",
        icon="mdi:lightning-bolt-circle",
    ),
    CuratedSensor(
        "battery_state_report.remaining_charging_time_complete",
        "Remaining charging time",
        "duration",
        "s",
        "measurement",
        transform="duration_s",
        icon="mdi:battery-clock",
    ),
    CuratedSensor(
        "battery_state_report.remaining_charging_time_bulk",
        "Remaining time to bulk",
        "duration",
        "s",
        "measurement",
        transform="duration_s",
        icon="mdi:battery-clock",
    ),
    # === Distance & Range ===
    CuratedSensor(
        "mileage.value",
        "Mileage",
        "distance",
        "km",
        "total_increasing",
        icon="mdi:counter",
        unit_field="mileage.unit",
        unit_resolver="distance",
        suggested_display_precision=0,
    ),
    CuratedSensor(
        "range.value",
        "Electric range",
        "distance",
        "km",
        "measurement",
        icon="mdi:map-marker-distance",
        unit_field="range.unit",
        unit_resolver="distance",
        suggested_display_precision=0,
    ),
    # === Climate ===
    CuratedSensor(
        "remaining_climate_time",
        "Remaining climate time",
        "duration",
        "s",
        "measurement",
        transform="duration_s",
    ),
    CuratedSensor(
        "residual_energy_in_percent",
        "Residual energy",
        None,
        "%",
        "measurement",
        icon="mdi:battery",
    ),
    # === Temperature ===
    CuratedSensor(
        "min_temperature", "Battery min temperature", "temperature", "°C", "measurement"
    ),
    CuratedSensor(
        "max_temperature", "Battery max temperature", "temperature", "°C", "measurement"
    ),
    # === Vehicle Status ===
    CuratedSensor(
        "mileage.value.timestamp",
        "Last connected",
        "timestamp",
        None,
        None,
        icon="mdi:clock",
    ),
    # === Enum/Status Sensors ===
    CuratedSensor(
        "charging_state_report.current_charge_state",
        "Charge state",
        icon="mdi:ev-station",
    ),
    CuratedSensor(
        "charging_state_report.charge_mode", "Charge mode", icon="mdi:ev-station"
    ),
    CuratedSensor(
        "charging_state_report.charge_type", "Charge type", icon="mdi:power-plug"
    ),
    CuratedSensor(
        "charging_state_report.charging_scenario",
        "Charging scenario",
        icon="mdi:ev-station",
    ),
    CuratedSensor(
        "charging_state_report.immediate_action_state",
        "Charging action state",
        icon="mdi:ev-station",
    ),
    CuratedSensor(
        "settings.charge_mode_selection", "Charge mode selection", icon="mdi:cog"
    ),
    CuratedSensor(
        "settings.max_charge_current_ac", "Max AC charge current", icon="mdi:current-ac"
    ),
    CuratedSensor(
        "window_heating_state", "Window heating", icon="mdi:car-defrost-rear"
    ),
    CuratedSensor("bem_level", "BEM level", None, None, None, icon="mdi:information"),
)

CURATED_BINARY_DOTTED: tuple[CuratedBinary, ...] = (
    # === General Lock State ===
    CuratedBinary("locked", "Vehicle locked", "lock", invert=True, icon="mdi:car-key"),
    # ID.x datasets carry a flat-named parking_brake field even though most of
    # their fields are dotted, so it belongs in the dotted group too.
    CuratedBinary("parking_brake", "Parking brake", None, icon="mdi:car-brake-parking"),
)

# ---------------------------------------------------------------------------
# DEINE OPTIMIERTE T7-REGISTRY (Vollständig mit deinen Bezeichnern und Icons)
# ---------------------------------------------------------------------------

CURATED_SENSORS_FLAT: tuple[CuratedSensor, ...] = (
    CuratedSensor(
        "mileage",
        "Kilometerstand",
        "distance",
        "km",
        "total_increasing",
        icon="mdi:counter",
        suggested_display_precision=0,
    ),
    CuratedSensor(
        "short_term_data_mileage",
        "Strecke (Letzte Fahrt)",
        "distance",
        "km",
        "measurement",
        icon="mdi:map-marker-distance",
        suggested_display_precision=0,
    ),
    CuratedSensor(
        "short_term_data_travel_time",
        "Fahrzeit (Letzte Fahrt)",
        "duration",
        "min",
        "measurement",
        icon="mdi:timer-outline",
    ),
    CuratedSensor(
        "cruising_range_combined",
        "Diesel Reichweite",
        "distance",
        "km",
        "measurement",
        icon="mdi:gas-station",
        suggested_display_precision=0,
    ),
    CuratedSensor(
        "scr_range",
        "AdBlue Reichweite",
        "distance",
        "km",
        "measurement",
        icon="mdi:map-marker-distance",
        suggested_display_precision=0,
    ),
    CuratedSensor(
        "fuel_level_current_level",
        "Tankfüllstand",
        None,
        "%",
        "measurement",
        icon="mdi:gauge",
    ),
    CuratedSensor(
        "oil_level_actual_level",
        "Motoröl Füllstand (Messbereich)",
        None,
        "%",
        "measurement",
        icon="mdi:oil",
    ),

    # Wartungsintervalle: Ölwechsel
    CuratedSensor(
        "maintenance_interval_distance_until_oil_change",
        "Service km bis Ölwechsel",
        "distance",
        "km",
        "measurement",
        icon="mdi:wrench",
        transform="abs",
        suggested_display_precision=0,
    ),
    CuratedSensor(
        "maintenance_interval__time_until_oil_change",
        "Service Tage bis Ölwechsel",
        None,
        "Tage",
        "measurement",
        icon="mdi:calendar-clock",
        transform="abs",
        suggested_display_precision=0,
    ),
    
    # Wartungsintervalle: Allgemeine Inspektion
    CuratedSensor(
        "maintenance_interval_distance_until_inspection",
        "Service km bis Inspektion",
        "distance",
        "km",
        "measurement",
        icon="mdi:car-wrench",
        transform="abs",
        suggested_display_precision=0,
    ),
    CuratedSensor(
        "maintenance_interval__time_until_inspection",
        "Service Tage bis Inspektion",
        None,
        "Tage",
        "measurement",
        icon="mdi:calendar-check",
        transform="abs",
        suggested_display_precision=0,
    ),
    
    # Temperaturen & Verbrauch
    CuratedSensor(
        "outside_temperature",
        "Außentemperatur",
        "temperature",
        "°C", 
        "measurement", 
        transform="decikelvin_to_celsius",
    ),
    CuratedSensor(
        "short_term_data_average_fuel_consumption",
        "Verbrauch letzte Fahrt",
        None,
        "l/100km",
        "measurement",
        icon="mdi:fuel",
        transform="fuel_consumption",
        suggested_display_precision=1,
    ),
)

CURATED_BINARY_FLAT: tuple[CuratedBinary, ...] = (
    # === Übergreifender Status ===
    CuratedBinary(
        "locked",
        "Fahrzueg verriegelt",
        "lock",
        invert=True,
        icon="mdi:car-key",
    ),
    CuratedBinary(
        "parking_brake",
        "Parkbremse Status",
        None,
        icon="mdi:car-brake-parking",
    ),
    CuratedSensor(
        "parking_lights",
        "Parklicht Status",
        None,
        icon="mdi:car-parking-lights",
    ),

    # === Physische Türen & Klappen (Exakt deine deutschen Namen & deine opening-Klassen) ===
    CuratedBinary(
        "open_state_front_left_door",
        "Fahrertür",
        "door",
        icon="mdi:car-door",
    ),
    CuratedBinary(
        "open_state_front_right_door",
        "Beifahrertür",
        "door",
        icon="mdi:car-door",
    ),
    CuratedBinary(
        "open_state_rear_left_door",
        "Schiebetür links",
        "door",
        icon="mdi:car-door",
    ),
    CuratedBinary(
        "open_state_rear_right_door",
        "Schiebetür rechts",
        "door",
        icon="mdi:car-door",
    ),
    CuratedBinary(
        "open_state_tailgate",
        "Heckklappe",
        "opening",
        icon="mdi:car-back",
    ),
    CuratedBinary(
        "open_state_front_engine_bonnet",
        "Motorhaube",
        "opening",
        icon="mdi:car-hood",
    ),

    # === Schlösser (Hier sind zur Sicherheit beide Varianten drin, einfacher & doppelter Unterstrich) ===
    CuratedBinary(
        "locked_state_front_left_door",
        "Fahrertür Schloss",
        "lock",
        invert=True,
        icon="mdi:car-door-lock",
    ),
    CuratedBinary(
        "locked_state_front_right_door",
        "Beifahrertür Schloss",
        "lock",
        invert=True,
        icon="mdi:car-door-lock",
    ),
    CuratedBinary(
        "locked_state__rear_left_door",
        "Schiebetür links Schloss",
        "lock",
        invert=True,
        icon="mdi:car-door-lock",
    ),
    CuratedBinary(
        "locked_state_rear_right_door",
        "Schiebetür rechts Schloss",
        "lock",
        invert=True,
        icon="mdi:car-door-lock",
    ),
    CuratedBinary(
        "locked_state_tailgate",
        "Heckklappe Schloss",
        "lock",
        invert=True,
        icon="mdi:car-door-lock",
    ),

    # === Fenster ===
    CuratedBinary(
        "state_front_left_door_window_lifter",
        "Fenster vorne links",
        "window",
        icon="mdi:window-closed",
    ),
    CuratedBinary(
        "state_front_right_door_window_lifter",
        "Fenster vorne rechts",
        "window",
        icon="mdi:window-closed",
    ),
)

# ---------------------------------------------------------------------------
# Combined fields for backward compatibility and field validation
# ---------------------------------------------------------------------------

CURATED_FIELDS: frozenset[str] = frozenset(
    [s.field_name for s in CURATED_SENSORS_DOTTED]
    + [s.field_name for s in CURATED_SENSORS_FLAT]
    + [b.field_name for b in CURATED_BINARY_DOTTED]
    + [b.field_name for b in CURATED_BINARY_FLAT]
)
