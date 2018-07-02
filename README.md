# home-assistant-dlna-dmr
DLNA Media Renderer component

Note: this is a work in progress. Currently only tested with my own Samsung TV, YMMV! Please test and create issues.

# Note: Currently being integrated into Home Assistant

Currently, this module is currently being integrated into Home Assitant. See this [pull request](https://github.com/home-assistant/home-assistant/pull/14749).

If you wish to help, please test the module (`dlna_dmr.py`) from that pull request and report any findings.

## Installation and configuration
Symlink (or copy) `home_assistant_dlna_dmr/dlna_dmr.py` to your Home Assistant configuration folder (e.g., `~/.homeassistant/custom_components/media_player`):
```
ln -s `pwd`/home_assistant_dlna_dmr/dlna_dmr.py ~/.homeassistant/custom_components/media_player/dlna_dmr.py
```

Add this to your `~/.homeassistant/configuration.yaml`:
```
media_player:
  - platform: dlna_dmr
    url: http://192.168.178.71:9197/dmr
    name: My DLNA Player
```

Replace url (`http://192.168.178.71:9197/dmr`) with the URL to the UPnP description XML.

## Notes
- Still in development, YMMV.
- Only tested with my own TV (Samsung). This TV only updates via UPnP if the stream was opened via a remote UPnP device (e.g., BubbleUPnP), not when a stream is opened via the local UPnP browser!
- Feedback/testing is appreciated.
- I'll create a pull request for automatic discovery to `netdisco` as soon as this component is more mature.
- Buttons (play, pause, stop) are visible dependent on the reported status of the device. If it is playing something, these button will show up.

# License
Licensed under Apache 2.0.
