# Govee BLE companion — explicit device profiles

The companion supports the existing authenticated H6008 realtime path and the
classic H6159 single-zone strip. Both use the same loopback server and private
allowlist; their packet encoders and sessions remain separate. No communication
key is needed for the tested H6159 revision. No H6008 anti-fade command is sent
to that strip. The default color rate is at most 10 writes per second.

## Bluetooth-only integration

Install `GoveeBluetoothOnly.js` together with its companion `.qml` from the
SignalRGB addon. The client discovers configured single-zone profiles from
`op:catalog`; it contains no hardcoded model list or private Bluetooth address.
One non-addressable strip appears as one RGB zone with its configured name.
Supported brightness and shutdown controls are selected from profile metadata.

To register a device, identify its actual Bluetooth address and compatible
profile first, then run the helper against a PRIVATE configuration:

```powershell
python configure_device.py --list-profiles
python configure_device.py --config C:\private\config.local.json --device shelf --profile h6159-classic-v1 --address 00:00:00:00:00:03 --name "Shelf strip"
```

The address above is synthetic. The second command previews and validates.
Append `--apply` to write atomically with a private backup. Restart the companion
after changing its allowlist. An installed generic SignalRGB client refreshes
its catalogue automatically; first installation of the new plugin can require
a SignalRGB restart. `config.classic.example.json` is a keyless starting point
when only classic strips are used. Existing H6008 entries and credentials are
preserved by the helper.

Classic requests are `strip_colors` with `device`, `rgb`, and supported
`brightness`/explicit `on`, and `strip_release` with mode `restore`, `color` or
`off`. Acquisition takes a full snapshot before changing anything. Initial
power-on is performed once if requested; a later external OFF is respected.
The same two-second lease and 500 ms heartbeat apply. Keep Govee Home disconnected
from the target during PC control, since simultaneous BLE ownership can conflict.

See [EXTENDING.md](EXTENDING.md) for adding profiles, transport families, required
fixtures and hardware validation. A new model sharing a supported protocol needs
a profile and private configuration, not another SignalRGB plugin. This does
not claim support for all Govee devices or every revision sharing a model name.

Tests: `python -B -m unittest discover -p "test_*.py" -v` (no real lights).

## H6008-specific protocol and behavior

### Recovery from missing Windows GATT characteristics

The 20 September afternoon fix forces `winrt={"use_cached_services": false}`
for H6008 connections. A new client otherwise can reuse the Windows attribute
cache; the [Bleak WinRT documentation](https://bleak.readthedocs.io/en/latest/api/args.html#bleak.args.winrt.WinRTClientArgs.use_cached_services)
defines `false` as reading that database from the remote device. The transport
validates the expected service, notification characteristic, control
characteristic and their properties before beginning E7 authentication.

Cleanup previously called `stop_notify` even when subscription had failed.
That could replace the original error with `Characteristic ...2b10 was not
found!`, obscuring why the connection failed. Cleanup now only unsubscribes
after a successful subscription, always attempts to disconnect, and preserves
the original connection/authentication exception. Old queued notifications
are discarded before beginning a new session.

Two H6008s were observed stuck on this error while the third and H6159 still
worked. After a clean companion-only reload, all three H6008s resumed streaming
without an error; H6159 also resumed and later confirmed its normal blackout
state. The regression suite passes **110 tests**, including seven GATT/cleanup
tests. See [the validation record](validation/gatt-recovery-20260920.json).
An incomplete Windows cache remains a likely explanation, not a proven root
cause: the former cleanup could mask the first exception and the recovery
also restarted the companion. This short observation does not establish
hours-long reliability. No LAN-color fallback, radio reset, firmware change
or retry-limit increase was introduced.

### Recovery after a PC restart

A bulb can remain in realtime mode `05` while a Windows restart discards the
companion's in-memory static-color snapshot. Older versions refused this state,
so the addon retried without ever becoming ready. An explicit acquisition with
an RGB color now establishes that requested color as a **new** static `0D`
baseline, verifies it through authenticated `AA05` readback, and then enters
realtime mode again. This does not recover or claim to restore the color from
before the PC reboot. Release restores the newly verified baseline; power and
brightness remain under LAN control.

This recovery requires a verified `AA14` identity and an explicitly requested
RGB. Ordinary mode `0D` also accepts white-temperature settings: its complete
authenticated payload, including Kelvin, is preserved for release. The original
RGB-only guard incorrectly rejected the observed 6500 K state. Unknown scenes
remain rejected.
A release or expired lease during the transition prevents realtime activation.
The status exposes `last_mode_hex`, `recovery_baseline` and `recovery_pending`
without exposing authentication keys.

The transport also retries a missing initial `E701` reply once on the same BLE
connection before reconnecting. Timeout messages identify the expected reply
instead of returning an empty error. This is bounded retry of authentication,
not a LAN-color fallback or a firmware change.

The 20 September regression tests cover loss of the first authentication reply,
wrong identity, a fresh process finding mode `05`, readback mismatch, lease expiry
and explicit release during recovery. Run the full Python test suite as above.

The following sections describe the H6008 worker. Its authentication, LAN power
ownership and anti-fade initialization do not apply to the classic H6159 worker.

This bridge keeps up to three H6008 BLE sessions alive and accepts RGB frames from a local controller such as SignalRGB. The H6008 `05/01` initialization and `05/00/RGB` realtime path were recovered from Govee Home7.6.21 and visually validated on hardware/firmware1.07.03/1.01.25. The ordinary `0D` color command fades on the tested bulb. A targeted LAN `ptReal` test with the same plaintext packets produced no visible color change on that bulb.

All three authorized H6008 bulbs passed a hardware acceptance run on19September2026: exact identity/snapshot checks,16RGB frames each at2fps, then verified restoration of all original states. A subsequent continuous HSV benchmark measured9.76–9.86 completed writes/s with10fps requested and13.90–14.10/s with20fps requested. The user found both fluid, with no visible difference; all original states were again restored. Keep the production default at100ms/10fps. Final SignalRGB runtime integration remains a separate validation step. It does not flash firmware, use a cloud account, or automatically power on bulbs. **LAN remains responsible for power and brightness.**

## Start

Install `requirements.txt`. Copy `config.example.json` to a private file **outside this public code directory** and fill in the locally recovered communication key and allowlisted identities. Do not commit real configuration or keys. The key is used only for the E7 handshake; each connection gets its own session key, held only in memory.

```powershell
python bridge.py --config ../ble-bridge.local.json
python bridge.py --config ../ble-bridge.local.json --serve
python bridge.py --config ../ble-bridge.local.json --serve --seconds 600 --stop-file ../bridge.stop
```

The first command validates configuration without network/BLE activity. Only `--serve` opens the loopback API. Even then, no bulb connection starts until an acquisition or color message arrives. `--seconds` and `--stop-file` offer clean bounded shutdown; use a stop-file path that does not already exist. A private configuration may specify `python_libs` as a path relative to that configuration when dependencies were installed in a local target directory.

Windows launchers keep the configuration external and can run without a visible console:

```powershell
.\Start-Bridge.ps1 -ConfigFile C:\private\ble-bridge.local.json -Python python -Background
.\Stop-Bridge.ps1 -ConfigFile C:\private\ble-bridge.local.json
```

Runtime logs and a small process/stop-marker record are stored next to the private configuration, never inside the distributable. No key is printed. The stop script requests serialized cleanup; it does not forcibly kill Python. No startup task is installed by these scripts.

The process listens exclusively on UDP `127.0.0.1:47684` by default. It rejects external addresses, packets larger than4096bytes, unknown identities, duplicate identities, noninteger/out-of-range RGB, and implicit power flags. An optional `api_token` can be configured; if present, each request must include the matching `token`. The default is no token because the endpoint is strictly local and controls only allowlisted colors.

## API

Use a single UDP socket bound to an ephemeral loopback port, so replies arrive on the same socket. Every response echoes `id`. A color message acquires/renews the stream and replaces the previous pending frame:

```json
{"id":1,"op":"colors","colors":[{"device":"STABLE_ID","rgb":[20,40,60],"on":true}]}
```

Each message supports one to three allowlisted devices. `on:true` permits streaming but never sends a power-on command. If AA01 reports the bulb is off, state becomes `paused_off`; the bridge resumes once LAN/the user powers it on. `on:false` releases the stream without restoring color, leaving the LAN shutdown sequence in control.

Keep static colors alive with a heartbeat every500ms. The default lease expires after2seconds without a color/heartbeat:

```json
{"id":2,"op":"heartbeat","devices":["STABLE_ID"]}
{"id":3,"op":"status"}
```

`acquire` uses the same `devices` list and can establish a session before frames. `colors` already performs acquisition, so separate acquire is normally unnecessary.

Release restores the original static RGB mode by default. For a deliberate LAN handoff, set `restore:false`. For a shutdown color, supply `final_rgb`; it is written as normal mode0D after pending streaming work has stopped, without a competing LAN color command:

```json
{"id":4,"op":"release","devices":["STABLE_ID"]}
{"id":5,"op":"release","devices":["STABLE_ID"],"restore":false}
{"id":6,"op":"release","devices":["STABLE_ID"],"restore":false,"final_rgb":[255,100,20]}
```

A release ACK is sent after serialized cleanup, with a12second deadline. If `final_rgb` arrives before the BLE connection is ready, the bridge allows one bounded10second connection/authentication attempt and then writes0D directly, without entering05 or changing power. A failure is reported in `restore_error`; it is not reported as a successful final color. During normal operation wait for the ACK before sending a replacement LAN color. For an immediate power-off, `release restore:false` followed by LAN `turn=0` cannot be undone by the bridge because the bridge never emits power commands. On process shutdown, completed LAN handoffs remain respected.

Response:

```json
{"id":1,"ok":true,"devices":[{"device":"STABLE_ID","state":"streaming","ready":true,"last_sent_rgb":[20,40,60],"error":null,"restore_error":null,"failures":0,"power":1,"lease_remaining_ms":1950}]}
```

States: `idle`, `connecting`, `initializing`, `ready`, `streaming`, `paused_off`, `reconnecting`, `releasing`, `failed`. `ok:true` acknowledges API processing, **not receipt of a color by the bulb**. `ready:true` means its authenticated, identity-verified BLE session is available. `last_sent_rgb` records a completed BLE write operation, not a per-frame optical measurement. Errors are exposed without keys.

The addon should wait while correlated ACKs show connection/initialization, bounded by its overall acquisition deadline. Do not send `colorwc` concurrently with an active BLE stream. A fallback must first release; pending connection/query/initialization is canceled and activity is checked before mode/color writes to prevent a delayed frame after the handoff. `paused_off` is not evidence that BLE is unavailable and must not trigger an automatic power-on fallback.

## Session and restoration behavior

Every new BLE connection performs E701/E702 and reads AA14. The returned WiFi MAC must match the configured allowlist entry before mode/color writes. The first acquisition reads AA05, AA01 and AA04. Unknown scenes and Kelvin mode are refused because this version cannot promise exact restoration of those states.

On Windows, an explicit `windows_direct_address:true` setting permits an allowlisted address to connect through Bleak's public BLEDevice constructor after a missed advertisement. WinRT then requests that known Bluetooth address directly. Authentication and AA14 remain mandatory. This does not pair devices or reset the adapter. Without that option, repeated discovery failures wait for a real new scan instead of consuming three attempts against the same cached scan.

One worker serializes each bulb's queries and writes. It initializes05/01 once per connection/stream, coalesces pending frames, suppresses identical RGB, and enforces at least100ms between color frames. This conservative10fps starting point matches the app's palette throttle; it is not a measured hardware limit. Status reads keep a static session alive without retransmitting identical colors.

Reconnection is bounded to three failed attempts per acquisition with backoff. A successful reconnect revalidates AA14, reinitializes05/01 and resends the latest RGB while preserving the original restoration snapshot. An explicit release followed by a new acquisition permits another bounded attempt cycle. A restore:false handoff retains the previous known snapshot in memory when the bulb still has this bridge's realtime mode; a later static color reported by LAN refreshes it on acquisition.

On lease expiry/clean shutdown, the original static color is restored when a connection is available. Power and brightness are never written. If restoration is impossible, `restore_error` and `failed` report that fact; success is not fabricated. Abrupt process termination or loss of Bluetooth cannot guarantee restoration. Restarting while the bulb is already in an unknown realtime/scenes mode is refused until a known static RGB state is established.

Implicit lease expiry never opens a new connection to restore later, because a client may already have returned to LAN. An explicitly requested release can make one bounded authenticated reconnection and waits for its cleanup ACK. A shutdown release can promote an earlier fallback release; requests are serialized by revision so the final policy is retained.

For an explicit benchmark only, `experimental_20fps:true` allows `minimum_interval_ms:50`; production defaults remain100ms. A status request with `metrics:true` adds the last256completed-color-write timestamps per bulb. Every ordinary status contains `frame_writes` and `last_write_monotonic`. Initialization/status writes are excluded. Counters measure completed host BLE writes, not optical frame confirmation.

## Offline verification

```powershell
python -B -m unittest discover -p "test_*.py" -v
```

The 25 tests in `test_bridge.py` replace BLE endpoints and clocks and perform no radio/network I/O. Coverage includes encrypted handshake, rejected identity, three independent bulbs, coalescing/cadence, static heartbeat, OFF→ON, reconnect/reinitialization, bounded failure, release during connection, expiry during initialization, shutdown color ordering and unavailable restoration. Synthetic test keys are not OEM keys.

The four Windows tests in `test_udp_windows.py` use real ephemeral sockets bound only to `127.0.0.1`, with no LAN or Bluetooth traffic. They verify client socket churn, the CPython Proactor failure path using an injected receive error, recovery on the same Bridge instance, and bounded failure after three unsuccessful rebinds. All 29 tests passed on CPython 3.12.14; see `UDP-VALIDATION.md` for evidence and limitations.

## Windows UDP listener recovery

An observed failure left the old Python process alive and its UDP port bound, while repeated status requests received no reply. BLE frame writes stopped immediately before a SignalRGB restart. CPython 3.12.14's Proactor datagram receive callback can stop scheduling reads after an `OSError` without closing the socket. The same open-but-unresponsive state is reproduced in the tests by injecting an error at the actual Proactor receive operation.

An ICMP port-unreachable notification after a client socket closes is one possible trigger. It was **not reproduced as an operating-system-generated error on this PC**. The bridge now explicitly disables Windows UDP connection-reset notifications through `WSAIoctl(SIO_UDP_CONNRESET, FALSE)` as a defense. Microsoft's [Winsock IOCTL documentation](https://learn.microsoft.com/en-us/windows/win32/winsock/winsock-ioctls) specifies this behavior. Python's restricted `socket.ioctl` wrapper on the tested runtime does not expose that operation, so the bridge uses the typed WinSock API directly.

Any reported UDP error is logged with its exception type and numeric error codes only. The supervisor closes the failed listener and recreates it on the same loopback port while retaining the existing Bridge and BLE sessions. Recovery permits at most three attempts in 30 seconds. If recovery fails repeatedly, the process performs normal BLE cleanup and exits with an error instead of remaining silently bound. UDP ACKs queued at closure can be lost; clients must correlate replies and retry. The bridge does not change firewall settings or listen on LAN interfaces.

Transport facts are documented in the companion local investigation. The public [govee2mqtt LAN implementation](https://github.com/wez/govee2mqtt/blob/main/src/lan_api.rs) and [author's ptReal explanation](https://github.com/egold555/Govee-Reverse-Engineering/issues/11) establish the LAN envelope; they do not guarantee H6008 firmware acceptance. No unverified LAN variants are tried by this bridge.
