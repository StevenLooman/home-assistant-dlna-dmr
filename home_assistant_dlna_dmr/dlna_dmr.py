# -*- coding: utf-8 -*-
"""
Support for DLNA DMR (Device Media Renderer)
"""

import asyncio
import functools
import logging
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime
from datetime import timedelta

import aiohttp
import async_timeout
import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.components.http.view import (
    request_handler_factory, HomeAssistantView)
from homeassistant.components.media_player import (
    SUPPORT_PLAY, SUPPORT_PAUSE, SUPPORT_STOP,
    SUPPORT_VOLUME_MUTE, SUPPORT_VOLUME_SET,
    SUPPORT_PLAY_MEDIA,
    SUPPORT_PREVIOUS_TRACK, SUPPORT_NEXT_TRACK,
    MediaPlayerDevice,
    PLATFORM_SCHEMA)
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STOP,
    CONF_URL, CONF_NAME,
    STATE_OFF, STATE_ON, STATE_IDLE, STATE_PLAYING, STATE_PAUSED)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession


REQUIREMENTS = ['async_upnp_client==0.10.0']

DEFAULT_NAME = 'DLNA_DMR'
CONF_MAX_VOLUME = 'max_volume'
CONF_PICKY_DEVICE = 'picky_device'

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Required(CONF_URL): cv.string,
    vol.Optional(CONF_NAME): cv.string,
    vol.Optional(CONF_MAX_VOLUME): cv.positive_int,
    vol.Optional(CONF_PICKY_DEVICE): cv.boolean,
})

NS = {
    'didl_lite': 'urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/',
    'dc': 'http://purl.org/dc/elements/1.1/',
}

SERVICE_TYPES = {
    'RC': 'urn:schemas-upnp-org:service:RenderingControl:1',
    'AVT': 'urn:schemas-upnp-org:service:AVTransport:1',
}

HOME_ASSISTANT_UPNP_CLASS_MAPPING = {
    'music': 'object.item.audioItem',
    'tvshow': 'object.item.videoItem',
    'video': 'object.item.videoItem',
    'episode': 'object.item.videoItem',
    'channel': 'object.item.videoItem',
    'playlist': 'object.item.playlist',
}


_LOGGER = logging.getLogger(__name__)


# region Decorators
def requires_action(service_type, action_name, value_not_connected=None):
    """Raise NotImplemented() if connected but service/action not available."""

    def call_wrapper(func):
        """Call wrapper for decorator"""

        @functools.wraps(func)
        def wrapper(self, *args, **kwargs):
            """
            Require device is connected and has service/action.
            If device is not connected, value_not_connected is returned.
            """
            # pylint: disable=protected-access

            # _LOGGER.debug('needs_action(): %s.%s', self, func.__name__)
            if not self._is_connected:
                # _LOGGER.debug('needs_action(): %s.%s: not connected', self, func.__name__)
                return value_not_connected

            service = self._service(service_type)
            if not service:
                _LOGGER.error('requires_state_variable(): %s.%s: no service: %s',
                              self, func.__name__, service_type)
                raise NotImplementedError()

            action = service.action(action_name)
            if not action:
                _LOGGER.error('requires_action(): %s.%s: no action: %s.%s',
                              self, func.__name__, service_type, action_name)
                raise NotImplementedError()
            return func(self, action, *args, **kwargs)

        return wrapper

    return call_wrapper


def requires_state_variable(service_type, state_variable_name, value_not_connected=None):
    """Raise NotImplemented() if connected but service/state_variable not available."""

    def call_wrapper(func):
        """Call wrapper for decorator."""

        @functools.wraps(func)
        def wrapper(self, *args, **kwargs):
            """
            Require device is connected and has service/state_variable.
            If device is not connected, value_not_connected is returned.
            """
            # pylint: disable=protected-access

            # _LOGGER.debug('needs_service(): %s.%s', self, func.__name__)
            if not self._is_connected:
                # _LOGGER.debug('needs_service(): %s.%s: not connected', self, func.__name__)
                return value_not_connected

            service = self._service(service_type)
            if not service:
                _LOGGER.error('requires_state_variable(): %s.%s: no service: %s',
                              self, func.__name__, service_type)
                raise NotImplementedError()

            state_var = service.state_variable(state_variable_name)
            if not state_var:
                _LOGGER.error('requires_state_variable(): %s.%s: no state_variable: %s.%s',
                              self, func.__name__, service_type, state_variable_name)
                raise NotImplementedError()
            return func(self, state_var, *args, **kwargs)
        return wrapper
    return call_wrapper
# endregion


# region Home Assistant
def start_notify_view(hass):
    """Register notify view."""
    hass_data = hass.data[__name__]
    name = 'notify_view'
    if name in hass_data:
        return hass_data[name]

    view = UpnpNotifyView(hass)
    hass_data[name] = view
    hass.http.register_view(view)
    return view


def start_proxy_view(hass):
    """Register proxy view."""
    hass_data = hass.data[__name__]
    name = 'proxy_view'
    if name in hass_data:
        return hass_data[name]

    view = PickyDeviceProxyView(hass)
    hass_data[name] = view
    hass.http.register_view(view)
    return view


def setup_platform(hass: HomeAssistant, config, add_devices, discovery_info=None):
    """Set up DLNA DMR platform."""
    if discovery_info and \
       'upnp_device_type' in discovery_info and \
       discovery_info['upnp_device_type'] != 'urn:schemas-upnp-org:device:MediaRenderer:1':
        _LOGGER.debug('Device is not a MediaRenderer: %s', discovery_info.get('ssdp_description'))
        return

    is_picky = False
    if config.get(CONF_URL) is not None:
        url = config.get(CONF_URL)
        name = config.get(CONF_NAME)
    elif discovery_info is not None:
        url = discovery_info['ssdp_description']
        name = discovery_info['name']
        # Samsung TVs are particular picky with regard to their sources
        is_picky = 'samsung' in discovery_info.get('manufacturer', '').lower() and \
                   'tv' in discovery_info.get('name', '').lower()

    cfg_extra = {
        CONF_MAX_VOLUME: config.get(CONF_MAX_VOLUME),
        CONF_PICKY_DEVICE: config.get(CONF_PICKY_DEVICE) or is_picky,
    }

    # set up our Views, if not already done so
    if __name__ not in hass.data:
        hass.data[__name__] = {}

    hass.async_run_job(start_notify_view, hass)
    hass.async_run_job(start_proxy_view, hass)

    from async_upnp_client import UpnpFactory
    requester = HassUpnpRequester(hass)
    factory = UpnpFactory(requester)
    device = DlnaDmrDevice(hass, url, name, factory, **cfg_extra)

    _LOGGER.debug("Adding device: %s", device)
    add_devices([device])


@asyncio.coroutine
def fetch_headers(hass, url, headers):
    # try a HEAD request to the source
    src_response = None
    try:
        session = async_get_clientsession(hass)
        src_response = yield from session.head(url, headers=headers)
        yield from src_response.release()
    except:
        pass

    if src_response and 200 <= src_response.status < 300:
        return src_response.headers

    # then try a GET request to the source, but ignore all the data
    session = async_get_clientsession(hass)
    src_response = yield from session.get(url, headers=headers)
    yield from src_response.release()

    return src_response.headers


class UpnpNotifyView(HomeAssistantView):
    """Callback view for UPnP NOTIFY messages"""

    url = '/api/dlna_dmr.notify'
    name = 'api:dlna_dmr:notify'
    requires_auth = False

    def __init__(self, hass):
        self.hass = hass
        self._registered_services = {}
        self._backlog = {}

    def register(self, router):
        """Register the view with a router."""
        handler = request_handler_factory(self, self.async_notify)
        router.add_route('notify', UpnpNotifyView.url, handler)

    @asyncio.coroutine
    def async_notify(self, request):
        """Callback method for NOTIFY requests."""
        #_LOGGER.debug('%s.async_notify(): request: %s, remote: %s, SID: %s', self, request, request.remote, request.headers.get('SID', None))

        if 'SID' not in request.headers:
            return aiohttp.web.Response(status=422)

        headers = request.headers
        sid = headers['SID']
        body = yield from request.text()

        # find UpnpService by SID
        if sid not in self._registered_services:
            #_LOGGER.debug('%s.async_notify(): unknown SID: %s, storing for later', self, sid)
            self._backlog[sid] = {'headers': headers, 'body': body}
            return aiohttp.web.Response(status=202)

        service = self._registered_services[sid]
        service.on_notify(headers, body)
        return aiohttp.web.Response(status=200)

    @property
    def callback_url(self):
        """Full URL to be called by device/service."""
        base_url = self.hass.config.api.base_url
        return urllib.parse.urljoin(base_url, self.url)

    def register_service(self, sid, service):
        """
        Register a UpnpService under SID.
        """
        #_LOGGER.debug('%s.register_service(): sid: %s, service: %s', self, sid, service)
        if sid in self._registered_services:
            raise RuntimeError('SID {} already registered.'.format(sid))

        self._registered_services[sid] = service

        if sid in self._backlog:
            item = self._backlog[sid]
            service.on_notify(item['headers'], item['body'])
            del self._backlog[sid]

    def unregister_service(self, sid):
        """Unregister service by SID."""
        if sid in self._registered_services:
            del self._registered_services[sid]


class PickyDeviceProxyView(HomeAssistantView):
    """View to serve device"""

    url = '/api/dlna_dmr.proxy/{key}'
    proxy_path = '/api/dlna_dmr.proxy'

    name = 'api:dlna_dmr:proxy'
    requires_auth = False

    def __init__(self, hass):
        self.hass = hass
        self._entries = {}

    def register(self, router):
        """Register the view with a router."""
        handler = request_handler_factory(self, self.async_head)
        router.add_route('head', self.url, handler)

        handler = request_handler_factory(self, self.async_get)
        router.add_route('get', self.url, handler)

    def prune_entries(self):
        max_age = timedelta(hours=24)
        now = datetime.now()

        to_remove = []
        for key, entry in self._entries.items():
            age = now - entry['added_at']
            if age > max_age:
                to_remove.append(key)

        for key in to_remove:
            del self._entries[key]

    def add_url(self, url):
        self.prune_entries()

        import hashlib
        key = hashlib.sha256(url.encode('utf-8')).hexdigest()

        self._entries[key] = {
            'url': url,
            'added_at': datetime.now(),
        }

        return key

    @property
    def callback_url(self):
        """Full URL to be called by device/service."""
        base_url = self.hass.config.api.base_url
        return urllib.parse.urljoin(base_url, self.url)

    @asyncio.coroutine
    def async_head(self, request, **args):
        """Handle HEAD request."""
        #_LOGGER.debug('%s.async_head(): %s %s\n%s\n%s', self, request.method, request.url,
        #        '\n'.join([key + ': ' + value for key, value in request.headers.items()]), args)

        url = None
        if 'key' in args:
            key = args['key']
            entry = self._entries[key]
            url = entry['url']
        else:
            return aiohttp.web.Response(body="Missing URL", status=422)

        src_headers = yield from fetch_headers(self.hass, url, request.headers)
        headers = {
            'Accept-Ranges': 'bytes',
            'transferMode.dlna.org': 'Streaming',
            'contentFeatures.dlna.org': 'DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000',
        }
        headers.update(src_headers)
        #_LOGGER.debug('%s.async_head(): Response: %s\n%s', self, 200, '\n'.join([key + ': ' + value for key, value in headers.items()]))
        return aiohttp.web.Response(headers=headers)

    @asyncio.coroutine
    def async_get(self, request, **args):
        """Handle GET request."""
        #_LOGGER.debug('%s.async_get(): %s %s\n%s\n%s', self, request.method, request.url,
        #        '\n'.join([key + ': ' + value for key, value in request.headers.items()]), args)

        url = None
        if 'key' in args:
            key = args['key']
            entry = self._entries[key]
            url = entry['url']
        else:
            return aiohttp.web.Response(body="Missing URL", status=422)

        # get data from source
        session = async_get_clientsession(self.hass)
        src_response = yield from session.get(url, headers=request.headers)
        src_data = yield from src_response.read()

        headers = {
            'Accept-Ranges': 'bytes',
            'transferMode.dlna.org': 'Streaming',
            'contentFeatures.dlna.org': 'DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000',
        }
        headers.update(src_response.headers)

        if 'range' in request.headers:
            range_ = request.headers['range']
            parts = [int(x) for x in range_.replace('bytes=', '').split('-') if x]
            from_ = parts[0]
            to = parts[1] if len(parts) == 2 else len(src_data)
            chunk_size = (to - from_)
            headers['Content-Range'] = 'bytes {}-{}/{}'.format(from_, to, len(src_data))
            headers['Content-Length'] = str(chunk_size)
            src_data = src_data[from_:to]
            #_LOGGER.debug('%s.async_get(): Response: %s\n%s\n%s', self, 206, '\n'.join([key + ": " + value for key, value in headers.items()]), len(src_data))
            return aiohttp.web.Response(body=src_data, status=206, headers=headers)

        #_LOGGER.debug('%s.async_get(): Response: %s\n%s\n%s', self, 200, '\n'.join([key + ": " + value for key, value in headers.items()]), len(src_data))
        return aiohttp.web.Response(body=src_data, status=200, headers=headers)


class HassUpnpRequester(object):
    """UpnpRequester for home-assistant."""

    def __init__(self, hass):
        self.hass = hass

    @asyncio.coroutine
    def async_http_request(self, method, url, headers=None, body=None):
        session = async_get_clientsession(self.hass)
        with async_timeout.timeout(5, loop=self.hass.loop):
            response = yield from session.request(method, url, headers=headers, data=body)
            response_body = yield from response.text()
            yield from response.release()
        yield from asyncio.sleep(0.25)

        return response.status, response.headers, response_body


class DlnaDmrDevice(MediaPlayerDevice):
    """Representation of a DLNA DMR device."""

    def __init__(self, hass, url, name, factory, **additional_configuration):
        self.hass = hass
        self._url = url
        self._name = name
        self._factory = factory
        self._additional_configuration = additional_configuration

        self._notify_view = hass.data[__name__]['notify_view']

        self._device = None
        self._is_connected = False

        hass.bus.listen_once(EVENT_HOMEASSISTANT_STOP, self._async_on_hass_stop)

    @property
    def available(self):
        """Device is avaiable?"""
        return self._is_connected

    @asyncio.coroutine
    def _async_on_hass_stop(self, event):
        """Event handler on HASS stop."""
        #_LOGGER.debug('%s._on_hass_stop(): %s', self, event)
        yield from self.async_unsubscribe_all()

    def _service(self, service_type):
        """Get UpnpService by service_type or alias."""
        if not self._device:
            return

        service_type = SERVICE_TYPES.get(service_type, service_type)
        return self._device.service(service_type)

    @asyncio.coroutine
    def async_unsubscribe_all(self):
        """
        Disconnect from device.
        This removes all UpnpServices.
        """
        #_LOGGER.debug('%s.async_disconnect()', self)

        if not self._device:
            return

        for service in self._device.services.values():
            try:
                sid = service.subscription_sid
                if sid:
                    self._notify_view.unregister_service(sid)
                    yield from service.async_unsubscribe(True)
            except (asyncio.TimeoutError, aiohttp.ClientError):
                pass

    @asyncio.coroutine
    def _async_init_device(self):
        """Fetch and init services."""
        #_LOGGER.debug('%s._async_init_device()', self)
        self._device = yield from self._factory.async_create_device(self._url)

        # set name
        if self.name is None or self.name == DEFAULT_NAME:
            self._name = self._device.name

        # subscribe services for events
        callback_url = self._notify_view.callback_url
        for service in self._device.services.values():
            service.on_state_variable_change = self.on_state_variable_change

            sid = yield from service.async_subscribe(callback_url)
            #_LOGGER.debug('%s._async_init_device(): Got SID: %s', self, sid)
            if sid:
                self._notify_view.register_service(sid, service)

    @asyncio.coroutine
    def async_update(self):
        """Retrieve the latest data."""
        #_LOGGER.debug('%s.async_update()', self)
        if not self._device:
            #_LOGGER.debug('%s.async_update(): no device', self)
            try:
                yield from self._async_init_device()
            except (asyncio.TimeoutError, aiohttp.ClientError):
                # Not yet seen alive, leave for now, gracefully
                #_LOGGER.debug('%s._async_update(): device not seen yet, leaving', self)
                return

        # XXX TODO: if re-connected, then (re-)subscribe

        # call GetTransportInfo/GetPositionInfo regularly
        try:
            #_LOGGER.debug('%s.async_update(): calling...', self)
            avt_service = self._service('AVT')
            if avt_service:
                get_transport_info_action = avt_service.action('GetTransportInfo')
                state = yield from self._async_poll_transport_info(get_transport_info_action)
                yield from asyncio.sleep(0.25)

                if state == STATE_PLAYING or state == STATE_PAUSED:
                    # playing something... get position info
                    get_position_info_action = avt_service.action('GetPositionInfo')
                    yield from self._async_poll_position_info(get_position_info_action)
            else:
                #_LOGGER.debug('%s.async_update(): pinging...', self)
                yield from self._device.async_ping()

            self._is_connected = True
        except (asyncio.TimeoutError, aiohttp.ClientError) as ex:
            _LOGGER.debug('%s.async_update(): error on update: %s', self, ex)
            self._is_connected = False
            yield from self.async_unsubscribe_all()

    @asyncio.coroutine
    def _async_poll_transport_info(self, action):
        """Update transport info from device."""
        #_LOGGER.debug('%s._async_poll_transport_info()', self)

        result = yield from action.async_call(InstanceID=0)
        # _LOGGER.debug('Got result: %s', result)

        # set/update state_variable 'TransportState'
        service = action.service
        state_var = service.state_variable('TransportState')
        old_value = state_var.value
        state_var.value = result['CurrentTransportState']

        if old_value != result['CurrentTransportState']:
            self.on_state_variable_change(service, [state_var])

        #_LOGGER.debug('%s._async_poll_transport_info(): state: %s', self, self.state)
        return self.state

    @asyncio.coroutine
    def _async_poll_position_info(self, action):
        """Update position info"""
        #_LOGGER.debug('%s._async_poll_position_info()', self)

        result = yield from action.async_call(InstanceID=0)
        # _LOGGER.debug('Got result: %s', result)

        service = action.service
        track_duration = service.state_variable('CurrentTrackDuration')
        track_duration.value = result['TrackDuration']

        time_position = service.state_variable('RelativeTimePosition')
        time_position.value = result['RelTime']

        self.on_state_variable_change(service, [track_duration, time_position])

    def on_state_variable_change(self, service, state_variables):
        """State variable(s) changed, let homeassistant know"""
        self.schedule_update_ha_state()

    @property
    def supported_features(self):
        """Flag media player features that are supported."""
        supported_features = 0

        if not self._device:
            return supported_features

        rc_service = self._service('RC')
        if rc_service:
            if rc_service.state_variable('Mute'):
                supported_features |= SUPPORT_VOLUME_MUTE
            if rc_service.state_variable('Volume'):
                supported_features |= SUPPORT_VOLUME_SET

        avt_service = self._service('AVT')
        if avt_service:
            state_var = avt_service.state_variable('CurrentTransportActions')
            #_LOGGER.debug('%s.supported_features(): State: %s', self, state_var.value)
            if state_var:
                value = state_var.value or ''
                actions = value.split(',')
                if 'Play' in actions:
                    supported_features |= SUPPORT_PLAY
                if 'Stop' in actions:
                    supported_features |= SUPPORT_STOP
                if 'Pause' in actions:
                    supported_features |= SUPPORT_PAUSE

            current_track_var = avt_service.state_variable('CurrentTrack')
            num_tracks_var = avt_service.state_variable('NumberOfTracks')
            if current_track_var and num_tracks_var and \
               current_track_var.value is not None and num_tracks_var.value is not None:
                current_track = current_track_var.value
                num_tracks = num_tracks_var.value
                if current_track > 1:
                    supported_features |= SUPPORT_PREVIOUS_TRACK

                if num_tracks > current_track:
                    supported_features |= SUPPORT_NEXT_TRACK

            play_media_action = avt_service.action('SetAVTransportURI')
            play_action = avt_service.action('Play')
            if play_media_action and play_action:
                supported_features |= SUPPORT_PLAY_MEDIA

        return supported_features

    @property
    @requires_state_variable('RC', 'Volume')
    def volume_level(self, state_variable):  # pylint: disable=arguments-differ
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
    def async_set_volume_level(self, action, volume):  # pylint: disable=arguments-differ
        """Set volume level, range 0..1."""
        #_LOGGER.debug('%s.async_set_volume_level(): %s', self, volume)
        state_variable = action.argument('DesiredVolume').related_state_variable
        min_ = state_variable.min_value or 0
        override_max = self._additional_configuration.get('max_volume', None)
        max_ = override_max or state_variable.max_value or 100
        desired_volume = int(min_ + volume * (max_ - min_))

        yield from action.async_call(InstanceID=0, Channel='Master', DesiredVolume=desired_volume)

    @property
    @requires_state_variable('RC', 'Mute')
    def is_volume_muted(self, state_variable):  # pylint: disable=arguments-differ
        """Boolean if volume is currently muted."""
        value = state_variable.value
        if value is None:
            _LOGGER.debug('%s.is_volume_muted(): Got no value', self)
            return None

        return value

    @asyncio.coroutine
    @requires_action('RC', 'SetMute')
    def async_mute_volume(self, action, mute):  # pylint: disable=arguments-differ
        """Mute the volume."""
        #_LOGGER.debug('%s.async_mute_volume(): %s', self, mute)
        desired_mute = bool(mute)
        yield from action.async_call(InstanceID=0, Channel='Master', DesiredMute=desired_mute)

    @asyncio.coroutine
    @requires_action('AVT', 'Pause')
    def async_media_pause(self, action):  # pylint: disable=arguments-differ
        """Send pause command."""
        #_LOGGER.debug('%s.async_media_pause()', self)
        yield from action.async_call(InstanceID=0)

    @asyncio.coroutine
    @requires_action('AVT', 'Play')
    def async_media_play(self, action):  # pylint: disable=arguments-differ
        """Send play command."""
        #_LOGGER.debug('%s.async_media_play()', self)
        yield from action.async_call(InstanceID=0, Speed='1')

    @asyncio.coroutine
    @requires_action('AVT', 'Stop')
    def async_media_stop(self, action):  # pylint: disable=arguments-differ
        """Send stop command."""
        #_LOGGER.debug('%s.async_media_stop()', self)
        yield from action.async_call(InstanceID=0)

    @asyncio.coroutine
    @requires_action('AVT', 'SetAVTransportURI')
    def async_play_media(self, action, media_type, media_id, **kwargs):  # pylint: disable=arguments-differ
        _LOGGER.debug('%s.async_play_media(): %s, %s, %s', self, media_type, media_id, kwargs)

        picky_device = self._additional_configuration.get(CONF_PICKY_DEVICE, False)
        _LOGGER.debug('%s.async_play_media(): picky_device: %s, additional_configuration: %s', self, picky_device, self._additional_configuration)

        media_info = {
            'media_url': media_id,
            'upnp_class': HOME_ASSISTANT_UPNP_CLASS_MAPPING[media_type],
        }

        src_headers = None
        try:
            src_headers = yield from fetch_headers(self.hass, media_id, {'getcontentFeatures.dlna.org': '1'})
            media_info['content_type'] = src_headers['Content-Type']
            media_info['content_features'] = src_headers['contentFeatures.dlna.org']
        except Exception as e:
            pass

        is_dlna_source = src_headers and 'contentFeatures.dlna.org' in src_headers
        if not is_dlna_source:
            if picky_device:
                _LOGGER.debug('%s.async_play_media(): detected invalid source, routing through proxy', self)
                base_url = self.hass.config.api.base_url
                proxy_view = self.hass.data[__name__]['proxy_view']
                key = proxy_view.add_url(media_id)
                proxy_url = urllib.parse.urljoin(base_url, PickyDeviceProxyView.proxy_path)
                _LOGGER.debug('%s.async_play_media(): new URL: %s, key: %s', self, proxy_url, key)

                media_info['media_url'] = '{}/{}'.format(proxy_url, key)
                media_info['content_features'] = 'DLNA.ORG_OP=01;DLNA.ORG_FLAGS=01700000000000000000000000000000'
            else:
                media_info['content_features'] = 'DLNA.ORG_OP=01;DLNA.ORG_FLAGS=00000000000000000000000000000000'

        meta_data = """<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"
                                  xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:sec="http://www.sec.co.kr/">
<item id="0" parentID="0" restricted="1">
  <dc:title>Home Assistant</dc:title>
  <upnp:class>{upnp_class}</upnp:class>
  <res protocolInfo="http-get:*:{content_type}:{content_features}">{media_url}</res>
</item>
</DIDL-Lite>""".format(**media_info)
        yield from action.async_call(InstanceID=0, CurrentURI=media_id, CurrentURIMetaData=meta_data)
        yield from asyncio.sleep(0.25)

        # send play command
        yield from self.async_media_play()
        yield from asyncio.sleep(0.25)

    @asyncio.coroutine
    @requires_action('AVT', 'Previous')
    def async_media_previous_track(self, action):  # pylint: disable=arguments-differ
        """Send previous track command."""
        #_LOGGER.debug('%s.async_media_previous_track()', self)
        yield from action.async_call(InstanceID=0)

    @asyncio.coroutine
    @requires_action('AVT', 'Next')
    def async_media_next_track(self, action):  # pylint: disable=arguments-differ
        """Send next track command."""
        #_LOGGER.debug('%s.async_media_next_track()', self)
        yield from action.async_call(InstanceID=0)

    @property
    @requires_state_variable('AVT', 'CurrentTrackMetaData')
    def media_title(self, state_variable):  # pylint: disable=arguments-differ
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
    def media_image_url(self, state_variable):  # pylint: disable=arguments-differ
        """Image url of current playing media."""
        xml = state_variable.value
        if not xml:
            return

        root = ET.fromstring(xml)
        for res in root.findall('.//didl_lite:res', NS):
            protocol_info = res.attrib.get('protocolInfo') or ''
            if protocol_info.startswith('http-get:*:image/'):
                url = protocol_info.text
                #_LOGGER.debug('%s.media_image_url(): Url: %s', self, url)
                return url

        return None

    @property
    def state(self):
        """State of the player."""
        if not self._is_connected:
            return STATE_OFF

        avt_service = self._service('AVT')
        if not avt_service:
            return STATE_ON

        transport_state = avt_service.state_variable('TransportState')
        if not transport_state:
            return STATE_ON
        elif transport_state.value == 'PLAYING':
            return STATE_PLAYING
        elif transport_state.value == 'PAUSED_PLAYBACK':
            return STATE_PAUSED

        return STATE_IDLE

    @property
    @requires_state_variable('AVT', 'CurrentTrackDuration')
    def media_duration(self, state_variable):  # pylint: disable=arguments-differ
        """Duration of current playing media in seconds."""
        if state_variable is None or state_variable.value is None:
            return None

        split = [int(v) for v in re.findall(r"[\w']+", state_variable.value)]
        delta = timedelta(hours=split[0], minutes=split[1], seconds=split[2])
        return delta.seconds

    @property
    @requires_state_variable('AVT', 'RelativeTimePosition')
    def media_position(self, state_variable):  # pylint: disable=arguments-differ
        """Position of current playing media in seconds."""
        if state_variable is None or state_variable.value is None:
            return None

        split = [int(v) for v in re.findall(r"[\w']+", state_variable.value)]
        delta = timedelta(hours=split[0], minutes=split[1], seconds=split[2])
        return delta.seconds

    @property
    @requires_state_variable('AVT', 'RelativeTimePosition')
    def media_position_updated_at(self, state_variable):  # pylint: disable=arguments-differ
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
        return "{}.{}".format(__name__, self._url)

    def __str__(self):
        return "<DlnaDmrDevice('{}')>".format(self._url)
# endregion
