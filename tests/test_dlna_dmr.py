import asyncio
import pytest
from unittest.mock import MagicMock

from home_assistant_dlna_dmr import DlnaDmrDevice


class MockUpnpStateVariable(object):

    def __init__(self, value, min_value=None, max_value=None):
        self.value = value
        self.min_value = min_value
        self.max_value = max_value

    def validate_value(self, value):
        pass


class MockUpnpAction(object):

    class MockArgument(object):

        def __init__(self, name, related_state_variable):
            self.name = name
            self.related_state_variable = related_state_variable

    def __init__(self, arguments, response_args):
        self.arguments = [
            MockUpnpAction.MockArgument(name, state_var) for name, state_var in arguments.items()
        ]
        self.response_args = response_args

    def argument(self, name):
        for arg in self.arguments:
            if arg.name == name:
                return arg

    @asyncio.coroutine
    def async_call(self, **kwargs):
        return {}


class TestDlnaDmr:

    def createDlnaDmrInstance(self, upnp_device):
        hass = MagicMock()
        url = 'http://localhost:1234'
        callback_view = MagicMock()
        device = DlnaDmrDevice(hass, url, None, callback_view)

        device._device = upnp_device
        device._is_connected = True

        return device

    def createUpnpDeviceInstance(self):
        state_var_current_volume = MockUpnpStateVariable(5, min_value=0, max_value=100)

        action_set_volume = MagicMock()
        action_set_volume.async_call.return_value = {}
        action_set_volume.argument().related_state_variable = state_var_current_volume

        service = MagicMock()
        service.state_variable.return_value = state_var_current_volume
        service.action.return_value = action_set_volume

        device = MagicMock()
        device.service.return_value = service

        return device

    def test_volume_level(self):
        upnp_device = self.createUpnpDeviceInstance()
        device = self.createDlnaDmrInstance(upnp_device)

        assert device.volume_level == 0.05

    @pytest.mark.asyncio
    def test_async_set_volume_level(self):
        upnp_device = self.createUpnpDeviceInstance()
        device = self.createDlnaDmrInstance(upnp_device)

        yield from device.async_set_volume_level(0.05)

        action = upnp_device.service().action()
        action.async_call.assert_any_call(InstanceID=0, Channel='Master', DesiredVolume=5)
