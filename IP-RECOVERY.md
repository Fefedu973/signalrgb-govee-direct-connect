# Recover saved devices after an IP address change

This fork is based on upstream commit `b6bdd8511f72e4c01f6ac4511b66a344cae2928b` (addon version 2.1.4). The IP recovery change first shipped as `2.1.5-local-ip-recovery` and remains in `2.2.0-h6008-ble`, which adds the separately documented optional BLE transport.

## Problem and behavior

The original addon indexes discovery controllers by the configured IP address. When DHCP gives a saved device a different address, its replies cannot reach that controller. The manual Update path also changes the device address without consistently updating discovery's index, saved cache, registration and relay socket.

The patch sends a standard LAN `scan` at startup discovery and at most once per minute. A reply can relocate an existing device only when exactly one saved device matches its case-normalized device identifier. The announced IP must match the UDP source, and a known model must still match. Unknown devices are not enrolled automatically, and an occupied destination address is not overwritten.

The current transport address is stored separately from `controllerId`, which remains the original SignalRGB endpoint identifier. Older saved devices fall back to their existing IP as that identifier. This preserves the key used for enablement and Canvas placement. Both discovery recovery and manual Update use the same replacement path:

1. Remove the registered controller using its existing identifier.
2. Close its relay socket and remove the old address index.
3. Save the new transport address while retaining `controllerId` and device configuration.
4. Allocate a new local relay port, rebuild the controller, and announce it.

Delete continues to accept the stable UI identifier after a device moves. Duplicate scans do not trigger repeated replacement. Persisted settings keep the stable identifier on the next load.

## Install

Add `https://github.com/Fefedu973/signalrgb-govee-direct-connect` in SignalRGB Add-ons. Disable the original Govee Direct Connect addon, enable this fork, then restart SignalRGB once the addon has been downloaded. Both versions use the same service name and UDP discovery port, so only one should be active.

Previously saved devices must have a known device identifier to recover automatically. The patch preserves their existing enabled/disabled state; it does not enable disabled devices. After discovery, the configured transport address should match the device's current address while its existing layout placement remains associated with the same endpoint.

## Tests

Run with Node.js, without extra packages:

```sh
node tests/ip-recovery.cjs
```

The eight tests execute the production addon classes in isolated VM contexts with SignalRGB's host I/O replaced by stubs. They cover three independent address migrations, repeated scans, status/RGB relay routing, malformed or mismatched replies, address collisions, manual updates, scan cadence, persistence and deletion after migration.

All fixtures are generated from documentation-range IP addresses and invented identifiers. They contain no real device captures, account data or network configuration. These tests do not contact devices and do not establish hardware compatibility or optical timing.

## Scope and limitations

This is an address-recovery change. Existing RGB protocol selection and color packet formats remain unchanged. It does not disable firmware fades or add a new H6008 real-time mode.

Discovery requires devices to answer the standard Govee LAN scan and requires LAN Control/network reachability to work. Device identifiers in LAN replies are not cryptographically authenticated. Automatic recovery is designed for a trusted local network; it cannot authenticate an actively forged response. If an address is already assigned to another saved controller, resolve that conflict manually.

Original addon by RickOfficial/fu-raz; original MIT license retained. No upstream pull request accompanies this fork.
