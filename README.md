# SolarEdge Modbus Multi

[![hacs_badge](https://img.shields.io/badge/HACS-Default-41BDF5.svg?style=for-the-badge)](https://github.com/hacs/integration)

This integration provides Modbus/TCP local polling to one or more SolarEdge inverters for Home Assistant.
Each inverter can support three meters and three batteries over Modbus/TCP. It works with single inverters,
multiple inverters, meters, and batteries. It has significant improvements over similar integrations, and
`solaredge_modbus_multi` is actively maintained.

By default, only features which are officially documented by SolarEdge are enabled: inverters,
synergy inverters, and meters. All of the battery (read only) and control features (read/write battery and
limit controls) can be enabled by configuring the hub after you add it to your Home Assistant. Support for
batteries and controls is from documentation not publicly available from SolarEdge or through user
discovery and may not be supported by SolarEdge.

## Features

- Inverter support for 1 to 32 SolarEdge inverters.
- Meter support for 1 to 3 meters per inverter.
- Battery support for 1 to 3 batteries per inverter.
- Supports site limit and storage controls.
- Automatically detects meters and batteries.
- Supports Three Phase Inverters with Synergy Technology.
- Polling frequency configuration option (1 to 86400 seconds).
- Auto-discovers inverters via Fast Scan (IDs 1–32), Complete Scan (IDs 1–247), or manual device ID list.
- Connects locally using Modbus/TCP - no cloud dependencies.
- Informational sensor for device and its attributes
- Supports status and error reporting sensors.
- Contains a failing device so it cannot take the rest of the poll with it.
- User friendly: Config Flow, Options, Repair Issues, and Reconfiguration.

### Partial updates

The inverter, each of its meters and each of its batteries are polled
separately, so a device that does not answer no longer takes the others with
it: everything else on the same inverter still refreshes, and only that
device's entities go unavailable. One slow meter no longer blanks every sensor
on the hub. Nothing stale is published in the meantime — the readings that
failed are held back rather than repeated as if they were fresh — and the log
names the device and the error behind it. Only losing the connection itself
fails the whole update.

Energy totals are the exception: they stay available and hold their last
reading whenever a device stops answering, including overnight when the
inverter powers down entirely. An unavailable total-increasing sensor puts a
gap in Home Assistant's long-term statistics and the energy dashboard, so a
counter keeps its value and the connectivity entities report whether the
hardware is actually there.

Read about more features on the wiki: [WillCodeForCats/solaredge-modbus-multi/wiki](https://github.com/WillCodeForCats/solaredge-modbus-multi/wiki)

## Installation

Install with [HACS](https://hacs.xyz): Search for "SolarEdge Modbus Multi" in the default repository,

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=WillCodeForCats&repository=solaredge-modbus-multi&category=integration)

OR

Download the [latest release](https://github.com/WillCodeForCats/solaredge-modbus-multi/releases) and copy the `solaredge_modbus_multi` folder into to your Home Assistant `config/custom_components` folder.

After rebooting Home Assistant, this integration can be configured through the integration setup UI. It also supports options, repair issues, and reconfiguration through the user interface.

### Configuration

[WillCodeForCats/solaredge-modbus-multi/wiki/Configuration](https://github.com/WillCodeForCats/solaredge-modbus-multi/wiki/Configuration)

Inverter site limit and battery storage controls are disabled by default: not all inverters support controls. You will need to enable Power Control Options after adding your inverter hub in the integration.

### Documentation

[WillCodeForCats/solaredge-modbus-multi/wiki](https://github.com/WillCodeForCats/solaredge-modbus-multi/wiki)

### Minimum Required Versions

- Home Assistant 2025.2.0 (HA=>2025.9.0 requires release v3.1.7 or newer)
- modbus-connection 4.6.1 (installed automatically; replaces the direct
  pymodbus dependency of earlier releases)

## Specifications

[WillCodeForCats/solaredge-modbus-multi/tree/main/doc](https://github.com/WillCodeForCats/solaredge-modbus-multi/tree/main/doc)

## Project Sponsors

- [@bertybuttface](https://github.com/bertybuttface)
- [@dominikamann](https://github.com/dominikamann)
- [@maksyms](https://github.com/maksyms)
- [@pwo108](https://github.com/pwo108)
- [@barrown](https://github.com/barrown)
