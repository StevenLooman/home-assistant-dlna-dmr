# home-assistant-dlna-dmr
DLNA Media Renderer component

Note: this is a work in progress. Currently only tested with my own Samsung TV, YMMV! Please test and create issues.

## Installation and configuration
Copy (or symlink) `dlna_dmr.py` to `~/.homeassistant/custom_components/media_player`.

Add this to your `~/.homeassistant/configuration.yaml`:
```
media_player:
  - platform: dlna_dmr
    url: http://192.168.178.71:9197/dmr
```

Replace `http://192.168.178.71:9197/dmr` with the URL to the UPnP description XML.

## Notes
- Still in development, YMMV!
- Feedback/testing is appreciated!
- I'll create a pull request for automatic discovery to `netdisco` as soon as this component is more mature.

# License
Licensed under Apache 2.0.
