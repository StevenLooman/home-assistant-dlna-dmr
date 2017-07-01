import asyncio
import unittest
import voluptuous as vol
import xml.etree.ElementTree as ET
from tests.common import (
    get_test_home_assistant, mock_coro)

from upnp_client import UpnpFactory

NS = {
    'device': 'urn:schemas-upnp-org:device-1-0',
    'service': 'urn:schemas-upnp-org:service-1-0',
    'event': 'urn:schemas-upnp-org:event-1-0',
    'didl_lite': 'urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/',

    'soap_envelope': 'http://schemas.xmlsoap.org/soap/envelope/',
}




def read_file(filename):
    with open(filename, 'r') as f:
        return f.read()


class TestUpnpServiceStateVariable(unittest.TestCase):

    def setUp(self):
        self.hass = object()
        self.factory = UpnpFactory(self.hass)

    def tearDown(self):
        self.factory = None
        self.hass = None

    def test_init(self):
        rc_xml = ET.parse('fixtures/RenderingControl_1.xml')
        volume_state_var_xml = rc_xml.find('.//service:stateVariable[service:name="Volume"]', NS)
        sv = self.factory.create_state_variable(volume_state_var_xml)
        self.assertIsNotNone(sv)
        self.assertEqual(sv.name, 'Volume')

    def test_set_value_volume(self):
        rc_xml = ET.parse('fixtures/RenderingControl_1.xml')
        volume_state_var_xml = rc_xml.find('.//service:stateVariable[service:name="Volume"]', NS)
        sv = self.factory.create_state_variable(volume_state_var_xml)

        sv.value = 10
        self.assertEqual(sv.value, 10)
        self.assertEqual(sv.upnp_value, '10')

        sv.upnp_value = '20'
        self.assertEqual(sv.value, 20)
        self.assertEqual(sv.upnp_value, '20')

    def test_set_value_mute(self):
        rc_xml = ET.parse('fixtures/RenderingControl_1.xml')
        mute_state_var_xml = rc_xml.find('.//service:stateVariable[service:name="Mute"]', NS)
        sv = self.factory.create_state_variable(mute_state_var_xml )

        sv.value = True
        self.assertEqual(sv.value, True)
        self.assertEqual(sv.upnp_value, '1')

        sv.value = False
        self.assertEqual(sv.value, False)
        self.assertEqual(sv.upnp_value, '0')

        sv.upnp_value = '1'
        self.assertEqual(sv.value, True)
        self.assertEqual(sv.upnp_value, '1')

        sv.upnp_value = '0'
        self.assertEqual(sv.value, False)
        self.assertEqual(sv.upnp_value, '0')

    def test_value_min_max(self):
        rc_xml = ET.parse('fixtures/RenderingControl_1.xml')
        volume_state_var_xml = rc_xml.find('.//service:stateVariable[service:name="Volume"]', NS)
        sv = self.factory.create_state_variable(volume_state_var_xml)

        self.assertEqual(sv.min_value, 0)
        self.assertEqual(sv.max_value, 100)

        with self.assertRaises(vol.error.MultipleInvalid):
            sv.value = -10

        with self.assertRaises(vol.error.MultipleInvalid):
            sv.value = 110

    def test_value_allowed_value(self):
        rc_xml = ET.parse('fixtures/AVTransport_1.xml')
        state_var = rc_xml.find('.//service:stateVariable[service:name="PlaybackStorageMedium"]', NS)
        sv = self.factory.create_state_variable(state_var)

        self.assertEqual(sv.allowed_values, ['NONE', 'NETWORK'])

        # should be ok
        sv.value = 'NETWORK'

        with self.assertRaises(vol.error.MultipleInvalid):
            sv.value = 'NETWORK,NONE'

    def test_value_validate(self):
        rc_xml = ET.parse('fixtures/RenderingControl_1.xml')

        state_var_xml = rc_xml.find('.//service:stateVariable[service:name="Volume"]', NS)
        sv = self.factory.create_state_variable(state_var_xml )

        sv.validate_value(10)
        with self.assertRaises(vol.error.MultipleInvalid):
            sv.validate_value('test')

            state_var_xml = rc_xml.find('.//service:stateVariable[service:name="Mute"]', NS)
        sv = self.factory.create_state_variable(state_var_xml )

        sv.validate_value(True)
        with self.assertRaises(vol.error.MultipleInvalid):
            sv.validate_value('test')


class TestUpnpServiceAction(unittest.TestCase):

    def setUp(self):
        self.hass = object()
        self.factory = UpnpFactory(self.hass)

    def tearDown(self):
        self.factory = None
        self.hass = None

    def get_state_variable(self, state_variable_name):
        rc_xml = ET.parse('fixtures/RenderingControl_1.xml')
        query = './/service:stateVariable[service:name="{}"]'.format(state_variable_name)
        volume_state_var_xml = rc_xml.find(query, NS)
        return self.factory.create_state_variable(volume_state_var_xml)

    def test_init(self):
        state_vars = {
            'A_ARG_TYPE_InstanceID': self.get_state_variable('A_ARG_TYPE_InstanceID'),
            'A_ARG_TYPE_Channel': self.get_state_variable('A_ARG_TYPE_Channel'),
            'Volume': self.get_state_variable('Volume'),
        }

        rc_xml = ET.parse('fixtures/RenderingControl_1.xml')
        action_xml = rc_xml.find('.//service:action[service:name="GetVolume"]', NS)
        action = self.factory.create_action(action_xml, state_vars)

        self.assertIsNotNone(action)
        self.assertEqual(action.name, 'GetVolume')

    def test_valid_arguments(self):
        state_vars = {
            'A_ARG_TYPE_InstanceID': self.get_state_variable('A_ARG_TYPE_InstanceID'),
            'A_ARG_TYPE_Channel': self.get_state_variable('A_ARG_TYPE_Channel'),
            'Volume': self.get_state_variable('Volume'),
        }

        rc_xml = ET.parse('fixtures/RenderingControl_1.xml')
        action_xml = rc_xml.find('.//service:action[service:name="SetVolume"]', NS)
        action = self.factory.create_action(action_xml, state_vars)

        # all ok
        action.validate_arguments(InstanceID=0, Channel='Master', DesiredVolume=10)

        # invalid type for InstanceID
        with self.assertRaises(vol.error.MultipleInvalid):
            action.validate_arguments(InstanceID='0', Channel='Master', DesiredVolume=10)

        # missing DesiredVolume
        with self.assertRaises(KeyError):
            action.validate_arguments(InstanceID=0, Channel='Master')

    def test_format_request(self):
        state_vars = {
            'A_ARG_TYPE_InstanceID': self.get_state_variable('A_ARG_TYPE_InstanceID'),
            'A_ARG_TYPE_Channel': self.get_state_variable('A_ARG_TYPE_Channel'),
            'Volume': self.get_state_variable('Volume'),
        }

        rc_xml = ET.parse('fixtures/RenderingControl_1.xml')
        action_xml = rc_xml.find('.//service:action[service:name="SetVolume"]', NS)
        action = self.factory.create_action(action_xml, state_vars)

        url = 'http://localhost:1234'
        service_type = 'urn:schemas-upnp-org:service:RenderingControl:1'
        headers, body = action.create_request(url, service_type,
            InstanceID=0, Channel='Master', DesiredVolume=10)
        root = ET.fromstring(body)
        ns = {'rc_service': service_type}
        self.assertIsNotNone(root.find('.//rc_service:SetVolume', ns))
        self.assertIsNotNone(root.find('.//DesiredVolume', ns))

    def test_parse_response(self):
        state_vars = {
            'A_ARG_TYPE_InstanceID': self.get_state_variable('A_ARG_TYPE_InstanceID'),
            'A_ARG_TYPE_Channel': self.get_state_variable('A_ARG_TYPE_Channel'),
            'Volume': self.get_state_variable('Volume'),
        }

        rc_xml = ET.parse('fixtures/RenderingControl_1.xml')
        action_xml = rc_xml.find('.//service:action[service:name="GetVolume"]', NS)
        action = self.factory.create_action(action_xml, state_vars)

        service_type = 'urn:schemas-upnp-org:service:RenderingControl:1'
        response = read_file('fixtures/action_GetVolume.xml')
        result = action.parse_response(service_type, {}, response)
        self.assertEqual(result, {'CurrentVolume': 3})


class TestUpnpService(unittest.TestCase):

    def setUp(self):
        self.hass = get_test_home_assistant()
        self.factory = UpnpFactory(self.hass)

    def tearDown(self):
        self.factory = None
        self.hass.stop()

    def construct_service(self):
        dmr_xml = ET.parse('fixtures/dmr')
        scpd_xml = ET.parse('fixtures/RenderingControl_1.xml')
        service_desc_xml = dmr_xml.find('.//device:service', NS)
        url = 'http://localhost:1234'
        return self.factory.create_service(service_desc_xml, url, scpd_xml)

    def test_init(self):
        url = 'http://localhost:1234'
        service = self.construct_service()
        self.assertIsNotNone(service)
        self.assertEqual(service.service_type, 'urn:schemas-upnp-org:service:RenderingControl:1')
        self.assertEqual(service.control_url, url + '/upnp/control/RenderingControl1')
        self.assertEqual(service.event_sub_url, url + '/upnp/event/RenderingControl1')
        self.assertEqual(service.scpd_url, url + '/RenderingControl_1.xml')

    def test_state_variables_actions(self):
        service = self.construct_service()

        state_var = service.state_variable('Volume')
        self.assertIsNotNone(state_var)

        action = service.action('GetVolume')
        self.assertIsNotNone(action)

    def test_subscribe(self):
        service = self.construct_service()
        callback_uri = 'http://callback_uri'
        sid = 'uuid:dummy'

        @asyncio.coroutine
        def run_test():
            received_sid = yield from service.async_subscribe(callback_uri)
            assert sid == received_sid
            assert sid == service.subscription_sid

        @asyncio.coroutine
        def mocked_do_http_request(_a, _b, _c=None, _d=None):
            return mock_coro((200, {'sid': sid}, None))

        service._async_do_http_request = mocked_do_http_request

        self.hass.add_job(run_test())
        self.hass.async_block_till_done()

    def test_unsubscribe(self):
        service = self.construct_service()
        service._subscription_sid = 'uuid:dummy'

        @asyncio.coroutine
        def run_test():
            assert service.subscription_sid == 'uuid:dummy'
            yield from service.async_unsubscribe()
            assert service.subscription_sid == None

        @asyncio.coroutine
        def mocked_do_http_request(_a, _b, _c=None, _d=None):
            return mock_coro((200, {}, None))

        service._async_do_http_request = mocked_do_http_request

        self.hass.add_job(run_test())
        self.hass.async_block_till_done()

    def test_call_action(self):
        service = self.construct_service()
        action = service.action('GetVolume')
        response_body = read_file('fixtures/action_GetVolume.xml')

        @asyncio.coroutine
        def run_test():
            result = yield from service.async_call_action(action, InstanceID=0, Channel='Master')
            assert result['CurrentVolume'] == 3

        @asyncio.coroutine
        def mocked_do_http_request_ok(_a, _b, _c, _d):
            return mock_coro((200, {}, response_body))

        service._async_do_http_request = mocked_do_http_request_ok

        self.hass.add_job(run_test())
        self.hass.async_block_till_done()
