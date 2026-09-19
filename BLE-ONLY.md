# Govee Bluetooth Only

`GoveeBluetoothOnly.js` and its matching `.qml` are an independent SignalRGB network service for allowlisted, single-zone Bluetooth devices. They do not change Govee Direct Connect or its validated H6008 realtime implementation. The initial backend profile is intended for the non-addressable H6159 shelf strip; this client does not contain that model, its identity, a Bluetooth address or a key.

The local bridge advertises supported devices on UDP `127.0.0.1:47684`. The catalogue is refreshed every five seconds. A profile must declare `transport:"ble"`, `leds:1`, `capabilities.rgb:true` and `capabilities.addressable:false`. Names, models, profile IDs and stable device IDs come from the local allowlist. The SignalRGB controller ID is `govee-ble:` followed by the stable device ID. Future compatible single-zone profiles require backend/profile configuration, not another JavaScript model list. Addressable/multi-zone devices are rejected.

The renderer exposes one Canvas/Forced color. Brightness is displayed only when `capabilities.brightness` is true. Shutdown always offers Hold current color / Single color; Release control requires `restore:true`, and Turn device off requires `power:true`. Release control remains the default for the initial H6159 profile; profiles without restoration default to Hold current color. An incompatible saved setting shows an alert and falls back to the latest rendered Canvas/Forced RGB. If no color has been rendered, it sends no invented RGB and lets the bridge lease expire. Unsupported off/restore commands are never submitted, including during error recovery.

Ordinary device transitions remain active; this is not the H6008 anti-fade transport. Color requests are limited to 10 per second with one color request awaiting an ACK and latest-frame coalescing. Unchanged colors are suppressed; a heartbeat renews the lease every 500 ms. The backend owns initial power-on, serialized device commands, snapshots and restoration. Optional brightness and power fields are sent only when the profile declares those capabilities. Hold current color means the latest Canvas/Forced color known to the client, not a hardware readback.

Only correlated replies from the local bridge are accepted. A missing response after two seconds or acquisition longer than fifteen seconds pauses colors, sends a best-effort release and displays an alert. Automatic retry waits thirty seconds. There is no LAN fallback. A bridge restart reporting an idle session causes a fresh color request so a static scene can recover.

`paused_off` is a valid state even when `ready:false`; it continues heartbeats without an acquisition deadline or automatic restore. `connecting`, `initializing` and `reconnecting` may carry transient errors while the bridge performs its bounded attempts; those errors retain the fifteen-second acquisition deadline rather than immediately forcing a release. Preserving an externally observed power-off during lease expiry/restoration is the backend's responsibility.

## Protocol

Discovery request: `{id,op:"catalog"}`. Reply: `{id,ok:true,devices:[{device,profile,model,name,transport:"ble",leds:1,capabilities:{rgb:true,addressable:false,brightness:true,power:true,restore:true}}]}`. At most sixteen catalogue entries are accepted. The backend excludes devices already owned by the H6008 client.

Color request: `{id,op:"strip_colors",device,rgb:[R,G,B],brightness:0..100,on:true}`. Heartbeat: `{id,op:"heartbeat",devices:[device]}`. The backend interprets power intent at acquisition, not as a repeated forced power-on command.

Shutdown: `{id,op:"strip_release",device,mode:"restore"|"color"|"off",rgb?}`. Replies use `{id,ok,devices:[{device,state,ready,error,restore_error}]}`. An ACK confirms processing at the local bridge, not optical frame delivery. Shutdown is best effort because SignalRGB closes the renderer socket; the backend lease provides the independent cleanup deadline.

## Architecture and adding a profile

The JavaScript service performs catalogue discovery, capability filtering and SignalRGB controller registration. Each renderer sends only a stable device ID and the requested action to the shared local UDP API. The backend allowlist resolves that ID to a registered profile and a dedicated session worker; its transport owns the BLE connection. Protocol/authentication details and restoration snapshots stay in the backend, not in the SignalRGB service or QML.

The backend `profiles.py` registry distinguishes command families, authentication requirements, minimum intervals and capabilities. The current classic family uses its own command builders and snapshot/restore codec; H6008 keeps its separately validated authenticated session. H6008 entries have `bluetooth_only:false` and are excluded from this catalogue, preventing two SignalRGB clients from taking control of the same bulb.

To add a future device:

1. Establish its actual command format, authentication, supported state queries and restoration behavior from protocol evidence. A shared model prefix or manufacturer name is not enough.
2. Register a unique profile ID with the supported capabilities and proven command/codec functions. A device using an existing compatible family can reuse that family. A different protocol/authentication or state lifecycle requires another backend session/transport implementation and an explicit factory registration.
3. Add its stable ID, display name, profile ID and Bluetooth address to the private backend allowlist. Keep addresses, session keys and account data out of the public plugin. The catalogue will expose only the metadata needed by SignalRGB.
4. Add byte fixtures, validation and session lifecycle tests, then verify discovery, color, brightness/power where supported, and shutdown/restoration on authorized hardware. Only after those checks should the profile be described as supported.

The current classic worker requires a restorable snapshot for its safe lease lifecycle. Although the client correctly handles a catalogue entry declaring `restore:false`, that does **not** mean such a profile can be inserted into the existing classic worker without additional backend work. Likewise, addressable/multi-zone profiles remain outside this client's contract. No claim is made that all Govee Bluetooth devices work.

## Validation

Run `node tests/bluetooth-only.cjs`. **Twenty-one VM/contract tests pass**, executing the actual plugin source. They cover discovery, stable JSON identities, topology filtering, sender/ACK checks, cadence/coalescing, heartbeat, outage/retry, acquisition timeout, bridge restart, 120 seconds paused externally off, recovery from transient reconnect errors, shutdown policies, optional capabilities, incompatible saved settings and the QML method contract. Tests perform no network/Bluetooth traffic.

The QML checks validate its companion-file contract; they are not a SignalRGB GUI render test. Real discovery and physical H6159 behavior still require the backend and the authorized hardware validation. No installer or remote publication is performed by these files.
