# Govee Direct Connect

[![Click here to add this repo to SignalRGB](https://raw.githubusercontent.com/SRGBmods/QMK-Images/main/images/add-to-signalrgb.png)](https://srgbmods.net/s?p=addon/install?url=https://github.com/Fefedu973/signalrgb-govee-direct-connect)

## IP recovery fork

This fork adds automatic recovery when a previously configured device receives a new LAN address. It preserves the existing SignalRGB controller identity so enablement and Canvas placement can remain attached to the device. It also fixes the manual IP update path.

Install this fork through the button above or add `https://github.com/Fefedu973/signalrgb-govee-direct-connect` in SignalRGB Add-ons. Disable the original Govee Direct Connect addon before restarting so only one discovery service owns UDP port 4002. Existing saved devices are reused through the unchanged service name.

The IP-recovery portion uses standard LAN discovery. It does not remove firmware color transitions, introduce a new bulb protocol, or claim support for additional hardware. See [the implementation and validation notes](IP-RECOVERY.md).

## Optional H6008 BLE realtime transport

An optional **H6008 BLE realtime bridge** setting routes H6008 single-color devices through a separately configured local BLE companion. It is **off by default**. Other models and the existing LAN protocols remain unchanged. The companion source is provided in [`ble-companion/`](ble-companion/README.md), but it must be installed, privately configured and run separately; enabling the SignalRGB option alone does not start it. See [the transport contract and validation notes](H6008-BLE.md).

While enabled, a companion outage holds the last color, shows an alert and retries automatically every 30 seconds. It does not silently resume fading LAN colors. Disable the option explicitly to restore LAN rendering. The device page shows the active transport and local request/reply counters.

## Bluetooth-only client and companion profiles

The separate [Govee Bluetooth Only client](BLE-ONLY.md) discovers allowlisted, single-zone RGB profiles from the same local companion. Its initial classic profile targets H6159 and retains normal device transitions; it does not enable the H6008 realtime protocol. Only profiles explicitly declared by the backend are accepted. Addressable/multi-zone devices and unverified models are not covered by this client.

Read the [companion setup instructions](ble-companion/README.md) before enabling a Bluetooth controller. No real Bluetooth addresses, communication keys or private configuration are included in this repository. Keep those in a separate local configuration. The [backend extension guide](ble-companion/EXTENDING.md) and [client architecture notes](BLE-ONLY.md#architecture-and-adding-a-profile) explain how to add a proven compatible profile without another JavaScript model list; a new protocol family may require backend code and hardware validation.

## Durable Windows companion installation

Use a permanent checkout for the Bluetooth companion. With Python 3.13 x64, create a virtual environment at the repository root and install the tested Windows dependency lock:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r ble-companion/requirements-windows-py313.lock.txt
```

Keep the personal configuration outside the repository at `%LOCALAPPDATA%\GoveeBleBridge\config.local.json`; follow the [companion guide](ble-companion/README.md) for configuration and supported profiles. Do not carry forward a `python_libs` override pointing to an older dependency directory when using this virtual environment. `Lancer-pont.cmd` starts this checkout with its local virtual environment, and `Arreter-pont.cmd` requests a clean shutdown.

When the companion is installed alongside `SignalRGB-Local-Bridges`, its private configuration instead lives in that repository's ignored `local-installation/govee/config.local.json`. Both local launchers prefer this shared location when present. Explicit `-ConfigFile` always takes precedence. This avoids MSIX application-data virtualization: a Windows logon task and a packaged application's child process must see the same actual file, not two virtualized AppData copies.

For automatic startup at Windows sign-in, use the shared [SignalRGB Local Bridges supervisor](https://github.com/Fefedu973/SignalRGB-Local-Bridges/tree/main/startup). It supplies the private configuration and Python paths explicitly, checks for existing bridge processes and retries with backoff. The SignalRGB addon and the companion are separate installations: updating the addon alone does not update this checkout.

## LAN setup

This SignalRGB Addon allows you to add Govee devices via a direct IP connection. You control the amount of leds of the device and what protocol is used to communicate with the device. You can even use components to build your exact Govee Glide setup.

You should make sure your device is connected to your wifi via the app, you have refreshed the light segments and have turned on LAN Control. It works best if the device has an assigned IP by reserving an IP in your router.

If your device consists of multiple components like the Govee Bars, you can select 'Duplicate'. The amount of LEDS you enter should then be the amount of 1 bar. For example: The H6046 has two bars and each bar has 10 LEDs. You would then set the LED count to 10 and select 'Duplicate'. Your layout will now have one device representing both of the bars.
If you'd rather split the leds evenly over two devices, then fill in the total amount of LEDs and select 'Two devices'. Your layout will now show two devices representing each of the bars.

If you have a more complex device like the Glide (H6062), you can select 'Custom components'. This way you can make your layout exactly like you have your device on the wall. Make sure for the H6062 that you count 9 LEDs for the each straight bar and 3 LEDs for each corner. If you'd like to use the actual components for this device, they are included in this repository.

## Known Issues
- Getting the information from the device sometimes takes a while, give the bars around 30 seconds to start
- Sometimes the bars don't turn off when you shut down SignalRGB

## Installation
Click the button above and allow signalrgb to install this extension when prompted.

## Support
Feel free to open issues here, or join the SignalRGB Testing Server and post an issue there https://discord.com/invite/J5dwtcNhqC. Don't forget to tag me @rickofficial
