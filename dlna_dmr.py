"""
Support for DLNA DMR (Device Media Renderer)
Most likely your TV
"""
import asyncio
import functools
import logging
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import timedelta

import aiohttp
import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.components.http import (
    request_handler_factory, HomeAssistantView)
from homeassistant.components.media_player import (
    SUPPORT_PLAY, SUPPORT_PAUSE, SUPPORT_STOP,
    SUPPORT_VOLUME_MUTE, SUPPORT_VOLUME_SET,
    SUPPORT_PREVIOUS_TRACK, SUPPORT_NEXT_TRACK,
    MediaPlayerDevice,
    PLATFORM_SCHEMA)
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STOP,
    CONF_URL, CONF_NAME,
    STATE_OFF, STATE_ON, STATE_IDLE, STATE_PLAYING, STATE_PAUSED)
from homeassistant.core import HomeAssistant

from .upnp_client import UpnpFactory


REQUIREMENTS = []

DEFAULT_NAME = 'DLNA_DMR'
CONF_MAX_VOLUME = 'max_volume'

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Required(CONF_URL): cv.string,
    vol.Optional(CONF_NAME): cv.string,
    vol.Optional(CONF_MAX_VOLUME): cv.positive_int,
})

NS = {
    'didl_lite': 'urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/',
    'dc': 'http://purl.org/dc/elements/1.1/',
}

SERVICE_TYPES = {
    'RC': 'urn:schemas-upnp-org:service:RenderingControl:1',
    'CM': 'urn:schemas-upnp-org:service:ConnectionManager:1',
    'AVT': 'urn:schemas-upnp-org:service:AVTransport:1',
}


_LOGGER = logging.getLogger(__name__)


# region Decorators
def requires_connection(default=None):
    """Return default if DlnaDmrDevice is not connected."""

    def call_wrapper(func):
        """Call wrapper for decorator"""

        @functools.wraps(func)
        def wrapper(self, *args, **kwargs):
            """
            Require device is connected.
            If device is not connected, None is returned.
            """

            # _LOGGER.debug('needs_connection(): %s.%s', self, func.__name__)
            if not self.is_connected():
                # _LOGGER.debug('needs_connection(): %s.%s: not connected', self, func.__name__)
                return default
            return func(self, *args, **kwargs)
        return wrapper
    return call_wrapper


def requires_action(service_name, action_name, value_not_connected=None):
    """Raise NotImplemented() if connected but service/action not available."""

    def call_wrapper(func):
        """Call wrapper for decorator"""

        @functools.wraps(func)
        def wrapper(self, *args, **kwargs):
            """
            Require device is connected and has service/action.
            If device is not connected, value_not_connected is returned.
            """

            # _LOGGER.debug('needs_action(): %s.%s', self, func.__name__)
            if not self.is_connected():
                # _LOGGER.debug('needs_action(): %s.%s: not connected', self, func.__name__)
                return value_not_connected

            # pylint: disable=protected-access
            if service_name not in self._services:
                _LOGGER.error('requires_action(): %s.%s: no service: %s',
                              self, func.__name__, service_name)
                raise NotImplementedError()

            # pylint: disable=protected-access
            service = self._services[service_name]
            action = service.action(action_name)
            if not action:
                _LOGGER.error('requires_action(): %s.%s: no action: %s.%s',
                              self, func.__name__, service_name, action_name)
                raise NotImplementedError()
            return func(self, service, action, *args, **kwargs)

        return wrapper

    return call_wrapper


def requires_state_variable(service_name, state_variable_name, value_not_connected=None):
    """Raise NotImplemented() if connected but service/state_variable not available."""

    def call_wrapper(func):
        """Call wrapper for decorator."""

        @functools.wraps(func)
        def wrapper(self, *args, **kwargs):
            """
            Require device is connected and has service/state_variable.
            If device is not connected, value_not_connected is returned.
            """

            # _LOGGER.debug('needs_service(): %s.%s', self, func.__name__)
            if not self.is_connected():
                # _LOGGER.debug('needs_service(): %s.%s: not connected', self, func.__name__)
                return value_not_connected

            # pylint: disable=protected-access
            if service_name not in self._services:
                _LOGGER.error('requires_state_variable(): %s.%s: no service: %s',
                              self, func.__name__, service_name)
                raise NotImplementedError()

            # pylint: disable=protected-access
            service = self._services[service_name]
            state_var = service.state_variable(state_variable_name)
            if not state_var:
                _LOGGER.error('requires_state_variable(): %s.%s: no state_variable: %s.%s',
                              self, func.__name__, service_name, state_variable_name)
                raise NotImplementedError()
            return func(self, service, state_var, *args, **kwargs)
        return wrapper
    return call_wrapper
# endregion


# region Home Assistant
def setup_platform(hass: HomeAssistant, config, add_devices, discovery_info=None):
    """Set up DLNA DMR platform."""
    _LOGGER.debug('dlna_dmr.setup_platform')

    if config.get(CONF_URL) is not None:
        url = config.get(CONF_URL)
        name = config.get(CONF_NAME)
    elif discovery_info is not None:
        url = discovery_info['ssdp_description']
        name = discovery_info['name']

    def start_notify_view():
        """Register notify view."""
        _LOGGER.debug('start_notify_view()')

        if 'notify_view' in hass.data.get(__name__, {}):
            return hass.data[__name__]['notify_view']

        view = UpnpNotifyView(hass.config.api.base_url)
        hass.data[__name__] = {
            'notify_view': view
        }
        hass.http.register_view(view)
        return view

    view = start_notify_view()
    device = DlnaDmrDevice(hass, url, name, view)

    _LOGGER.debug("Adding device: %s", device)
    add_devices([device])


class UpnpNotifyView(HomeAssistantView):
    """Callback view for UPnP NOTIFY messages"""

    url = '/api/dlna_dmr.notify'
    name = 'api:dlna_dmr.notify'
    requires_auth = False

    def __init__(self, hass_base_url):
        self._hass_base_url = hass_base_url
        self._registered_services = {}

    def register(self, router):
        """Register the view with a router."""
        handler = request_handler_factory(self, self.async_notify)
        router.add_route('notify', UpnpNotifyView.url, handler)

    @asyncio.coroutine
    def async_notify(self, request):
        """Callback method for NOTIFY requests."""
        _LOGGER.debug('%s.async_notify(): request: %s', self, request)

        if 'SID' not in request.headers:
            _LOGGER.error('%s.async_notify(): request without SID')
            return

        # find UpnpService by SID
        sid = request.headers['SID']
        if sid not in self._registered_services:
            _LOGGER.debug('%s.async_notify(): unknown SID: %s', self, sid)
            return

        service = self._registered_services[sid]
        _LOGGER.debug('%s.async_notify(): service: %s, sid: %s', self, service, sid)
        headers = request.headers
        body = yield from request.text()
        service.on_notify(headers, body)

    @property
    def callback_url(self):
        """Full URL to be called by device/service."""
        return urllib.parse.urljoin(self._hass_base_url, UpnpNotifyView.url)

    def register_service(self, sid, service):
        """
        Register a UpnpService under SID.
        To be called from UpnpService.async_subscribe().
        """
        if sid in self._registered_services:
            raise RuntimeError('SID {} already registered.'.format(sid))

        self._registered_services[sid] = service

    def unregister_service(self, sid):
        """Unregister service by SID."""
        if sid in self._registered_services:
            del self._registered_services[sid]


class DlnaDmrDevice(MediaPlayerDevice):
    """Representation of a DLNA DMR device."""

    def __init__(self, hass, url, name, callback_view, **additional_configuration):
        self.hass = hass
        self._url = url
        self._name = name
        self._callback_view = callback_view
        self._additional_configuration = additional_configuration

        self._services = {}

        hass.bus.listen_once(EVENT_HOMEASSISTANT_STOP, self._async_on_hass_stop)

    @asyncio.coroutine
    def _async_on_hass_stop(self, event):
        """Event handler on HASS stop."""
        _LOGGER.debug('%s._on_hass_stop(): %s', self, event)
        # UNSUBSCRIBE all services
        yield from self.async_disconnect()

    @asyncio.coroutine
    def async_connect(self):
        """
        Connect to device.
        This intializes all UpnpServices.
        """
        _LOGGER.debug('%s.async_connect()', self)
        if self._services:
            _LOGGER.debug('%s.async_connect(): already connected', self)
            return

        yield from self._async_init_services()

    def is_connected(self):
        """Device connected?"""
        # _LOGGER.debug('%s.is_connected(): %s', self, bool(self._services))
        return bool(self._services)

    @asyncio.coroutine
    def async_disconnect(self):
        """
        Disconnect from device.
        This removes all UpnpServices.
        """
        _LOGGER.debug('%s.async_disconnect()', self)

        for service in self._services.values():
            sid = service.subscription_sid
            if sid:
                self._callback_view.unregister_service(sid)
                yield from service.async_unsubscribe(True)

        self._services = {}

    @asyncio.coroutine
    def _async_ensure_connection(self):
        # _LOGGER.debug('%s._async_ensure_connection()', self)
        if not self.is_connected():
            yield from self.async_connect()
        return self.is_connected()

    @asyncio.coroutine
    def _async_init_services(self):
        """Fetch and init services."""
        _LOGGER.debug('%s._async_init_services()', self)
        factory = UpnpFactory(self.hass)
        try:
            name, services = yield from factory.async_create_services(self._url)
        except Exception as ex:  # pylint: disable=broad-except
            _LOGGER.debug('%s._init_services(): caught exception: %s', self, ex)
            self._services = {}
            return

        if self.name is None or self.name == DEFAULT_NAME:
            self._name = name

        # find required services
        self._services = {
            'RC': next(s for s in services
                       if s.service_type == SERVICE_TYPES['RC']),
            'CM': next(s for s in services
                       if s.service_type == SERVICE_TYPES['CM']),
        }

        # find optional services
        try:
            # AVTransport is optional
            self._services['AVT'] = next(s for s in services
                                         if s.service_type == SERVICE_TYPES['AVT'])
        except StopIteration:
            pass

        # subscribe services for events
        callback_url = self._callback_view.callback_url
        for service in self._services.values():
            service.on_state_variable_change = self.on_state_variable_change

            sid = yield from service.async_subscribe(callback_url)
            if sid:
                self._callback_view.register_service(sid, service)

    @asyncio.coroutine
    def async_update(self):
        """Retrieve the latest data."""
        # _LOGGER.debug('%s.async_update()', self)
        is_connected = yield from self._async_ensure_connection()
        if not is_connected:
            _LOGGER.debug('No connection')
            return

        # call GetTransportInfo/GetPositionInfo regularly
        try:
            if 'AVT' in self._services:
                # pylint: disable=no-value-for-parameter
                state = yield from self._async_poll_transport_info()

                if state == STATE_PLAYING or state == STATE_PAUSED:
                    # pylint: disable=no-value-for-parameter
                    yield from self._async_poll_position_info()
        except (asyncio.TimeoutError, aiohttp.ClientError):
            # be graceful, don't spam the error log when this is expected
            yield from self.async_disconnect()

    @asyncio.coroutine
    @requires_action('AVT', 'GetTransportInfo')
    # pylint: disable=arguments-differ
    def _async_poll_transport_info(self, service, action):
        """Update transport info from device."""
        _LOGGER.debug('%s._async_poll_transport_info()', self)
        result = yield from service.async_call_action(action, InstanceID=0)
        # _LOGGER.debug('Got result: %s', result)

        # set/update state_variable 'TransportState'
        state_var = service.state_variable('TransportState')
        old_value = state_var.value
        state_var.value = result['CurrentTransportState']

        if old_value != result['CurrentTransportState']:
            self.on_state_variable_change(service, [state_var])

        return self.state

    @asyncio.coroutine
    @requires_action('AVT', 'GetPositionInfo')
    # pylint: disable=arguments-differ
    def _async_poll_position_info(self, service, action):
        """Update position info"""
        _LOGGER.debug('%s._async_poll_position_info()', self)

        result = yield from service.async_call_action(action, InstanceID=0)
        # _LOGGER.debug('Got result: %s', result)

        track_duration = service.state_variable('CurrentTrackDuration')
        track_duration.value = result['TrackDuration']

        time_position = service.state_variable('RelativeTimePosition')
        time_position.value = result['RelTime']

        self.on_state_variable_change(service, [track_duration, time_position])

    def on_state_variable_change(self, service, state_variables):
        """State variable(s) changed, let homeassistant know"""
        self.schedule_update_ha_state()

    @property
    @requires_connection(0)
    def supported_features(self):
        """Flag media player features that are supported."""
        supported_features = 0

        if 'RC' in self._services:
            service = self._services['RC']
            if service.state_variable('Mute'):
                supported_features |= SUPPORT_VOLUME_MUTE
            if service.state_variable('Volume'):
                supported_features |= SUPPORT_VOLUME_SET

        if 'AVT' in self._services:
            service = self._services['AVT']
            state_var = service.state_variable('CurrentTransportActions')
            if state_var:
                value = state_var.value or ''
                actions = value.split(',')
                if 'Play' in actions:
                    supported_features |= SUPPORT_PLAY
                if 'Stop' in actions:
                    supported_features |= SUPPORT_STOP
                if 'Pause' in actions:
                    supported_features |= SUPPORT_PAUSE

            current_track_var = service.state_variable('CurrentTrack')
            num_tracks_var = service.state_variable('NumberOfTracks')
            if current_track_var and num_tracks_var and \
               current_track_var.value is not None and num_tracks_var.value is not None:
                current_track = current_track_var.value
                num_tracks = num_tracks_var.value
                if current_track > 1:
                    supported_features |= SUPPORT_PREVIOUS_TRACK

                if num_tracks > current_track:
                    supported_features |= SUPPORT_NEXT_TRACK

        return supported_features

    @property
    @requires_state_variable('RC', 'Volume')
    # pylint: disable=arguments-differ
    def volume_level(self, service, state_variable):
        """Volume level of the media player (0..1)."""
        value = state_variable.value
        if value is None:
            _LOGGER.debug('%s.volume_level(): Got no value', self)
            return None

        override_max = self._additional_configuration.get('max_volume', None)
        max_value = override_max or state_variable.max_value or 100
        return min(value / max_value, 1.0)

    @asyncio.coroutine
    @requires_action('RC', 'SetVolume')
    # pylint: disable=arguments-differ
    def async_set_volume_level(self, service, action, volume):
        """Set volume level, range 0..1."""
        _LOGGER.debug('%s.async_set_volume_level(): %s', self, volume)
        state_variable = action.argument('DesiredVolume').related_state_variable
        min_ = state_variable.min_value or 0
        override_max = self._additional_configuration.get('max_volume', None)
        max_ = override_max or state_variable.max_value or 100
        desired_volume = int(min_ + volume * (max_ - min_))

        yield from service.async_call_action(action, InstanceID=0, Channel='Master',
                                             DesiredVolume=desired_volume)

    @property
    @requires_state_variable('RC', 'Mute')
    # pylint: disable=arguments-differ
    def is_volume_muted(self, service, state_variable):
        """Boolean if volume is currently muted."""
        return state_variable.value

    @asyncio.coroutine
    @requires_action('RC', 'SetMute')
    # pylint: disable=arguments-differ
    def async_mute_volume(self, service, action, mute):
        """Mute the volume."""
        _LOGGER.debug('%s.async_mute_volume(): %s', self, mute)
        desired_mute = bool(mute)
        yield from service.async_call_action(action, InstanceID=0, Channel='Master',
                                             DesiredMute=desired_mute)

    @asyncio.coroutine
    @requires_action('AVT', 'Pause')
    # pylint: disable=arguments-differ
    def async_media_pause(self, service, action):
        """Send pause command."""
        _LOGGER.debug('%s.async_media_pause()', self)
        yield from service.async_call_action(action, InstanceID=0)

    @asyncio.coroutine
    @requires_action('AVT', 'Play')
    # pylint: disable=arguments-differ
    def async_media_play(self, service, action):
        """Send play command."""
        _LOGGER.debug('%s.async_media_play()', self)
        yield from service.async_call_action(action, InstanceID=0, Speed='1')

    @asyncio.coroutine
    @requires_action('AVT', 'Stop')
    # pylint: disable=arguments-differ
    def async_media_stop(self, service, action):
        """Send stop command."""
        _LOGGER.debug('%s.async_media_stop()', self)
        yield from service.async_call_action(action, InstanceID=0)

    @asyncio.coroutine
    @requires_action('AVT', 'Previous')
    # pylint: disable=arguments-differ
    def async_media_previous_track(self, service, action):
        """Send previous track command."""
        _LOGGER.debug('%s.async_media_previous_track()', self)
        yield from service.async_call_action(action, InstanceID=0)

    @asyncio.coroutine
    @requires_action('AVT', 'Next')
    # pylint: disable=arguments-differ
    def async_media_next_track(self, service, action):
        """Send next track command."""
        _LOGGER.debug('%s.async_media_next_track()', self)
        yield from service.async_call_action(action, InstanceID=0)

    @property
    @requires_state_variable('AVT', 'CurrentTrackMetaData')
    # pylint: disable=arguments-differ
    def media_title(self, _, state_variable):
        """Title of current playing media."""
        xml = state_variable.value
        if not xml:
            return

        root = ET.fromstring(xml)
        title_xml = root.find('.//dc:title', NS)
        if title_xml is None:
            return None

        return title_xml.text

    @property
    @requires_state_variable('AVT', 'CurrentTrackMetaData')
    # pylint: disable=arguments-differ
    def media_image_url(self, service, state_variable):
        """Image url of current playing media."""
        xml = state_variable.value
        if not xml:
            return

        root = ET.fromstring(xml)
        for res in root.findall('.//didl_lite:res', NS):
            protocol_info = res.attrib.get('protocolInfo') or ''
            if protocol_info.startswith('http-get:*:image/'):
                url = protocol_info.text
                _LOGGER.debug('%s.media_image_url(): Url: %s', self, url)
                return url

        return None

    @property
    def state(self):
        """State of the player."""
        if not self._services:
            return STATE_OFF

        if 'AVT' not in self._services:
            return STATE_ON
        service = self._services['AVT']
        if 'TransportState' not in service.state_variables:
            return STATE_ON

        service = self._services['AVT']
        transport_state = service.state_variable('TransportState')
        if transport_state.value == 'PLAYING':
            return STATE_PLAYING
        elif transport_state.value == 'PAUSED_PLAYBACK':
            return STATE_PAUSED

        return STATE_IDLE

    @property
    @requires_state_variable('AVT', 'CurrentTrackDuration')
    # pylint: disable=arguments-differ
    def media_duration(self, service, state_variable):
        """Duration of current playing media in seconds."""
        if state_variable is None or state_variable.value is None:
            return None

        split = [int(v) for v in re.findall(r"[\w']+", state_variable.value)]
        delta = timedelta(hours=split[0], minutes=split[1], seconds=split[2])
        return delta.seconds

    @property
    @requires_state_variable('AVT', 'RelativeTimePosition')
    # pylint: disable=arguments-differ
    def media_position(self, service, state_variable):
        """Position of current playing media in seconds."""
        if state_variable is None or state_variable.value is None:
            return None

        split = [int(v) for v in re.findall(r"[\w']+", state_variable.value)]
        delta = timedelta(hours=split[0], minutes=split[1], seconds=split[2])
        return delta.seconds

    @property
    @requires_state_variable('AVT', 'RelativeTimePosition')
    # pylint: disable=arguments-differ
    def media_position_updated_at(self, service, state_variable):
        """When was the position of the current playing media valid.

        Returns value from homeassistant.util.dt.utcnow().
        """
        return state_variable.updated_at

    @property
    def name(self):
        """Return the name of the device."""
        return self._name

    @property
    def unique_id(self) -> str:
        """Return an unique ID."""
        return "{}.{}".format(self.__class__.__name__, self._url)

    def __str__(self):
        return "<DlnaDmrDevice('{}')>".format(self._url)
# endregion
