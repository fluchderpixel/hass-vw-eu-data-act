"""Sensor platform: curated sensors + raw diagnostic data points."""

from __future__ import annotations

from datetime import datetime
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
import homeassistant.util.dt as dt_util

from . import EudaConfigEntry
from .const import raw_unique_id
from .coordinator import EudaCoordinator
from .data import (
    CURATED_BINARY_DOTTED,
    CURATED_BINARY_FLAT,
    CURATED_SENSORS_DOTTED,
    CURATED_SENSORS_FLAT,
    UNIT_RESOLVERS,
    CuratedSensor,
    DataPoint,
    detect_dataset_format,
    friendly_name,
    resolve_distance_unit,
)
from .entity import EudaEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EudaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    points: dict[str, DataPoint] = coordinator.data or {}
    present_fields = {dp.field_name for dp in points.values()}

    # Format erkennen (Dotted für ID, Flat für T7)
    format_type = detect_dataset_format(points)
    
    if format_type == "dotted":
        curated_sensors = CURATED_SENSORS_DOTTED
        binary_fields = {b.field_name for b in CURATED_BINARY_DOTTED}
    else:
        curated_sensors = CURATED_SENSORS_FLAT
        binary_fields = {b.field_name for b in CURATED_BINARY_FLAT}

    # 1. Registriere die verfeinerten T7/ID Sensoren
    entities: list[SensorEntity] = []
    curated_fields: set[str] = set()

    for cur in curated_sensors:
        if cur.field_name in present_fields:
            entities.append(EudaCuratedSensor(coordinator, cur))
            curated_fields.add(cur.field_name)

    # 2. Registriere den Rest als Diagnose-Sensoren (standardmäßig deaktiviert)
    for key, dp in points.items():
        if dp.field_name not in curated_fields and dp.field_name not in binary_fields:
            entities.append(EudaRawSensor(coordinator, key))

    async_add_entities(entities)


class EudaCuratedSensor(EudaEntity, SensorEntity):
    """A high-value sensor with explicit translation and device-class mappings."""

    def __init__(self, coordinator: EudaCoordinator, cur: CuratedSensor) -> None:
        super().__init__(coordinator)
        self._cur = cur
        self._attr_unique_id = f"{coordinator.vin}_{cur.field_name}"
        self._attr_name = cur.name
        self._attr_icon = cur.icon

        if cur.device_class:
            self._attr_device_class = SensorDeviceClass(cur.device_class)
        if cur.state_class:
            self._attr_state_class = SensorStateClass(cur.state_class)
        if cur.suggested_display_precision is not None:
            self._attr_suggested_display_precision = cur.suggested_display_precision

    def _find_dp(self) -> DataPoint | None:
        """Hilfsmethode, um den Datenpunkt sicher anhand des field_name zu finden."""
        points = self.coordinator.data or {}
        for point in points.values():
            if getattr(point, "field_name", None) == self._cur.field_name:
                return point
        return None

    @property
    def native_value(self):
        dp = self._find_dp()
        if not dp:
            return None

        # Mathematische Transformationen anwenden
        if self._cur.transform == "duration_s":
            from .data import parse_duration_seconds
            return self._sticky(parse_duration_seconds(dp.raw_value))
        if self._cur.transform == "decikelvin_to_celsius":
            from .data import decikelvin_to_celsius
            return self._sticky(decikelvin_to_celsius(dp.raw_value))
        if self._cur.transform == "abs":
            from .data import abs_value
            return self._sticky(abs_value(dp.value))
        if self._cur.transform == "fuel_consumption":
            from .data import fuel_consumption_l_per_1000km_to_l_per_100km
            return self._sticky(fuel_consumption_l_per_1000km_to_l_per_100km(dp.raw_value))

        return self._sticky(dp.value)

    @property
    def native_unit_of_measurement(self) -> str | None:
        cur = self._cur
        if cur.unit_field:
            points = self.coordinator.data or {}
            unit_dp = None
            for point in points.values():
                if getattr(point, "field_name", None) == cur.unit_field:
                    unit_dp = point
                    break
            
            resolver = UNIT_RESOLVERS.get(cur.unit_resolver)
            if unit_dp and resolver:
                resolved = resolver(unit_dp.value, cur.unit)
                if resolved:
                    return resolved
        return cur.unit

    @property
    def extra_state_attributes(self) -> dict:
        """Fügt das wichtige T7 Zeitstempel-Attribut für das Dashboard hinzu."""
        attrs = {}
        dp = self._find_dp()
        
        # 1. Option: Direkt den Zeitstempel des konkreten Datenpunkts nehmen
        if dp and dp.timestamp_utc:
            from .data import _parse_timestamp
            ts = _parse_timestamp(dp.timestamp_utc)
            if ts:
                attrs["fahrzeug_datenstand"] = ts.isoformat()
                return attrs

        # 2. Option: Fallback auf das globale Paket-Datum des Datasets
        if self.coordinator.latest_dataset and self.coordinator.latest_dataset.captured_at:
            attrs["fahrzeug_datenstand"] = self.coordinator.latest_dataset.captured_at.isoformat()
        else:
            attrs["fahrzeug_datenstand"] = "Unbekannt"
            
        return attrs


class EudaRawSensor(EudaEntity, SensorEntity):
    """A raw data point exposed as a disabled-by-default diagnostic sensor."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: EudaCoordinator, key: str) -> None:
        super().__init__(coordinator)
        dp = coordinator.data[key]
        self._key = key
        # Namespace by VIN: dataset keys are shared across vehicles, so a bare
        # key collides between config entries (see raw_unique_id / migration).
        self._attr_unique_id = raw_unique_id(coordinator.vin, key)
        self._attr_name = friendly_name(dp.field_name, dp.description)
        # only attach a unit when the value is numeric
        if dp.unit and dp.type_hint in ("int", "float"):
            self._attr_native_unit_of_measurement = dp.unit

    @property
    def native_value(self):
        dp = (self.coordinator.data or {}).get(self._key)
        return self._sticky(dp.value if dp else None)

    @property
    def extra_state_attributes(self) -> dict:
        dp = (self.coordinator.data or {}).get(self._key)
        if not dp:
            return {}
        attrs = {"raw_key": self._key}
        if dp.description:
            attrs["description"] = dp.description
        if dp.cluster:
            attrs["cluster"] = dp.cluster
        if dp.timestamp:
            attrs["captured_at"] = dp.timestamp.isoformat()
        return attrs