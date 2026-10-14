# Changelog

All notable changes to Idus Elgato Light are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1] – 2026-10-14

### Added
- One-line install: `bash <(curl -fsSL …/releases/latest/download/install-idus-elgato-light.sh)`.
  Run without the app next to it, the installer fetches the app and icon from the latest release.

## [1.0] – 2026-10-14

First public release — the successor of the private WolfLight, rebuilt from scratch in English.

### Added
- Controls Elgato Key Light, Key Light Air, Key Light Mini (with battery level), Ring Light
  and Light Strip (hue + saturation) over their local HTTP API (port 9123).
- Finds lights by mDNS and a quick port scan; on first start it asks before searching and adds
  every light it finds, side by side. Add by IP, extra networks to search (other VLANs),
  rename, reorder, remove, identify (blink). Lights are tracked by serial number, so a new
  IP address from DHCP is picked up automatically.
- Presets for any selection of lights — lights left out are not touched. Four starter presets.
- Shortcuts for OpenDeck: every action as a copy-ready command (full path) and a 144×144 key
  image in the shortcut's colour. Actions: all on / off / toggle, toggle one light, presets,
  brighter / dimmer, warmer / cooler (steps set in Settings).
- CLI for every action (`--on`, `--off`, `--toggle`, `--preset`, `--brightness`, `--kelvin`,
  `--light`, `--list`, `--scan`, `--identify`) and right-click menu actions.
- Slider changes are sent in the background, at most 20 per second per light; the window
  refreshes the lights every 15 seconds (configurable).
- `install-idus-elgato-light.sh`: self-contained folder in `~/.local/share`, menu entry
  with actions, dependency check.
