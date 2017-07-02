# home-assistant-dlna-dmr
DLNA Media Renderer component

Note: this is a work in progress. Currently only tested with my own Samsung TV, YMMV! Please test and create issues.

## Installation and configuration
Copy (or symlink) `dlna_dmr.py` to `~/.homeassistant/custom_components/media_player`.
Copy (or symlink) `upnp_client.py` to `~/.homeassistant/custom_components/media_player`.

Add this to your `~/.homeassistant/configuration.yaml`:
```
media_player:
  - platform: dlna_dmr
    url: http://192.168.178.71:9197/dmr
```

Replace `http://192.168.178.71:9197/dmr` with the URL to the UPnP description XML.

## Notes
- Still in development, YMMV.
- Only tested with my own TV (Samsung). This TV only updates via UPnP if the stream was opened via a remote UPnP device (e.g., BubbleUPnP), not when a stream is opened via the local UPnP browser!
- Feedback/testing is appreciated.
- I'll create a pull request for automatic discovery to `netdisco` as soon as this component is more mature.

# License
Licensed under Apache 2.0.
