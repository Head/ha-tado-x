"""Sensor platform for Tado X."""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import API_QUOTA_FREE_TIER, API_QUOTA_PREMIUM, DOMAIN
from .coordinator import (
    TadoXData,
    TadoXDataUpdateCoordinator,
    TadoXDevice,
    TadoXRoom,
    TadoXRoomAirComfort,
    TadoXWeather,
)

_LOGGER = logging.getLogger(__name__)


def _get_api_quota(data: TadoXData) -> int:
    """Get the appropriate API quota.

    Prefers real value from API headers if available, falls back to default values.
    """
    if data.api_quota_limit is not None:
        return data.api_quota_limit
    return API_QUOTA_PREMIUM if data.has_auto_assist else API_QUOTA_FREE_TIER


def _get_api_remaining(data: TadoXData) -> int:
    """Get remaining API calls.

    Prefers real value from API headers if available, falls back to calculated value.
    """
    if data.api_quota_remaining is not None:
        return data.api_quota_remaining
    return max(0, _get_api_quota(data) - data.api_calls_today)


@dataclass(frozen=True, kw_only=True)
class TadoXRoomSensorEntityDescription(SensorEntityDescription):
    """Describes a Tado X room sensor entity."""

    value_fn: Callable[[TadoXRoom], Any]


@dataclass(frozen=True, kw_only=True)
class TadoXDeviceSensorEntityDescription(SensorEntityDescription):
    """Describes a Tado X device sensor entity."""

    value_fn: Callable[[TadoXDevice], Any]


@dataclass(frozen=True, kw_only=True)
class TadoXHomeSensorEntityDescription(SensorEntityDescription):
    """Describes a Tado X home sensor entity."""

    value_fn: Callable[[TadoXData], Any]


@dataclass(frozen=True, kw_only=True)
class TadoXWeatherSensorEntityDescription(SensorEntityDescription):
    """Describes a Tado X weather sensor entity."""

    value_fn: Callable[[TadoXWeather], Any]


@dataclass(frozen=True, kw_only=True)
class TadoXAirComfortSensorEntityDescription(SensorEntityDescription):
    """Describes a Tado X air comfort sensor entity."""

    value_fn: Callable[[TadoXRoomAirComfort], Any]


def _format_running_time_hours(room: TadoXRoom) -> float:
    """Convert running time seconds to hours with one decimal."""
    seconds = room.running_time_today_seconds
    return round(seconds / 3600, 1)


ROOM_SENSORS: tuple[TadoXRoomSensorEntityDescription, ...] = (
    TadoXRoomSensorEntityDescription(
        key="temperature",
        translation_key="temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda room: room.current_temperature,
    ),
    TadoXRoomSensorEntityDescription(
        key="humidity",
        translation_key="humidity",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.HUMIDITY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda room: room.humidity,
    ),
    TadoXRoomSensorEntityDescription(
        key="heating_power",
        translation_key="heating_power",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:radiator",
        value_fn=lambda room: room.heating_power,
    ),
    TadoXRoomSensorEntityDescription(
        key="heating_time_today",
        translation_key="heating_time_today",
        native_unit_of_measurement=UnitOfTime.HOURS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL,
        icon="mdi:clock-time-four",
        value_fn=_format_running_time_hours,
    ),
)

DEVICE_SENSORS: tuple[TadoXDeviceSensorEntityDescription, ...] = (
    TadoXDeviceSensorEntityDescription(
        key="battery",
        translation_key="battery",
        device_class=SensorDeviceClass.ENUM,
        options=["normal", "low"],
        icon="mdi:battery",
        value_fn=lambda device: device.battery_state.lower() if device.battery_state else None,
    ),
    TadoXDeviceSensorEntityDescription(
        key="device_temperature",
        translation_key="device_temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda device: device.temperature_measured,
    ),
    TadoXDeviceSensorEntityDescription(
        key="temperature_offset",
        translation_key="temperature_offset",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:thermometer-plus",
        value_fn=lambda device: device.temperature_offset,
    ),
)

def _get_api_usage_percentage(data: TadoXData) -> float:
    """Calculate API usage percentage.

    Uses real values from API headers when available for accurate calculation.
    """
    quota = _get_api_quota(data)
    if quota == 0:
        return 100.0

    # Calculate calls used based on quota and remaining
    if data.api_quota_remaining is not None and data.api_quota_limit is not None:
        # Use real values from headers
        calls_used = data.api_quota_limit - data.api_quota_remaining
    else:
        # Fall back to internal counter
        calls_used = data.api_calls_today

    return min(100, round((calls_used / quota) * 100, 1))


def _get_api_calls_today(data: TadoXData) -> int:
    """Get API calls made today.

    Uses real values from API headers when available.
    """
    if data.api_quota_remaining is not None and data.api_quota_limit is not None:
        # Calculate from real header values
        return data.api_quota_limit - data.api_quota_remaining
    return data.api_calls_today


def _get_api_status(data: TadoXData) -> str:
    """Get API status - OK or RATE_LIMITED."""
    return "RATE_LIMITED" if data.rate_limited else "OK"


def _get_presence_state(data: TadoXData) -> str | None:
    """Get the current presence state (HOME/AWAY)."""
    return data.presence


def _get_presence_mode(data: TadoXData) -> str:
    """Get the presence mode - MANUAL (locked) or AUTO (geofencing)."""
    if data.presence_locked:
        return "MANUAL"
    return "AUTO"


def _heat_pump_value(data: TadoXData, key: str) -> Any:
    """Read a value from the live heat-pump state payload."""
    return data.heat_pump_state.get(key)


def _heat_pump_heating(data: TadoXData, key: str) -> Any:
    """Read a value from the heat-pump heating payload."""
    return data.heat_pump.get("heating", {}).get(key)


def _heat_pump_setpoint(data: TadoXData) -> Any:
    """Read the current heating setpoint."""
    return (
        data.heat_pump.get("heating", {})
        .get("currentBlockSetpoint", {})
        .get("setpointValue", {})
        .get("value")
    )


def _energy_iq_home(data: TadoXData) -> dict[str, Any]:
    """Return the GraphQL home object from Energy IQ."""
    return data.energy_iq.get("data", {}).get("home", {})


def _energy_iq_requested_month(data: TadoXData) -> dict[str, Any]:
    """Return the requested Energy IQ month aggregation."""
    return (
        _energy_iq_home(data)
        .get("heatingInsights", {})
        .get("consumption", {})
        .get("monthlyAggregation", {})
        .get("requestedMonth", {})
    )


def _energy_iq_today(data: TadoXData) -> float | None:
    """Return today's Energy IQ consumption in the preferred unit."""
    today = date.today().isoformat()
    for entry in _energy_iq_requested_month(data).get("consumptionPerDate", []):
        if entry.get("date") == today:
            return entry.get("consumption")
    return None


def _energy_iq_month_total(data: TadoXData) -> float | None:
    """Return the current month's Energy IQ total consumption."""
    return _energy_iq_requested_month(data).get("totalConsumption")


def _energy_iq_cop(data: TadoXData, source: str) -> float | None:
    """Return a heat-pump COP value from Energy IQ."""
    return (
        _energy_iq_home(data)
        .get("heatPump", {})
        .get("consumption", {})
        .get("cop", {})
        .get(source)
    )


HOME_SENSORS: tuple[TadoXHomeSensorEntityDescription, ...] = (
    TadoXHomeSensorEntityDescription(
        key="api_calls_today",
        translation_key="api_calls_today",
        icon="mdi:counter",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_get_api_calls_today,
    ),
    TadoXHomeSensorEntityDescription(
        key="api_quota_remaining",
        translation_key="api_quota_remaining",
        icon="mdi:api",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_get_api_remaining,
    ),
    TadoXHomeSensorEntityDescription(
        key="api_quota_limit",
        translation_key="api_quota_limit",
        icon="mdi:api",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_get_api_quota,
    ),
    TadoXHomeSensorEntityDescription(
        key="api_usage_percentage",
        translation_key="api_usage_percentage",
        icon="mdi:percent",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_get_api_usage_percentage,
    ),
    TadoXHomeSensorEntityDescription(
        key="api_reset_time",
        translation_key="api_reset_time",
        icon="mdi:clock-outline",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda data: data.api_reset_time,
    ),
    TadoXHomeSensorEntityDescription(
        key="api_status",
        translation_key="api_status",
        icon="mdi:api",
        device_class=SensorDeviceClass.ENUM,
        options=["OK", "RATE_LIMITED"],
        value_fn=_get_api_status,
    ),
    TadoXHomeSensorEntityDescription(
        key="presence_state",
        translation_key="presence_state",
        icon="mdi:home-account",
        device_class=SensorDeviceClass.ENUM,
        options=["HOME", "AWAY"],
        value_fn=_get_presence_state,
    ),
    TadoXHomeSensorEntityDescription(
        key="presence_mode",
        translation_key="presence_mode",
        icon="mdi:map-marker-account",
        device_class=SensorDeviceClass.ENUM,
        options=["AUTO", "MANUAL"],
        value_fn=_get_presence_mode,
    ),
    TadoXHomeSensorEntityDescription(
        key="heat_pump_electric_power",
        translation_key="heat_pump_electric_power",
        native_unit_of_measurement="W",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:flash",
        value_fn=lambda data: _heat_pump_value(data, "currentElectricityConsumptionInWatts"),
    ),
    TadoXHomeSensorEntityDescription(
        key="heat_pump_operation_state",
        translation_key="heat_pump_operation_state",
        device_class=SensorDeviceClass.ENUM,
        options=["NONE", "HEATING", "COOLING", "DOMESTIC_HOT_WATER"],
        icon="mdi:heat-pump",
        value_fn=lambda data: _heat_pump_value(data, "operationState"),
    ),
    TadoXHomeSensorEntityDescription(
        key="heat_pump_space_operation_mode",
        translation_key="heat_pump_space_operation_mode",
        icon="mdi:heat-pump",
        value_fn=lambda data: data.heat_pump.get("spaceOperationMode"),
    ),
    TadoXHomeSensorEntityDescription(
        key="heat_pump_heating_activity",
        translation_key="heat_pump_heating_activity",
        native_unit_of_measurement="%",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:percent",
        value_fn=lambda data: _heat_pump_heating(data, "heatingActivityInPercent"),
    ),
    TadoXHomeSensorEntityDescription(
        key="heat_pump_heating_setpoint",
        translation_key="heat_pump_heating_setpoint",
        native_unit_of_measurement="°C",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:thermometer",
        value_fn=_heat_pump_setpoint,
    ),
    TadoXHomeSensorEntityDescription(
        key="heat_pump_standby",
        translation_key="heat_pump_standby",
        icon="mdi:power-sleep",
        value_fn=lambda data: _heat_pump_heating(data, "standbyActive"),
    ),
    TadoXHomeSensorEntityDescription(
        key="heat_pump_connection_state",
        translation_key="heat_pump_connection_state",
        device_class=SensorDeviceClass.ENUM,
        options=["CONNECTED", "DISCONNECTED"],
        icon="mdi:connection",
        value_fn=lambda data: _heat_pump_value(data, "connectionState"),
    ),
    TadoXHomeSensorEntityDescription(
        key="energy_iq_consumption_today",
        translation_key="energy_iq_consumption_today",
        native_unit_of_measurement="kWh",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:counter",
        value_fn=_energy_iq_today,
    ),
    TadoXHomeSensorEntityDescription(
        key="energy_iq_month_total",
        translation_key="energy_iq_month_total",
        native_unit_of_measurement="kWh",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:calendar-month",
        value_fn=_energy_iq_month_total,
    ),
    TadoXHomeSensorEntityDescription(
        key="heat_pump_cop_heating",
        translation_key="heat_pump_cop_heating",
        icon="mdi:heat-pump",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _energy_iq_cop(data, "heating"),
    ),
    TadoXHomeSensorEntityDescription(
        key="heat_pump_cop_hot_water",
        translation_key="heat_pump_cop_hot_water",
        icon="mdi:water-boiler",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: _energy_iq_cop(data, "domesticHotWater"),
    ),
)

WEATHER_SENSORS: tuple[TadoXWeatherSensorEntityDescription, ...] = (
    TadoXWeatherSensorEntityDescription(
        key="outdoor_temperature",
        translation_key="outdoor_temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda weather: weather.outdoor_temperature,
    ),
    TadoXWeatherSensorEntityDescription(
        key="solar_intensity",
        translation_key="solar_intensity",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:white-balance-sunny",
        value_fn=lambda weather: weather.solar_intensity,
    ),
    TadoXWeatherSensorEntityDescription(
        key="weather_state",
        translation_key="weather_state",
        device_class=SensorDeviceClass.ENUM,
        options=["SUN", "SUNNY", "CLOUDY", "CLOUDY_PARTLY", "CLOUDY_MOSTLY", "SCATTERED_RAIN", "NIGHT_CLEAR", "NIGHT_CLOUDY", "RAIN", "DRIZZLE", "SNOW", "SCATTERED_SNOW", "FOGGY", "THUNDERSTORMS", "WINDY", "HAIL", "RAIN_HAIL", "RAIN_SNOW", "SCATTERED_RAIN_SNOW", "FREEZING"],
        icon="mdi:weather-partly-cloudy",
        value_fn=lambda weather: weather.weather_state,
    ),
)

AIR_COMFORT_SENSORS: tuple[TadoXAirComfortSensorEntityDescription, ...] = (
    TadoXAirComfortSensorEntityDescription(
        key="air_freshness",
        translation_key="air_freshness",
        device_class=SensorDeviceClass.ENUM,
        options=["humid", "comfy", "dry"],
        icon="mdi:air-filter",
        value_fn=lambda ac: ac.freshness.lower() if ac.freshness else None,
    ),
    TadoXAirComfortSensorEntityDescription(
        key="comfort_level",
        translation_key="comfort_level",
        device_class=SensorDeviceClass.ENUM,
        options=["cold", "comfy", "warm"],
        icon="mdi:thermometer-lines",
        value_fn=lambda ac: ac.comfort_level.lower() if ac.comfort_level else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Tado X sensor entities."""
    coordinator: TadoXDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = []

    # Add home-level sensors (API monitoring)
    for description in HOME_SENSORS:
        if description.key.startswith("heat_pump_") and not coordinator.enable_heat_pump:
            continue
        if description.key.startswith("energy_iq_") and not coordinator.enable_energy_iq:
            continue
        if description.key.startswith("heat_pump_") or description.key.startswith("energy_iq_"):
            entities.append(TadoXHeatPumpSensor(coordinator, description))
        else:
            entities.append(TadoXHomeSensor(coordinator, description))

    # Add weather sensors (only if feature is enabled)
    if coordinator.enable_weather:
        for description in WEATHER_SENSORS:
            entities.append(TadoXWeatherSensor(coordinator, description))

    # Add room sensors
    for room_id in coordinator.data.rooms:
        for description in ROOM_SENSORS:
            # Skip heating_time_today if running times feature is disabled
            if description.key == "heating_time_today" and not coordinator.enable_running_times:
                continue
            entities.append(TadoXRoomSensor(coordinator, room_id, description))

    # Add device sensors (for devices with batteries - valves and sensors)
    for device in coordinator.data.devices.values():
        if device.battery_state:  # Only devices with batteries
            for description in DEVICE_SENSORS:
                # Skip device temperature for sensors that don't have it
                if description.key == "device_temperature" and device.temperature_measured is None:
                    continue
                # Skip temperature offset for devices that don't support it (Bridge)
                if description.key == "temperature_offset" and device.temperature_offset is None:
                    continue
                entities.append(TadoXDeviceSensor(coordinator, device.serial_number, description))

    # Add air comfort sensors (per room) - only if feature is enabled
    if coordinator.enable_air_comfort:
        for room_id in coordinator.data.rooms:
            for description in AIR_COMFORT_SENSORS:
                entities.append(TadoXAirComfortSensor(coordinator, room_id, description))

    async_add_entities(entities)


class TadoXHomeSensor(CoordinatorEntity[TadoXDataUpdateCoordinator], SensorEntity):
    """Tado X home sensor entity."""

    _attr_has_entity_name = True
    entity_description: TadoXHomeSensorEntityDescription

    def __init__(
        self,
        coordinator: TadoXDataUpdateCoordinator,
        description: TadoXHomeSensorEntityDescription,
    ) -> None:
        """Initialize home sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.data.home_id}_{description.key}"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, str(self.coordinator.data.home_id))},
            name=f"{self.coordinator.data.home_name} Home",
            manufacturer="Tado",
        )

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose raw telemetry for fields not yet promoted to entities."""
        attributes: dict[str, Any] = {}
        if self.entity_description.key == "heat_pump_electric_power":
            if self.coordinator.enable_heat_pump:
                attributes["heat_pump_state"] = self.coordinator.data.heat_pump_state
                attributes["heat_pump"] = self.coordinator.data.heat_pump
            if self.coordinator.enable_energy_iq:
                attributes["energy_iq"] = self.coordinator.data.energy_iq
        return attributes

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()


class TadoXHeatPumpSensor(TadoXHomeSensor):
    """Heat-pump and Energy IQ sensor attached to the synthetic CK04 device."""

    @property
    def device_info(self) -> DeviceInfo:
        """Return the Heat Pump Optimizer X device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.coordinator.data.home_id}_ck04")},
            name="Heat Pump Optimizer X",
            manufacturer="Tado",
            model="CK04",
            via_device=(DOMAIN, str(self.coordinator.data.home_id)),
        )


class TadoXWeatherSensor(CoordinatorEntity[TadoXDataUpdateCoordinator], SensorEntity):
    """Tado X weather sensor entity."""

    _attr_has_entity_name = True
    entity_description: TadoXWeatherSensorEntityDescription

    def __init__(
        self,
        coordinator: TadoXDataUpdateCoordinator,
        description: TadoXWeatherSensorEntityDescription,
    ) -> None:
        """Initialize weather sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.data.home_id}_weather_{description.key}"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, str(self.coordinator.data.home_id))},
            name=f"{self.coordinator.data.home_name} Home",
            manufacturer="Tado",
        )

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        weather = self.coordinator.data.weather
        if not weather:
            return None
        return self.entity_description.value_fn(weather)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()


class TadoXRoomSensor(CoordinatorEntity[TadoXDataUpdateCoordinator], SensorEntity):
    """Tado X room sensor entity."""

    _attr_has_entity_name = True
    entity_description: TadoXRoomSensorEntityDescription

    def __init__(
        self,
        coordinator: TadoXDataUpdateCoordinator,
        room_id: int,
        description: TadoXRoomSensorEntityDescription,
    ) -> None:
        """Initialize the sensor entity."""
        super().__init__(coordinator)
        self._room_id = room_id
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.home_id}_{room_id}_{description.key}"

    @property
    def _room(self) -> TadoXRoom | None:
        """Get the room data."""
        return self.coordinator.data.rooms.get(self._room_id)

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        room = self._room
        room_name = room.name if room else f"Room {self._room_id}"

        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.coordinator.home_id}_{self._room_id}")},
            name=room_name,
            manufacturer="Tado",
            model="Tado X Room",
            via_device=(DOMAIN, str(self.coordinator.home_id)),
        )

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        room = self._room
        if not room:
            return None
        return self.entity_description.value_fn(room)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()


class TadoXDeviceSensor(CoordinatorEntity[TadoXDataUpdateCoordinator], SensorEntity):
    """Tado X device sensor entity."""

    _attr_has_entity_name = True
    entity_description: TadoXDeviceSensorEntityDescription

    def __init__(
        self,
        coordinator: TadoXDataUpdateCoordinator,
        serial_number: str,
        description: TadoXDeviceSensorEntityDescription,
    ) -> None:
        """Initialize the sensor entity."""
        super().__init__(coordinator)
        self._serial_number = serial_number
        self.entity_description = description
        self._attr_unique_id = f"{serial_number}_{description.key}"

    @property
    def _device(self) -> TadoXDevice | None:
        """Get the device data."""
        return self.coordinator.data.devices.get(self._serial_number)

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        device = self._device
        if not device:
            return DeviceInfo(
                identifiers={(DOMAIN, self._serial_number)},
            )

        # French names for device types (used in device name)
        device_type_names_fr = {
            "VA04": "Vanne",
            "SU04": "Capteur Temp",
            "TR04": "Récepteur",  # Wireless Receiver X
            "RU04": "Thermostat",  # Wired Smart Thermostat X
            "IB02": "Bridge X",
        }

        # English model names (used in device model field)
        device_type_models = {
            "VA04": "Radiator Valve X",
            "SU04": "Temperature Sensor X",
            "TR04": "Wireless Receiver X",
            "RU04": "Wired Smart Thermostat X",
            "IB02": "Bridge X",
        }

        # Determine via_device - link to room if device has one, otherwise to home
        via_device_id = (
            (DOMAIN, f"{self.coordinator.home_id}_{device.room_id}")
            if device.room_id
            else (DOMAIN, str(self.coordinator.home_id))
        )

        # Generate device name with room name and numbering
        base_name = device_type_names_fr.get(device.device_type, device.device_type)

        if device.room_id and device.room_name:
            # Count devices of same type in same room to determine numbering
            same_type_in_room = sorted([
                d.serial_number for d in self.coordinator.data.devices.values()
                if d.room_id == device.room_id and d.device_type == device.device_type
            ])

            if len(same_type_in_room) > 1:
                # Multiple devices of same type - add number
                device_number = same_type_in_room.index(self._serial_number) + 1
                device_name = f"{base_name} {device_number} - {device.room_name}"
            else:
                # Only one device of this type - no number needed
                device_name = f"{base_name} - {device.room_name}"
        else:
            # No room - use serial number suffix (e.g., Bridge)
            device_name = f"{base_name} ({self._serial_number[-4:]})"

        return DeviceInfo(
            identifiers={(DOMAIN, self._serial_number)},
            name=device_name,
            manufacturer="Tado",
            model=device_type_models.get(device.device_type, device.device_type),
            sw_version=device.firmware_version,
            via_device=via_device_id,
        )

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        device = self._device
        if not device:
            return None
        return self.entity_description.value_fn(device)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()


class TadoXAirComfortSensor(CoordinatorEntity[TadoXDataUpdateCoordinator], SensorEntity):
    """Tado X air comfort sensor entity."""

    _attr_has_entity_name = True
    entity_description: TadoXAirComfortSensorEntityDescription

    def __init__(
        self,
        coordinator: TadoXDataUpdateCoordinator,
        room_id: int,
        description: TadoXAirComfortSensorEntityDescription,
    ) -> None:
        """Initialize the air comfort sensor entity."""
        super().__init__(coordinator)
        self._room_id = room_id
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.home_id}_{room_id}_{description.key}"

    @property
    def _air_comfort(self) -> TadoXRoomAirComfort | None:
        """Get the air comfort data for this room."""
        return self.coordinator.data.air_comfort.get(self._room_id)

    @property
    def _room(self) -> TadoXRoom | None:
        """Get the room data."""
        return self.coordinator.data.rooms.get(self._room_id)

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        room = self._room
        room_name = room.name if room else f"Room {self._room_id}"

        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.coordinator.home_id}_{self._room_id}")},
            name=room_name,
            manufacturer="Tado",
            model="Tado X Room",
            via_device=(DOMAIN, str(self.coordinator.home_id)),
        )

    @property
    def native_value(self) -> Any:
        """Return the sensor value."""
        air_comfort = self._air_comfort
        if not air_comfort:
            return None
        return self.entity_description.value_fn(air_comfort)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()
