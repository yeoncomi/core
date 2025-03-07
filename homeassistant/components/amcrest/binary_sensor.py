"""Support for Amcrest IP camera binary sensors."""
from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
import logging
from typing import TYPE_CHECKING

from amcrest import AmcrestError
import voluptuous as vol

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import CONF_BINARY_SENSORS, CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.util import Throttle

from .const import (
    BINARY_SENSOR_SCAN_INTERVAL_SECS,
    DATA_AMCREST,
    DEVICES,
    SERVICE_EVENT,
    SERVICE_UPDATE,
)
from .helpers import log_update_error, service_signal

if TYPE_CHECKING:
    from . import AmcrestDevice


@dataclass
class AmcrestSensorEntityDescription(BinarySensorEntityDescription):
    """Describe Amcrest sensor entity."""

    event_code: str | None = None
    should_poll: bool = False


_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=BINARY_SENSOR_SCAN_INTERVAL_SECS)
_ONLINE_SCAN_INTERVAL = timedelta(seconds=60 - BINARY_SENSOR_SCAN_INTERVAL_SECS)

_AUDIO_DETECTED_KEY = "audio_detected"
_AUDIO_DETECTED_POLLED_KEY = "audio_detected_polled"
_AUDIO_DETECTED_NAME = "Audio Detected"
_AUDIO_DETECTED_EVENT_CODE = "AudioMutation"

_CROSSLINE_DETECTED_KEY = "crossline_detected"
_CROSSLINE_DETECTED_POLLED_KEY = "crossline_detected_polled"
_CROSSLINE_DETECTED_NAME = "CrossLine Detected"
_CROSSLINE_DETECTED_EVENT_CODE = "CrossLineDetection"

_MOTION_DETECTED_KEY = "motion_detected"
_MOTION_DETECTED_POLLED_KEY = "motion_detected_polled"
_MOTION_DETECTED_NAME = "Motion Detected"
_MOTION_DETECTED_EVENT_CODE = "VideoMotion"

_ONLINE_KEY = "online"

_DOORBELL_KEY = "doorbell"
_DOORBELL_NAME = "Doorbell Button"
_DOORBELL_EVENT_CODE = "CallNoAnswered"

BINARY_SENSORS: tuple[AmcrestSensorEntityDescription, ...] = (
    AmcrestSensorEntityDescription(
        key=_AUDIO_DETECTED_KEY,
        name=_AUDIO_DETECTED_NAME,
        device_class=BinarySensorDeviceClass.SOUND,
        event_code=_AUDIO_DETECTED_EVENT_CODE,
    ),
    AmcrestSensorEntityDescription(
        key=_AUDIO_DETECTED_POLLED_KEY,
        name=_AUDIO_DETECTED_NAME,
        device_class=BinarySensorDeviceClass.SOUND,
        event_code=_AUDIO_DETECTED_EVENT_CODE,
        should_poll=True,
    ),
    AmcrestSensorEntityDescription(
        key=_CROSSLINE_DETECTED_KEY,
        name=_CROSSLINE_DETECTED_NAME,
        device_class=BinarySensorDeviceClass.MOTION,
        event_code=_CROSSLINE_DETECTED_EVENT_CODE,
    ),
    AmcrestSensorEntityDescription(
        key=_CROSSLINE_DETECTED_POLLED_KEY,
        name=_CROSSLINE_DETECTED_NAME,
        device_class=BinarySensorDeviceClass.MOTION,
        event_code=_CROSSLINE_DETECTED_EVENT_CODE,
        should_poll=True,
    ),
    AmcrestSensorEntityDescription(
        key=_MOTION_DETECTED_KEY,
        name=_MOTION_DETECTED_NAME,
        device_class=BinarySensorDeviceClass.MOTION,
        event_code=_MOTION_DETECTED_EVENT_CODE,
    ),
    AmcrestSensorEntityDescription(
        key=_MOTION_DETECTED_POLLED_KEY,
        name=_MOTION_DETECTED_NAME,
        device_class=BinarySensorDeviceClass.MOTION,
        event_code=_MOTION_DETECTED_EVENT_CODE,
        should_poll=True,
    ),
    AmcrestSensorEntityDescription(
        key=_ONLINE_KEY,
        name="Online",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        should_poll=True,
    ),
    AmcrestSensorEntityDescription(
        key=_DOORBELL_KEY,
        name=_DOORBELL_NAME,
        device_class=BinarySensorDeviceClass.OCCUPANCY,
        event_code=_DOORBELL_EVENT_CODE,
    ),
)
BINARY_SENSOR_KEYS = [description.key for description in BINARY_SENSORS]
_EXCLUSIVE_OPTIONS = [
    {_AUDIO_DETECTED_KEY, _AUDIO_DETECTED_POLLED_KEY},
    {_MOTION_DETECTED_KEY, _MOTION_DETECTED_POLLED_KEY},
    {_CROSSLINE_DETECTED_KEY, _CROSSLINE_DETECTED_POLLED_KEY},
]

_UPDATE_MSG = "Updating %s binary sensor"


def check_binary_sensors(value: list[str]) -> list[str]:
    """Validate binary sensor configurations."""
    for exclusive_options in _EXCLUSIVE_OPTIONS:
        if len(set(value) & exclusive_options) > 1:
            raise vol.Invalid(
                f"must contain at most one of {', '.join(exclusive_options)}."
            )
    return value


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up a binary sensor for an Amcrest IP Camera."""
    if discovery_info is None:
        return

    name = discovery_info[CONF_NAME]
    device = hass.data[DATA_AMCREST][DEVICES][name]
    binary_sensors = discovery_info[CONF_BINARY_SENSORS]
    async_add_entities(
        [
            AmcrestBinarySensor(name, device, entity_description)
            for entity_description in BINARY_SENSORS
            if entity_description.key in binary_sensors
        ],
        True,
    )


class AmcrestBinarySensor(BinarySensorEntity):
    """Binary sensor for Amcrest camera."""

    def __init__(
        self,
        name: str,
        device: AmcrestDevice,
        entity_description: AmcrestSensorEntityDescription,
    ) -> None:
        """Initialize entity."""
        self._signal_name = name
        self._api = device.api
        self._channel = device.channel
        self.entity_description: AmcrestSensorEntityDescription = entity_description

        self._attr_name = f"{name} {entity_description.name}"
        self._attr_should_poll = entity_description.should_poll

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.entity_description.key == _ONLINE_KEY or self._api.available

    async def async_update(self) -> None:
        """Update entity."""
        if self.entity_description.key == _ONLINE_KEY:
            await self._async_update_online()
        else:
            await self._async_update_others()

    @Throttle(_ONLINE_SCAN_INTERVAL)
    async def _async_update_online(self) -> None:
        if not (self._api.available or self.is_on):
            return
        _LOGGER.debug(_UPDATE_MSG, self.name)

        if self._api.available:
            # Send a command to the camera to test if we can still communicate with it.
            # Override of Http.async_command() in __init__.py will set self._api.available
            # accordingly.
            with suppress(AmcrestError):
                await self._api.async_current_time
                await self._async_update_unique_id()
        self._attr_is_on = self._api.available

    async def _async_update_others(self) -> None:
        if not self.available:
            return
        _LOGGER.debug(_UPDATE_MSG, self.name)

        try:
            await self._async_update_unique_id()
        except AmcrestError as error:
            log_update_error(_LOGGER, "update", self.name, "binary sensor", error)
            return

        if (event_code := self.entity_description.event_code) is None:
            _LOGGER.error("Binary sensor %s event code not set", self.name)
            return

        try:
            self._attr_is_on = (
                len(await self._api.async_event_channels_happened(event_code)) > 0
            )
        except AmcrestError as error:
            log_update_error(_LOGGER, "update", self.name, "binary sensor", error)
            return

    async def _async_update_unique_id(self) -> None:
        """Set the unique id."""
        if self._attr_unique_id is None and (
            serial_number := await self._api.async_serial_number
        ):
            self._attr_unique_id = (
                f"{serial_number}-{self.entity_description.key}-{self._channel}"
            )

    @callback
    def async_on_demand_update_online(self) -> None:
        """Update state."""
        _LOGGER.debug(_UPDATE_MSG, self.name)
        self._attr_is_on = self._api.available
        self.async_write_ha_state()

    @callback
    def async_event_received(self, state: bool) -> None:
        """Update state from received event."""
        _LOGGER.debug(_UPDATE_MSG, self.name)
        self._attr_is_on = state
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Subscribe to signals."""
        if self.entity_description.key == _ONLINE_KEY:
            self.async_on_remove(
                async_dispatcher_connect(
                    self.hass,
                    service_signal(SERVICE_UPDATE, self._signal_name),
                    self.async_on_demand_update_online,
                )
            )
        else:
            self.async_on_remove(
                async_dispatcher_connect(
                    self.hass,
                    service_signal(SERVICE_UPDATE, self._signal_name),
                    self.async_write_ha_state,
                )
            )

        if (
            self.entity_description.event_code
            and not self.entity_description.should_poll
        ):
            self.async_on_remove(
                async_dispatcher_connect(
                    self.hass,
                    service_signal(
                        SERVICE_EVENT,
                        self._signal_name,
                        self.entity_description.event_code,
                    ),
                    self.async_event_received,
                )
            )
