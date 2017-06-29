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
import async_timeout
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
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util.dt import utcnow


REQUIREMENTS = []

DEFAULT_NAME = 'DLNA_DMR'
CONF_MAX_VOLUME = 'max_volume'

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Required(CONF_URL): cv.string,
    vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    vol.Optional(CONF_MAX_VOLUME): cv.positive_int,
})

NS = {
    'device': 'urn:schemas-upnp-org:device-1-0',
    'service': 'urn:schemas-upnp-org:service-1-0',
    'event': 'urn:schemas-upnp-org:event-1-0',
    'didl_lite': 'urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/',
    'dc': 'http://purl.org/dc/elements/1.1/',

    'soap_envelope': 'http://schemas.xmlsoap.org/soap/envelope/',
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


# pylint: disable=abstract-method,too-many-public-methods
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
        _LOGGER.debug('%s._on_hass_stop()', self)
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
        except Exception as ex:
            _LOGGER.debug('%s._init_services(): caught exception: %s', self, ex)
            self._services = {}
            return

        if self.name is None:
            self._name = name

        # find required services
        self._services = {
            'RC': next(s for s in services
                       if s.service_type == 'urn:schemas-upnp-org:service:RenderingControl:1'),
            'CM': next(s for s in services
                       if s.service_type == 'urn:schemas-upnp-org:service:ConnectionManager:1'),
        }

        # find optional services
        try:
            # AVTransport is optional
            self._services['AVT'] = next(s for s in services
                                         if s.service_type == 'urn:schemas-upnp-org:service:AVTransport:1')
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
        _LOGGER.debug('Got result: %s', result)

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
        _LOGGER.debug('Got result: %s', result)

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

        yield from service.async_call_action(action, InstanceID=0, Channel='Master', DesiredVolume=desired_volume)

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
        yield from service.async_call_action(action, InstanceID=0, Channel='Master', DesiredMute=desired_mute)

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


# region UPnP
class UpnpService(object):
    """UPnP Service representation."""

    def __init__(self, hass, service_description, device_url, state_variables, actions):
        self.hass = hass
        self._service_description = service_description
        self._device_url = device_url
        self._state_variables = state_variables
        self._actions = actions

        self._subscription_sid = None
        self._on_state_variable_change = None

    @property
    def service_type(self):
        """Get service type for this UpnpService."""
        return self._service_description['service_type']

    @property
    def service_id(self):
        """Get service ID for this UpnpService."""
        return self._service_description['service_id']

    @property
    def scpd_url(self):
        """Get full SCPD-url for this UpnpService."""
        return urllib.parse.urljoin(self._device_url, self._service_description['scpd_url'])

    @property
    def control_url(self):
        """Get full control-url for this UpnpService."""
        return urllib.parse.urljoin(self._device_url, self._service_description['control_url'])

    @property
    def event_sub_url(self):
        """Get full event sub-url for this UpnpService."""
        return urllib.parse.urljoin(self._device_url, self._service_description['event_sub_url'])

    @property
    def state_variables(self):
        """Get All UpnpStateVariables for this UpnpService."""
        return self._state_variables

    def state_variable(self, name):
        """Get UPnpStateVariable by name."""
        return self.state_variables.get(name, None)

    @property
    def actions(self):
        """Get All UpnpActions for this UpnpService."""
        return self._actions

    def action(self, name):
        """Get UPnpAction by name."""
        return self.actions.get(name, None)

    @asyncio.coroutine
    def async_call_action(self, action, **kwargs):
        """
        Call a UpnpAction.
        Parameters are in Python-values and coerced automatically to UPnP values.
        """
        if isinstance(action, str):
            action = self.actions[action]

        _LOGGER.debug('Calling action: %s', action.name)
        # build request
        headers, body = action.create_request(self.control_url, self.service_type, **kwargs)
        # _LOGGER.debug('Request_body: %s', body)

        # do request
        status_code, response_headers, response_body =\
            yield from self._async_do_http_request('POST', self.control_url, headers, body)
        # _LOGGER.debug('Status: %s Response_body: %s', status_code, response_body)

        if status_code != 200:
            raise RuntimeError('Error during call_action')

        # parse results
        response_args = action.parse_response(self.service_type, response_headers, response_body)
        return response_args

    @asyncio.coroutine
    def _async_do_http_request(self, method, url, headers, body):
        websession = async_get_clientsession(self.hass)
        try:
            with async_timeout.timeout(5, loop=self.hass.loop):
                response = yield from websession.request(method, url, headers=headers, data=body)
                response_body = yield from response.text()
        except (asyncio.TimeoutError, aiohttp.ClientError) as ex:
            _LOGGER.debug("Error in %s.async_call_action(): %s", self, ex)
            raise

        return response.status, response.headers, response_body

    @property
    def subscription_sid(self):
        """Return our current subscription ID for events."""
        return self._subscription_sid

    @asyncio.coroutine
    def async_subscribe(self, callback_uri):
        """SUBSCRIBE for events on StateVariables."""
        if self._subscription_sid:
            raise RuntimeError('Already subscribed, unsubscribe first')

        headers = {
            'NT': 'upnp:event',
            'TIMEOUT': 'Second-infinite',
            'Host': urllib.parse.urlparse(self.event_sub_url).netloc,
            'CALLBACK': '<{}>'.format(callback_uri),
        }
        response_status, response_headers, _ = yield from self._async_do_http_request('SUBSCRIBE', self.event_sub_url, headers, '')

        if response_status != 200:
            _LOGGER.error('Did not receive 200, but %s', response_status)
            return

        if 'sid' not in response_headers:
            _LOGGER.error('Did not receive a "SID"')
            return

        subscription_sid = response_headers['sid']
        self._subscription_sid = subscription_sid
        _LOGGER.debug('%s.subscribe(): Got SID: %s', self, subscription_sid)
        return subscription_sid

    @asyncio.coroutine
    def async_unsubscribe(self, force=False):
        """UNSUBSCRIBE from events on StateVariables."""
        if not force and not self._subscription_sid:
            raise RuntimeError('Cannot unsubscribed, subscribe first')

        subscription_sid = self._subscription_sid
        if force:
            # we don't care what happens further, make sure we are unsubscribed
            self._subscription_sid = None

        headers = {
            'Host': urllib.parse.urlparse(self.event_sub_url).netloc,
            'SID': subscription_sid,
        }
        try:
            response_status, _, _ = yield from self._async_do_http_request('UNSUBSCRIBE', self.event_sub_url, headers, '')
        except (asyncio.TimeoutError, aiohttp.ClientError):
            if not force:
                raise
            return

        if response_status != 200:
            _LOGGER.error('Did not receive 200, but %s', response_status)
            return

        self._subscription_sid = None

    def on_notify(self, headers, body):
        """
        Callback for UpnpNotifyView.
        Parses the headers/body and sets UpnpStateVariables with new values.
        """
        notify_sid = headers.get('SID')
        if notify_sid != self._subscription_sid:
            # _LOGGER.debug('Received NOTIFY for unknown SID: %s, known SID: %s', notify_sid, self._subscription_sid)
            return

        el_root = ET.fromstring(body)
        el_last_change = el_root.find('.//LastChange')
        if el_last_change is None:
            _LOGGER.debug("Got NOTIFY without body, ignoring")
            return

        changed_state_variables = []
        el_event = ET.fromstring(el_last_change.text)
        for el_instance_id in el_event.findall('./'):
            for el_state_var in el_instance_id .findall('./'):
                name = el_state_var.tag.split('}')[1]
                value = el_state_var.get('val')

                state_var = self.state_variable(name)
                try:
                    state_var.upnp_value = value
                except vol.error.MultipleInvalid:
                    _LOGGER.error('Got invalid value for %s: %s', state_var, value)

                changed_state_variables.append(state_var)

                _LOGGER.debug('%s.on_notify(): set state var %s to %s', self, name, value)

        self.notify_changed_state_variables(changed_state_variables)

    def notify_changed_state_variables(self, changed_state_variables):
        """Callback on UpnpStateVariable.value changes."""
        if self._on_state_variable_change:
            self._on_state_variable_change(self, changed_state_variables)

    @property
    def on_state_variable_change(self):
        """Get callback for value changes."""
        return self._on_state_variable_change

    @on_state_variable_change.setter
    def on_state_variable_change(self, callback):
        """Set callback for value changes."""
        self._on_state_variable_change = callback

    def __str__(self):
        return "<UpnpService({0})>".format(self.service_id)

    def __repr__(self):
        return "<UpnpService({0})>".format(self.service_id)


class UpnpAction(object):
    """Representation of an Action"""

    class Argument(object):
        """Representation of an Argument of an Action"""

        def __init__(self, name, direction, related_state_variable):
            self.name = name
            self.direction = direction
            self.related_state_variable = related_state_variable
            self._value = None

        def validate_value(self, value):
            """Validate value against related UpnpStateVariable."""
            self.related_state_variable.validate_value(value)

        @property
        def value(self):
            """Get Python value for this argument."""
            return self._value

        @value.setter
        def value(self, value):
            """Set Python value for this argument."""
            self.validate_value(value)
            self._value = value

        @property
        def upnp_value(self):
            """Get UPnP value for this argument."""
            return self.coerce_upnp(self.value)

        @upnp_value.setter
        def upnp_value(self, upnp_value):
            """Set UPnP value for this argument."""
            self._value = self.coerce_python(upnp_value)

        def coerce_python(self, upnp_value):
            """Coerce UPnP value to Python."""
            return self.related_state_variable.coerce_python(upnp_value)

        def coerce_upnp(self, value):
            """Coerce Python value to UPnP value."""
            return self.related_state_variable.coerce_upnp(value)

    def __init__(self, name, args):
        self._name = name
        self._args = args

    @property
    def name(self):
        """Get name of this UpnpAction."""
        return self._name

    def __str__(self):
        return "<UpnpService.Action({0})>".format(self.name)

    def validate_arguments(self, **kwargs):
        """Validate arguments against in-arguments of self.
        The python type is expected."""
        for arg in self.in_arguments():
            value = kwargs[arg.name]
            arg.validate_value(value)

    def in_arguments(self):
        """Get all in-arguments."""
        return [arg for arg in self._args if arg.direction == 'in']

    def out_arguments(self):
        """Get all out-arguments."""
        return [arg for arg in self._args if arg.direction == 'out']

    def argument(self, name, direction=None):
        """Get an UpnpAction.Argument by name (and possibliy direction.)"""
        for arg in self._args:
            if arg.name != name:
                continue
            if direction is not None and arg.direction != direction:
                continue

            return arg

    def create_request(self, control_url, service_type, **kwargs):
        """Create headers and headers for this to-be-called UpnpAction."""
        # construct SOAP body
        service_ns = service_type
        soap_args = self._format_request_args(**kwargs)
        body = """<?xml version="1.0"?>
        <s:Envelope s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/" xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">
          <s:Body>
            <u:{1} xmlns:u="{0}">
                {2}
            </u:{1}>
           </s:Body>
        </s:Envelope>""".format(service_ns, self.name, soap_args)

        # construct SOAP header
        soap_action = "{0}#{1}".format(service_type, self.name)
        headers = {
            'SOAPAction': u'"{0}"'.format(soap_action),
            'Host': urllib.parse.urlparse(control_url).netloc,
            'Content-Type': 'text/xml',
            'Content-Length': str(len(body)),
        }

        return headers, body

    def _format_request_args(self, **kwargs):
        self.validate_arguments(**kwargs)
        arg_strs = ["<{0}>{1}</{0}>".format(arg.name, arg.coerce_upnp(kwargs[arg.name])) for arg in self.in_arguments()]
        return "\n".join(arg_strs)

    # pylint: disable=unused-argument
    def parse_response(self, service_type, response_headers, response_body):
        """Parse response from called Action."""
        xml = ET.fromstring(response_body)

        query = './/soap_envelope:Body/soap_envelope:Fault'
        if xml.find(query, NS):
            _LOGGER.error('%s.async_call_action(): Error: %s', self, response_body)
            raise RuntimeError('Error during call_action')

        return self._parse_response_args(service_type, xml)

    def _parse_response_args(self, service_type, xml):
        args = {}
        query = ".//{{{0}}}{1}Response".format(service_type, self.name)
        response = xml.find(query, NS)
        for arg_xml in response.findall('./'):
            name = arg_xml.tag
            arg = self.argument(name, 'out')

            arg.upnp_value = arg_xml.text
            args[name] = arg.value

        return args


class UpnpStateVariable(object):
    """Representation of a State Variable."""

    def __init__(self, state_variable_info, schema):
        self._state_variable_info = state_variable_info
        self._schema = schema

        self._value = None
        self._updated_at = None

    @property
    def min_value(self):
        """Min value for this UpnpStateVariable, if defined."""
        type_info = self._state_variable_info['type_info']
        data_type = type_info['data_type_python']
        min_ = type_info.get('allowed_value_range', {}).get('min')
        if data_type == int and min_ is not None:
            return data_type(min_)

    @property
    def max_value(self):
        """Max value for this UpnpStateVariable, if defined."""
        type_info = self._state_variable_info['type_info']
        data_type = type_info['data_type_python']
        max_ = type_info.get('allowed_value_range', {}).get('max')
        if data_type == int and max_ is not None:
            return data_type(max_)

    @property
    def allowed_values(self):
        """List with allowed values for this UpnpStateVariable, if defined."""
        return self._state_variable_info['type_info'].get('allowed_values', [])

    @property
    def send_events(self):
        """Does this UpnpStatevariable send events?"""
        return self._state_variable_info['send_events']

    @property
    def name(self):
        """Name of the UpnpStatevariable."""
        return self._state_variable_info['name']

    @property
    def data_type(self):
        """Python datatype of UpnpStateVariable."""
        return self._state_variable_info['type_info']['data_type']

    @property
    def default_value(self):
        """Default value for UpnpStateVariable, if defined."""
        data_type = self._state_variable_info['type_info']['data_type_python']
        default_value = self._state_variable_info['type_info'].get('default_value', None)
        if default_value:
            return data_type(default_value)

    def validate_value(self, value):
        """Validate value"""
        self._schema({'value': value})

    @property
    def value(self):
        """Get the value, python typed."""
        return self._value

    @value.setter
    def value(self, value):
        """Set value, python typed."""
        self.validate_value(value)
        self._value = value
        self._updated_at = utcnow()

    @property
    def upnp_value(self):
        """Get the value, UPnP typed."""
        return self.coerce_upnp(self.value)

    @upnp_value.setter
    def upnp_value(self, upnp_value):
        """Set the value, UPnP typed."""
        self.value = self.coerce_python(upnp_value)

    def coerce_python(self, upnp_value):
        """Coerce value from UPNP to python."""
        data_type = self._state_variable_info['type_info']['data_type_python']
        if data_type == bool:
            return upnp_value == '1'
        return data_type(upnp_value)

    def coerce_upnp(self, value):
        """Coerce value from python to UPNP."""
        data_type = self._state_variable_info['type_info']['data_type_python']
        if data_type == bool:
            return '1' if value else '0'
        return str(value)

    @property
    def updated_at(self):
        """Get timestamp at which this UpnpStateVariable was updated."""
        return self._updated_at

    def __str__(self):
        return "<StateVariable({0}, {1})>".format(self.name, self.data_type)


class UpnpFactory(object):
    """Factory for UpnpService and friends."""

    STATE_VARIABLE_TYPE_MAPPING = {
        'i2': int,
        'i4': int,
        'ui2': int,
        'ui4': int,
        'string': str,
        'boolean': bool,
    }

    def __init__(self, hass):
        self.hass = hass

    @asyncio.coroutine
    def async_create_services(self, url):
        """Retrieve URL and create all defined services."""
        _LOGGER.debug('%s.async_create_services(): %s', self, url)
        root = yield from self._async_fetch_device_description(url)

        # get name
        name = root.find('.//device:device/device:friendlyName', NS).text

        # get services
        services = []
        for service_desc in root.findall('.//device:serviceList/device:service', NS):
            service = yield from self.async_create_service(service_desc, url)
            services.append(service)

        return name, services

    @asyncio.coroutine
    def async_create_service(self, service_description_xml, base_url):
        """Retrieve the SCPD for a service and create a UpnpService from it."""
        scpd_url = service_description_xml.find('device:SCPDURL', NS).text
        scpd_url = urllib.parse.urljoin(base_url, scpd_url)
        scpd_xml = yield from self._async_fetch_scpd(scpd_url)
        return self.create_service(service_description_xml, base_url, scpd_xml)

    def create_service(self, service_description_xml, base_url, scpd_xml):
        """Create a UnpnpService, with UpnpActions and UpnpStateVariables from scpd_xml."""
        service_description = self._service_parse_xml(service_description_xml)
        state_vars = self.create_state_variables(scpd_xml)
        actions = self.create_actions(scpd_xml, state_vars)

        return UpnpService(self.hass, service_description, base_url, state_vars, actions)

    def _service_parse_xml(self, service_description_xml):
        return {
            'service_id': service_description_xml.find('device:serviceId', NS).text,
            'service_type': service_description_xml.find('device:serviceType', NS).text,
            'control_url': service_description_xml.find('device:controlURL', NS).text,
            'event_sub_url': service_description_xml.find('device:eventSubURL', NS).text,
            'scpd_url': service_description_xml.find('device:SCPDURL', NS).text,
        }

    def create_state_variables(self, scpd_xml):
        """Create UpnpStateVariables from scpd_xml."""
        state_vars = {}
        for state_var_xml in scpd_xml.findall('.//service:stateVariable', NS):
            state_var = self.create_state_variable(state_var_xml)
            state_vars[state_var.name] = state_var
        return state_vars

    def create_state_variable(self, state_variable_xml):
        """Create UpnpStateVariable from state_variable_xml"""
        state_variable_info = self._state_variable_parse_xml(state_variable_xml)
        type_info = state_variable_info['type_info']
        schema = self._state_variable_create_schema(type_info)
        return UpnpStateVariable(state_variable_info, schema)

    def _state_variable_parse_xml(self, state_variable_xml):
        info = {
            'send_events': state_variable_xml.attrib['sendEvents'] == 'yes',
            'name': state_variable_xml.find('service:name', NS).text,
            'type_info': {}
        }
        type_info = info['type_info']

        data_type = state_variable_xml.find('service:dataType', NS).text
        type_info['data_type'] = data_type
        type_info['data_type_python'] = UpnpFactory.STATE_VARIABLE_TYPE_MAPPING[data_type]

        default_value = state_variable_xml.find('service:defaultValue', NS)
        if default_value:
            type_info['default_value'] = default_value.text
            type_info['default_type_coerced'] = data_type(default_value.text)

        allowed_value_range = state_variable_xml.find('service:allowedValueRange', NS)
        if allowed_value_range:
            type_info['allowed_value_range'] = {
                'min': allowed_value_range.find('service:minimum', NS).text,
                'max': allowed_value_range.find('service:maximum', NS).text,
            }
            if allowed_value_range.find('service:step', NS):
                type_info['allowed_value_range']['step'] = allowed_value_range.find('service:step', NS).text

        allowed_value_list = state_variable_xml.find('service:allowedValueList', NS)
        if allowed_value_list:
            type_info['allowed_values'] = [v.text for v in allowed_value_list.findall('service:allowedValue', NS)]

        return info

    def _state_variable_create_schema(self, type_info):
        # construct validators
        validators = []

        data_type = type_info['data_type_python']
        validators.append(data_type)

        if 'allowed_values' in type_info:
            allowed_values = type_info['allowed_values']
            in_ = vol.In(allowed_values)  # coerce allowed values? assume always string for now
            validators.append(in_)

        if 'allowed_value_range' in type_info:
            min_ = type_info['allowed_value_range'].get('min', None)
            max_ = type_info['allowed_value_range'].get('max', None)
            min_ = data_type(min_)
            max_ = data_type(max_)
            range_ = vol.Range(min=min_, max=max_)
            validators.append(range_)

        # construct key
        key = vol.Required('value')

        if 'default_value' in type_info:
            default_value = type_info['default_value']
            if data_type == bool:
                default_value = default_value == '1'
            else:
                default_value = data_type(default_value)
            key.default = default_value

        return vol.Schema({key: vol.All(*validators)})

    def create_actions(self, scpd_xml, state_variables):
        """Create UpnpActions from scpd_xml."""
        actions = {}
        for action_xml in scpd_xml.findall('.//service:action', NS):
            action = self.create_action(action_xml, state_variables)
            actions[action.name] = action
        return actions

    def create_action(self, action_xml, state_variables):
        """Create a UpnpAction from action_xml."""
        action_info = self._action_parse_xml(action_xml, state_variables)
        args = [UpnpAction.Argument(arg_info['name'], arg_info['direction'], arg_info['state_variable'])
                for arg_info in action_info['arguments']]
        return UpnpAction(action_info['name'], args)

    def _action_parse_xml(self, action_xml, state_variables):
        info = {
            'name': action_xml.find('service:name', NS).text,
            'arguments': [],
        }
        for argument_xml in action_xml.findall('.//service:argument', NS):
            state_variable_name = argument_xml.find('service:relatedStateVariable', NS).text
            argument = {
                'name': argument_xml.find('service:name', NS).text,
                'direction': argument_xml.find('service:direction', NS).text,
                'state_variable': state_variables[state_variable_name],
            }
            info['arguments'].append(argument)
        return info

    @asyncio.coroutine
    def _async_fetch_url(self, url):
        websession = async_get_clientsession(self.hass)
        try:
            with async_timeout.timeout(10, loop=self.hass.loop):
                response = yield from websession.get(url)
                response_body = yield from response.text()
        except (asyncio.TimeoutError, aiohttp.ClientError) as ex:
            _LOGGER.debug("Error for UpnpServiceFactory._async_fetch_scpd(): %s", ex)
            raise
        return response_body

    @asyncio.coroutine
    def _async_fetch_device_description(self, url):
        response_body = yield from self._async_fetch_url(url)
        root = ET.fromstring(response_body)
        return root

    @asyncio.coroutine
    def _async_fetch_scpd(self, url):
        response_body = yield from self._async_fetch_url(url)
        root = ET.fromstring(response_body)
        return root
# endregion
