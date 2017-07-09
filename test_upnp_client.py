"""Unit tests for upnp_client."""
import asyncio
import pytest
import voluptuous as vol
import xml.etree.ElementTree as ET

from upnp_client import (
    UpnpError,
    UpnpFactory,
    UpnpRequester
)


NS = {
    'device': 'urn:schemas-upnp-org:device-1-0',
    'service': 'urn:schemas-upnp-org:service-1-0',
}


def read_file(filename):
    with open(filename, 'r') as f:
        return f.read()


class UpnpTestRequester(UpnpRequester):

    def __init__(self, response_map):
        self._response_map = response_map

        self.hass = None

    @asyncio.coroutine
    def async_http_request(self, method, url, headers=None, body=None):
        yield from asyncio.sleep(0.01)

        key = (method, url)
        if key not in self._response_map:
            raise Exception('Request not in response map')

        return self._response_map[key]


RESPONSE_MAP = {
    ('GET', 'http://localhost:1234/dmr'):
        (200, {}, read_file('fixtures/dmr')),
    ('GET', 'http://localhost:1234/RenderingControl_1.xml'):
        (200, {}, read_file('fixtures/RenderingControl_1.xml')),
    ('GET', 'http://localhost:1234/AVTransport_1.xml'):
        (200, {}, read_file('fixtures/AVTransport_1.xml')),
    ('SUBSCRIBE', 'http://localhost:1234/upnp/event/RenderingControl1'):
        (200, {'sid': 'uuid:dummy'}, ''),
    ('UNSUBSCRIBE', 'http://localhost:1234/upnp/event/RenderingControl1'):
        (200, {'sid': 'uuid:dummy'}, ''),
}


class TestUpnpStateVariable:

    @pytest.mark.asyncio
    def test_init(self):
        r = UpnpTestRequester(RESPONSE_MAP)
        factory = UpnpFactory(r)
        device = yield from factory.async_create_device('http://localhost:1234/dmr')
        assert device

        service = device.service('urn:schemas-upnp-org:service:RenderingControl:1')
        assert service

        state_var = service.state_variable('Volume')
        assert state_var

    @pytest.mark.asyncio
    def test_set_value_volume(self):
        r = UpnpTestRequester(RESPONSE_MAP)
        factory = UpnpFactory(r)
        device = yield from factory.async_create_device('http://localhost:1234/dmr')
        service = device.service('urn:schemas-upnp-org:service:RenderingControl:1')
        sv = service.state_variable('Volume')

        sv.value = 10
        assert sv.value == 10
        assert sv.upnp_value == '10'

        sv.upnp_value = '20'
        assert sv.value == 20
        assert sv.upnp_value == '20'

    @pytest.mark.asyncio
    def test_set_value_mute(self):
        r = UpnpTestRequester(RESPONSE_MAP)
        factory = UpnpFactory(r)
        device = yield from factory.async_create_device('http://localhost:1234/dmr')
        service = device.service('urn:schemas-upnp-org:service:RenderingControl:1')
        sv = service.state_variable('Mute')

        sv.value = True
        assert sv.value is True
        assert sv.upnp_value == '1'

        sv.value = False
        assert sv.value is False
        assert sv.upnp_value == '0'

        sv.upnp_value = '1'
        assert sv.value is True
        assert sv.upnp_value == '1'

        sv.upnp_value = '0'
        assert sv.value is False
        assert sv.upnp_value == '0'

    @pytest.mark.asyncio
    def test_value_min_max(self):
        r = UpnpTestRequester(RESPONSE_MAP)
        factory = UpnpFactory(r)
        device = yield from factory.async_create_device('http://localhost:1234/dmr')
        service = device.service('urn:schemas-upnp-org:service:RenderingControl:1')
        sv = service.state_variable('Volume')

        assert sv.min_value == 0
        assert sv.max_value == 100

        try:
            sv.value = -10
            assert False
        except vol.error.MultipleInvalid:
            pass

        try:
            sv.value = 110
            assert False
        except vol.error.MultipleInvalid:
            pass

    @pytest.mark.asyncio
    def test_value_allowed_value(self):
        r = UpnpTestRequester(RESPONSE_MAP)
        factory = UpnpFactory(r)
        device = yield from factory.async_create_device('http://localhost:1234/dmr')
        service = device.service('urn:schemas-upnp-org:service:RenderingControl:1')
        sv = service.state_variable('A_ARG_TYPE_Channel')

        assert sv.allowed_values == ['Master']

        # should be ok
        sv.value = 'Master'
        assert sv.value == 'Master'

        try:
            sv.value = 'Left'
            assert False
        except vol.error.MultipleInvalid:
            pass


class TestUpnpServiceAction:

    @pytest.mark.asyncio
    def test_init(self):
        r = UpnpTestRequester(RESPONSE_MAP)
        factory = UpnpFactory(r)
        device = yield from factory.async_create_device('http://localhost:1234/dmr')
        service = device.service('urn:schemas-upnp-org:service:RenderingControl:1')
        action = service.action('GetVolume')

        assert action
        assert action.name == 'GetVolume'

    @pytest.mark.asyncio
    def test_valid_arguments(self):
        r = UpnpTestRequester(RESPONSE_MAP)
        factory = UpnpFactory(r)
        device = yield from factory.async_create_device('http://localhost:1234/dmr')
        service = device.service('urn:schemas-upnp-org:service:RenderingControl:1')
        action = service.action('SetVolume')

        # all ok
        action.validate_arguments(InstanceID=0, Channel='Master', DesiredVolume=10)

        # invalid type for InstanceID
        try:
            action.validate_arguments(InstanceID='0', Channel='Master', DesiredVolume=10)
            assert False
        except vol.error.MultipleInvalid:
            pass

        # missing DesiredVolume
        try:
            action.validate_arguments(InstanceID='0', Channel='Master')
            assert False
        except vol.error.MultipleInvalid:
            pass

    @pytest.mark.asyncio
    def test_format_request(self):
        r = UpnpTestRequester(RESPONSE_MAP)
        factory = UpnpFactory(r)
        device = yield from factory.async_create_device('http://localhost:1234/dmr')
        service = device.service('urn:schemas-upnp-org:service:RenderingControl:1')
        action = service.action('SetVolume')

        service_type = 'urn:schemas-upnp-org:service:RenderingControl:1'
        url, headers, body = action.create_request(InstanceID=0, Channel='Master', DesiredVolume=10)

        root = ET.fromstring(body)
        ns = {'rc_service': service_type}
        assert root.find('.//rc_service:SetVolume', ns) is not None
        assert root.find('.//DesiredVolume', ns) is not None

    @pytest.mark.asyncio
    def test_parse_response(self):
        r = UpnpTestRequester(RESPONSE_MAP)
        factory = UpnpFactory(r)
        device = yield from factory.async_create_device('http://localhost:1234/dmr')
        service = device.service('urn:schemas-upnp-org:service:RenderingControl:1')
        action = service.action('GetVolume')

        service_type = 'urn:schemas-upnp-org:service:RenderingControl:1'
        response = read_file('fixtures/action_GetVolume.xml')
        result = action.parse_response(service_type, {}, response)
        assert result == {'CurrentVolume': 3}

    @pytest.mark.asyncio
    def test_parse_response_error(self):
        r = UpnpTestRequester(RESPONSE_MAP)
        factory = UpnpFactory(r)
        device = yield from factory.async_create_device('http://localhost:1234/dmr')
        service = device.service('urn:schemas-upnp-org:service:RenderingControl:1')
        action = service.action('GetVolume')

        service_type = 'urn:schemas-upnp-org:service:RenderingControl:1'
        response = read_file('fixtures/action_GetVolumeError.xml')
        try:
            action.parse_response(service_type, {}, response)
            assert False
        except UpnpError:
            pass

class TestUpnpService:

    @pytest.mark.asyncio
    def test_init(self):
        r = UpnpTestRequester(RESPONSE_MAP)
        factory = UpnpFactory(r)
        device = yield from factory.async_create_device('http://localhost:1234/dmr')
        service = device.service('urn:schemas-upnp-org:service:RenderingControl:1')

        base_url = 'http://localhost:1234'
        assert service
        assert service.service_type == 'urn:schemas-upnp-org:service:RenderingControl:1'
        assert service.control_url == base_url + '/upnp/control/RenderingControl1'
        assert service.event_sub_url == base_url + '/upnp/event/RenderingControl1'
        assert service.scpd_url == base_url + '/RenderingControl_1.xml'

    @pytest.mark.asyncio
    def test_state_variables_actions(self):
        r = UpnpTestRequester(RESPONSE_MAP)
        factory = UpnpFactory(r)
        device = yield from factory.async_create_device('http://localhost:1234/dmr')
        service = device.service('urn:schemas-upnp-org:service:RenderingControl:1')

        state_var = service.state_variable('Volume')
        assert state_var

        action = service.action('GetVolume')
        assert action

    @pytest.mark.asyncio
    def test_subscribe(self):
        r = UpnpTestRequester(RESPONSE_MAP)
        factory = UpnpFactory(r)
        device = yield from factory.async_create_device('http://localhost:1234/dmr')
        service = device.service('urn:schemas-upnp-org:service:RenderingControl:1')

        callback_uri = 'http://callback_uri'
        sid = 'uuid:dummy'

        received_sid = yield from service.async_subscribe(callback_uri)
        assert sid == received_sid
        assert sid == service.subscription_sid

    @pytest.mark.asyncio
    def test_unsubscribe(self):
        r = UpnpTestRequester(RESPONSE_MAP)
        factory = UpnpFactory(r)
        device = yield from factory.async_create_device('http://localhost:1234/dmr')
        service = device.service('urn:schemas-upnp-org:service:RenderingControl:1')
        service._subscription_sid = 'uuid:dummy'

        assert service.subscription_sid == 'uuid:dummy'
        yield from service.async_unsubscribe()
        assert service.subscription_sid is None

    @pytest.mark.asyncio
    def test_call_action(self):
        responses = {
            ('POST', 'http://localhost:1234/upnp/control/RenderingControl1'):
                (200, {}, read_file('fixtures/action_GetVolume.xml'))
        }
        responses.update(RESPONSE_MAP)
        r = UpnpTestRequester(responses)
        factory = UpnpFactory(r)
        device = yield from factory.async_create_device('http://localhost:1234/dmr')
        service = device.service('urn:schemas-upnp-org:service:RenderingControl:1')
        action = service.action('GetVolume')

        result = yield from service.async_call_action(action, InstanceID=0, Channel='Master')
        assert result['CurrentVolume'] == 3
