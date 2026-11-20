# Orkli Termowifi integration (unofficial)

This is an unofficial Home Assistant custom integration for Orkli Termowifi controllers.
It provides climate entities for each room exposed by the Termowifi device. This
integration was tested with a single installation and may require adjustments for
other configurations.

## Features

- Exposes each room as a `climate` entity
- Supports HVAC modes: heat, cool, off
- Set target temperature from Home Assistant
- Reports current temperature and humidity (when available)
- Device information registered in the device registry (manufacturer: Orkli)

## Requirements

- Home Assistant (use the latest stable release)
- The Termowifi device reachable on your local network (IP and port)

{% if not installed %}

### Installation:

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=diegogd&repository=orkli-termowifi-hass-integration&category=integration)

* Go to HACS -> Integrations
* Click the three dots on the top right and select `Custom Repositories`
* Enter `https://github.com/diegogd/orkli-termowifi-hass-integration` as repository, select the category `Integration` and click Add
* A new custom integration shows up for installation (Orkli Termowifi) - install it
* Restart Home Assistant

{% endif %}

## Configuration

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=orkli_termowifi)

* Go to Configuration -> Integrations
* Click `Add Integration`
* Search for `Orkli Termowifi` and select it
* Choose **Search automatically** to scan the local network, or **Enter the address** to type the IP and port

### Discovery

- **DHCP:** Home Assistant offers the TermoWifi in *Discovered* when it sees a
  device with an Espressif MAC prefix (`F4:CF:A2`) that answers like a
  TermoWifi on port 12345. The entry follows the device if its IP changes.
  Other Espressif devices are checked and ignored.
- **Search:** scans the networks of Home Assistant (up to a /22; larger networks
  are reduced to the /24 around Home Assistant) for port 12345. It can take
  up to a minute.
- The device does not announce itself (no mDNS or UDP discovery), so a TermoWifi
  with a different MAC prefix is only found by the search.
- To check a device, the integration connects to port 12345 and sends the
  read-only status dump request (`3B 01 FE 04 23 00 26`). Any other device on the
  network listening on that port receives these bytes during a search.
- A device set up by hand gets its MAC the first time DHCP sees it. If its IP
  changed and it is the only entry set up by hand that lost its connection,
  the entry is moved to the new IP.

To change the IP address or port later, use **Reconfigure** on the integration entry. The entities and automations are kept.

Note: This integration uses config entries (UI setup). Do not add platform configuration to `configuration.yaml`.

## Configuration example

When entering the address you will be asked for:

- `IP address` — Termowifi device IP
- `Port` — Termowifi device TCP port (12345)

The device is checked before the entry is created. If the check fails, submit
the same address again to add it anyway.

Entity unique IDs follow the pattern: `{config_entry_id}_{room_id}`

## Logging and troubleshooting

If entities do not appear or do not update:

- Ensure the device IP/port are correct and accessible from the Home Assistant host.
- Enable debug logging for the integration to see detailed messages. Add the following
  to your `configuration.yaml`:

  ```yaml
  logger:
    default: info
    logs:
      custom_components.orkli_termowifi: debug
  ```

- Check Home Assistant logs for connection errors or exceptions.
- If you see threading warnings about hass.async_create_task being called from a
non-event-loop thread, update to the latest integration code that schedules work
with `hass.loop.call_soon_threadsafe(...)` or report the issue with logs attached.

## Development
- Add the integration directory to config/custom_components/ while developing.
- Restart Home Assistant after code changes or use the integration reload flow where applicable.
- Run tests with pytest; follow repository testing conventions.

## Releasing

HACS detects updates from GitHub releases. To publish a new version:

1. Bump `version` in `custom_components/orkli_termowifi/manifest.json`.
2. Push to `master` and create a release whose tag is `v<version>` (e.g. `v0.2.0`).

## Recommended commands (from the repository root):

- Run linters: pre-commit run --all-files
- Run tests for this integration (if tests exist): `pytest ./tests/components/orkli_termowifi --maxfail=1 -q`

## Contributing

Contributions and bug reports are welcome. Please open issues or pull requests on the
project repository. When reporting issues include:

- Home Assistant version
- Integration debug logs
- Device model/firmware (if known)
- Steps to reproduce

## License
See the repository LICENSE file for license details.

## Disclaimer

This integration is unofficial and provided as-is. Use at your own risk. It may require changes to work with different Termowifi firmware versions or network setups.
