"""Profile-driven, loopback-only Govee BLE bridge; --serve enables I/O."""
import argparse
import asyncio
from collections import deque
import hmac
import json
from pathlib import Path
import re
import socket
import sys
import time
from protocol import normal, packet, realtime, start_realtime
from transport import BleTransport, Discovery
from profiles import get_profile, profile_for_device

MAX_DEVICES = 16

MAC_RE = re.compile(r'^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$')

def load_config(path):
    path = Path(path).resolve()
    config = json.loads(path.read_text(encoding='utf-8'))
    if config.get('bind', '127.0.0.1') != '127.0.0.1':
        raise ValueError('Only 127.0.0.1 binding is supported')
    if type(config.get('port')) is not int or not 1024 <= config['port'] <= 65535:
        raise ValueError('Invalid local UDP port')
    devices = config['devices']
    if not isinstance(devices, list) or not 1 <= len(devices) <= MAX_DEVICES:
        raise ValueError('Allowlist must contain one to sixteen devices')
    ids = set(); addresses = set()
    for device in devices:
        if not isinstance(device, dict):
            raise ValueError('Invalid device entry')
        profile = profile_for_device(device)
        if not isinstance(device.get('device'), str) or not 1 <= len(device['device']) <= 100:
            raise ValueError('Invalid device identity')
        fields = ('ble_address', 'wifi_mac') if profile.family == 'h6008' else ('ble_address',)
        for field in fields:
            if not isinstance(device.get(field), str):
                raise ValueError('Missing device address required by its profile')
            device[field] = device[field].upper()
            if not MAC_RE.fullmatch(device[field]):
                raise ValueError('Invalid device MAC')
        if 'name' in device and (not isinstance(device['name'], str) or not 1 <= len(device['name']) <= 80):
            raise ValueError('Device name must contain one to eighty characters')
        if device['device'] in ids or device['ble_address'] in addresses:
            raise ValueError('Duplicate allowlisted identity')
        ids.add(device['device']); addresses.add(device['ble_address'])
    needs_key = any(profile_for_device(d).family == 'h6008' for d in devices)
    key = bytes.fromhex(config.get('communication_key_hex', ''))
    if (needs_key and len(key) != 16) or (key and len(key) != 16):
        raise ValueError('Encrypted profiles require a local sixteen-byte communication key')
    interval = config.get('minimum_interval_ms', 100) / 1000
    lease = config.get('lease_ms', 2000) / 1000
    minimum = .05 if config.get('experimental_20fps') is True else .1
    if not minimum <= interval <= 1 or not 1 <= lease <= 10:
        raise ValueError('Interval must be >=100ms (50ms with experimental_20fps:true); lease must be 1..10 seconds')
    if config.get('api_token') is not None and not isinstance(config['api_token'], str):
        raise ValueError('api_token must be a string or null')
    if config.get('python_libs'):
        sys.path.insert(0, str((path.parent / config['python_libs']).resolve()))
    return config, key, interval, lease

class BulbSession:
    """One serial BLE worker; callers only replace the latest desired RGB."""
    def __init__(self, device, factory, interval=.1, lease=2, clock=time.monotonic):
        self.device = device
        self.factory = factory
        self.interval = interval
        self.lease = lease
        self.clock = clock
        self.transport = None
        self.snapshot = None
        self.recovery_pending = None
        self.recovery_baseline = None
        self.last_mode_hex = None
        self.initialized = False
        self.modified = False
        self.requested = False
        self.deadline = 0
        self.desired = None
        self.last_sent = None
        self.last_sent_at = -1e9
        self.last_power_at = -1e9
        self.power = None
        self.state = 'idle'
        self.error = None
        self.restore_error = None
        self.failures = 0
        self.next_retry = 0
        self.restore_on_release = True
        self.restore_explicit = False
        self.final_rgb = None
        self.released = asyncio.Event(); self.released.set()
        self.stopping = False
        self.operation_task = None
        self.new_acquisition = True
        self.release_revision = 0
        self.releasing = False
        self.frame_writes = 0
        self.frame_history = deque(maxlen=256)

    def acquire(self, rgb=None):
        if not self.requested:
            self.new_acquisition = True
            self.failures = 0; self.next_retry = 0; self.error = None
            self.restore_error = None
            if self.state in ('idle','failed'):
                self.state = 'connecting'
        self.requested = True
        self.deadline = self.clock() + self.lease
        self.restore_on_release = True
        self.restore_explicit = False
        self.final_rgb = None
        self.released.clear()
        if rgb is not None:
            self.desired = tuple(rgb)

    def heartbeat(self):
        if self.requested:
            self.deadline = self.clock() + self.lease

    def request_release(self, restore=True, final_rgb=None):
        self.release_revision += 1
        self.requested = False
        self.restore_on_release = restore
        self.restore_explicit = True
        self.final_rgb = tuple(final_rgb) if final_rgb is not None else None
        self.desired = None
        self.released.clear()
        if not self.releasing and self.operation_task is not None and not self.operation_task.done():
            self.operation_task.cancel()

    def active(self):
        return self.requested and self.clock() < self.deadline and not self.stopping

    def status(self):
        ready = (self.active() and self.state in ('ready', 'streaming') and
                 self.transport is not None and self.transport.connected)
        return {'device':self.device['device'], 'state':self.state, 'ready':ready,
                'last_sent_rgb':list(self.last_sent) if self.last_sent is not None else None,
                'error':self.error, 'restore_error':self.restore_error, 'failures':self.failures,
                'power':self.power, 'initial_state':None if self.snapshot is None else {
                    'mode_hex':self.snapshot['mode'].hex(), 'power':self.snapshot['power'],
                    'brightness':self.snapshot['brightness']},
                'last_mode_hex':self.last_mode_hex,
                'recovery_baseline':self.recovery_baseline,
                'recovery_pending':self.recovery_pending is not None,
                'frame_writes':self.frame_writes,
                'last_write_monotonic':None if not self.frame_history else self.frame_history[-1][1],
                'lease_remaining_ms':max(0, round((self.deadline-self.clock())*1000)) if self.requested else 0}

    async def release(self):
        if self.released.is_set() and self.transport is None:
            return
        self.state = 'releasing'
        self.releasing = True
        revision = self.release_revision
        self.requested = False
        restored = self.final_rgb is None and (not self.modified or not self.restore_on_release)
        handoff = self.final_rgb is None and not self.restore_on_release
        final_rgb = self.final_rgb
        baseline = self.snapshot if self.snapshot is not None else self.recovery_pending
        try:
            if final_rgb is not None:
                if self.transport is None or not self.transport.connected:
                    if self.transport is not None:
                        await self.transport.disconnect()
                    self.transport = self.factory(self.device)
                    # Shutdown may arrive during initial connection. Authenticate
                    # a bounded final write without entering realtime or powering on.
                    await asyncio.wait_for(self.transport.connect(),10)
                await self.transport.send(normal(final_rgb))
                restored = True
            elif self.modified and baseline is not None and self.restore_on_release:
                if self.transport is None or not self.transport.connected:
                    if not self.restore_explicit:
                        raise ConnectionError('Lease expired without BLE; no late restoration reconnect')
                    if self.transport is not None:
                        await self.transport.disconnect()
                    self.transport = self.factory(self.device)
                    await asyncio.wait_for(self.transport.connect(),10)
                # Never restore power/brightness: LAN owns those controls.
                await self.transport.send(packet(0x33, 5, baseline['mode'][2:19]))
                restored = True
        except Exception as ex:
            self.restore_error = str(ex)
        finally:
            if self.transport is not None:
                try:
                    await self.transport.disconnect()
                except Exception as ex:
                    self.error = 'Disconnect: ' + str(ex)
            self.transport = None
            self.initialized = False
            self.desired = None
            self.last_sent = None
            if restored and not handoff:
                self.snapshot = None
                self.recovery_pending = None
                self.modified = False
            self.releasing = False
            if revision == self.release_revision:
                self.final_rgb = None
                self.state = 'idle' if restored else 'failed'
                self.released.set()
            else:
                # Shutdown can upgrade a fallback release while disconnect is
                # in progress. Preserve its policy for the next serial step.
                self.state = 'releasing'

    async def step(self):
        if not self.active():
            await self.release()
            return
        if self.failures >= 3 or self.clock() < self.next_retry:
            return
        try:
            if self.transport is None or not self.transport.connected:
                if self.transport is not None:
                    await self.transport.disconnect()
                self.state = 'connecting'
                self.transport = self.factory(self.device)
                await self.transport.connect()  # Includes handshake and exact AA14.
                if not self.active():
                    await self.release(); return
                self.state = 'initializing'
                self.initialized = False
                self.last_sent = None
                self.last_sent_at = -1e9
                power = await self.transport.query(1)
                self.power = power[2]
                if self.power not in (0, 1):
                    raise ValueError('Unrecognized power state')
                self.last_power_at = self.clock()
                mode = await self.transport.query(5, b'\x01')
                self.last_mode_hex = mode.hex()
                # A previous restore:false handoff may retain our old mode05.
                # If LAN set a new static color, refresh that snapshot instead.
                if mode[2] not in (5,13):
                    raise ValueError('Current mode is not a known static/realtime RGB mode')
                if self.snapshot is None and mode[2] == 5:
                    if self.desired is None:
                        raise ValueError('Orphan realtime mode requires an explicitly requested RGB color')
                    recovery_rgb = self.desired
                    brightness = await self.transport.query(4)
                    if not self.active():
                        await self.release(); return
                    # A reboot loses the old static snapshot. Establish only the
                    # explicitly requested RGB as a NEW base; never invent the
                    # color that existed before the reboot. Keep this provisional
                    # base across cancellation between the write and its readback.
                    recovery_command = normal(recovery_rgb)
                    self.recovery_pending = {
                        'mode':packet(0xaa, 5, recovery_command[2:19]),
                        'power':self.power, 'brightness':brightness[2]}
                    self.recovery_baseline = 'requested_rgb_after_orphan_realtime'
                    self.modified = True
                    await self.transport.send(recovery_command)
                    if not self.active():
                        await self.release(); return
                    mode = await self.transport.query(5, b'\x01')
                    self.last_mode_hex = mode.hex()
                    if mode[2] != 13 or tuple(mode[3:6]) != recovery_rgb or mode[6:8] != b'\0\0':
                        raise ValueError('Recovery RGB baseline readback does not match the requested static color')
                    self.snapshot = {'mode':mode, 'power':self.power, 'brightness':brightness[2]}
                    self.recovery_pending = None
                    self.modified = False
                    self.new_acquisition = False
                if self.snapshot is None or (self.new_acquisition and mode[2] == 13):
                    if mode[2] != 13:
                        raise ValueError('Initial mode is not a restorable static RGB color: mode=' +
                                         format(mode[2], '02x') + ', temperature=' +
                                         str(int.from_bytes(mode[6:8], 'big')))
                    # AA05 mode0D includes both RGB and white-temperature state.
                    # Preserve its entire payload for release, including Kelvin;
                    # requiring Kelvin=0 rejects a bulb left at ordinary white.
                    brightness = await self.transport.query(4)
                    self.snapshot = {'mode':mode, 'power':self.power, 'brightness':brightness[2]}
                    self.recovery_pending = None
                    self.modified = False
                self.new_acquisition = False
                self.state = 'ready'
                self.error = None
            # A release can arrive during a slow connection or status query.
            if not self.active():
                await self.release(); return
            if self.clock() - self.last_power_at >= .5:
                self.power = (await self.transport.query(1))[2]
                self.last_power_at = self.clock()
            if not self.active():
                await self.release(); return
            if self.power != 1:
                self.initialized = False
                self.last_sent = None
                self.state = 'paused_off'
                return
            if not self.initialized:
                self.modified = True
                await self.transport.send(start_realtime())
                self.initialized = True
                self.state = 'ready'
            if (self.active() and self.desired is not None and self.desired != self.last_sent and
                    self.clock() - self.last_sent_at >= self.interval):
                rgb = self.desired
                await self.transport.send(realtime(rgb))
                self.last_sent = rgb
                self.last_sent_at = self.clock()
                self.frame_writes += 1
                self.frame_history.append([self.frame_writes,round(self.last_sent_at,6)])
                self.state = 'streaming'
        except Exception as ex:
            self.error = str(ex)
            self.failures += 1
            self.state = 'failed' if self.failures >= 3 else 'reconnecting'
            self.next_retry = self.clock() + (.5, 1.5, 4)[min(self.failures-1, 2)]
            # Keep the ORIGINAL snapshot across reconnects; never snapshot mode05.
            if self.transport is not None:
                try:
                    await self.transport.disconnect()
                except Exception:
                    pass
            self.transport = None
            self.initialized = False

    async def run(self):
        try:
            while not self.stopping:
                self.operation_task = asyncio.create_task(self.step())
                try:
                    # Poll only the local lease while a slow BLE operation runs.
                    while not self.operation_task.done():
                        await asyncio.wait({self.operation_task},timeout=.05)
                        if self.requested and not self.active() and not self.operation_task.done():
                            self.operation_task.cancel()
                    await self.operation_task
                except asyncio.CancelledError:
                    if self.active():
                        raise
                    await self.release()
                finally:
                    self.operation_task = None
                await asyncio.sleep(.02)
        finally:
            await self.release()

class Bridge:
    def __init__(self, devices, factory, interval=.1, lease=2, token=None):
        from classic_session import ClassicSession
        self.device_entries = {d['device']:d for d in devices}
        self.profiles = {d['device']:profile_for_device(d) for d in devices}
        # New models using a known family only need a profile and allowlist entry.
        families = {
            'h6008': lambda d,p: BulbSession(d, factory, max(interval,p.minimum_interval), lease),
            'classic': lambda d,p: ClassicSession(d, factory, p, max(interval,p.minimum_interval), lease),
        }
        self.sessions = {}
        for device in devices:
            profile = self.profiles[device['device']]
            if profile.family not in families:
                raise ValueError('Unsupported BLE protocol family')
            self.sessions[device['device']] = families[profile.family](device, profile)
        self.token = token
        self.tasks = []

    def start(self):
        self.tasks = [asyncio.create_task(s.run()) for s in self.sessions.values()]

    def selected(self, message):
        ids = message.get('devices', list(self.sessions))
        if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_DEVICES or any(not isinstance(i,str) for i in ids) or len(set(ids)) != len(ids):
            raise ValueError('devices must contain distinct allowlisted IDs')
        if any(not isinstance(i,str) or i not in self.sessions for i in ids):
            raise ValueError('Unknown device identity')
        return [self.sessions[i] for i in ids]

    async def handle(self, message):
        if not isinstance(message,dict):
            raise ValueError('Expected a JSON object')
        if self.token is not None and not hmac.compare_digest(str(message.get('token','')), self.token):
            raise ValueError('Invalid local API token')
        op = message.get('op')
        if op == 'catalog':
            catalog = []
            for identity, profile in self.profiles.items():
                if not profile.bluetooth_only:
                    continue  # Existing H6008 controllers continue to own their bulbs.
                entry = self.device_entries[identity]
                catalog.append({'device': identity, 'profile': profile.id,
                                'model': profile.model, 'name': entry.get('name', profile.model),
                                'transport': 'ble', 'leds': 1,
                                'capabilities': dict(profile.capabilities)})
            return {'id':message.get('id'), 'ok':True, 'devices':catalog}
        if op in ('strip_colors', 'strip_release'):
            identity = message.get('device')
            if not isinstance(identity,str) or identity not in self.sessions or not self.profiles[identity].bluetooth_only:
                raise ValueError('This operation requires an allowlisted Bluetooth-only profile')
            session = self.sessions[identity]
            capabilities = self.profiles[identity].capabilities
            selected = [session]
            if op == 'strip_colors':
                rgb = message.get('rgb')
                realtime(rgb if isinstance(rgb,list) else [])
                brightness = message.get('brightness')
                if 'brightness' in message and (capabilities.get('brightness') is not True or
                        type(brightness) is not int or not 0 <= brightness <= 100):
                    raise ValueError('Brightness requires a supported integer in 0..100')
                on = message.get('on')
                if capabilities.get('power') is True:
                    if type(on) is not bool:
                        raise ValueError('Power-capable profiles require explicit boolean on')
                elif 'on' in message:
                    raise ValueError('This profile does not support power control')
                if on is False:
                    session.request_release(restore=False, turn_off=True)
                else:
                    session.acquire(rgb, brightness=brightness, on=on)
            else:
                mode = message.get('mode')
                if mode not in ('restore', 'color', 'off'):
                    raise ValueError('Unknown Bluetooth release policy')
                if mode == 'off' and capabilities.get('power') is not True:
                    raise ValueError('This profile does not support power control')
                if mode == 'restore' and capabilities.get('restore') is not True:
                    raise ValueError('This profile does not support state restoration')
                rgb = message.get('rgb') if mode == 'color' else None
                if mode == 'color':
                    realtime(rgb if isinstance(rgb,list) else [])
                session.request_release(restore=mode == 'restore', final_rgb=rgb, turn_off=mode == 'off')
                await asyncio.wait_for(session.released.wait(),12)
        elif op == 'colors':
            colors = message.get('colors')
            if not isinstance(colors,list) or not 1 <= len(colors) <= 3:
                raise ValueError('colors must contain one to three entries')
            selected = []; checked = []; seen = set()
            for color in colors:
                if not isinstance(color,dict) or not isinstance(color.get('device'),str):
                    raise ValueError('Malformed color entry')
                identity = color['device']
                if identity not in self.sessions or identity in seen or self.profiles[identity].bluetooth_only:
                    raise ValueError('Unknown or duplicate identity')
                if type(color.get('on')) is not bool:
                    raise ValueError('Each color requires explicit on:true/false')
                rgb = color.get('rgb'); realtime(rgb if isinstance(rgb,list) else [])
                seen.add(identity); selected.append(self.sessions[identity]); checked.append(color)
            for session,color in zip(selected,checked):
                if color['on']:
                    session.acquire(color['rgb'])
                else:
                    session.request_release(restore=False)
        elif op in ('acquire','heartbeat','release','status'):
            selected = self.selected(message)
            if op == 'acquire':
                for session in selected:session.acquire()
            elif op == 'heartbeat':
                for session in selected:session.heartbeat()
            elif op == 'release':
                restore = message.get('restore',True)
                if type(restore) is not bool:raise ValueError('restore must be boolean')
                final_rgb = message.get('final_rgb')
                if final_rgb is not None:
                    realtime(final_rgb if isinstance(final_rgb,list) else [])
                for session in selected:session.request_release(restore,final_rgb)
                # ACK follows worker serialization, avoiding a concurrent restore.
                await asyncio.wait_for(asyncio.gather(*(s.released.wait() for s in selected)),12)
        else:
            raise ValueError('Unsupported op')
        states = [s.status() for s in selected]
        if op == 'status' and message.get('metrics') is True:
            for session,state in zip(selected,states):
                state['frame_history'] = list(session.frame_history)
        return {'id':message.get('id'), 'ok':True, 'devices':states}

    async def close(self):
        for session in self.sessions.values():
            session.stopping = True
            if session.transport is not None or not session.released.is_set():
                session.request_release(session.restore_on_release)
        if self.tasks:
            await asyncio.wait_for(asyncio.gather(*self.tasks,return_exceptions=True),15)

def disable_udp_connection_reset(sock):
    """A late reply to a closed UDP client must not poison the next receive.

    Windows reports ICMP PORT_UNREACHABLE as WSAECONNRESET unless disabled.
    Python's socket.ioctl does not expose this Winsock operation on all builds.
    https://learn.microsoft.com/en-us/windows/win32/winsock/winsock-ioctls
    """
    if sys.platform != 'win32':
        return
    import ctypes
    winsock = ctypes.WinDLL('Ws2_32.dll')
    ioctl = winsock.WSAIoctl
    ioctl.argtypes = [ctypes.c_size_t, ctypes.c_uint32, ctypes.c_void_p,
                     ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
                     ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p,
                     ctypes.c_void_p]
    ioctl.restype = ctypes.c_int
    enabled = ctypes.c_uint32(0)
    returned = ctypes.c_uint32()
    if ioctl(sock.fileno(), 0x9800000C, ctypes.byref(enabled),
             ctypes.sizeof(enabled), None, 0, ctypes.byref(returned),
             None, None) != 0:
        code = winsock.WSAGetLastError()
        raise OSError(code, 'Cannot disable UDP connection-reset notifications')


async def create_loopback_udp_endpoint(protocol_factory, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        disable_udp_connection_reset(sock)
        sock.bind(('127.0.0.1', port))
        sock.setblocking(False)
        return await asyncio.get_running_loop().create_datagram_endpoint(
            protocol_factory, sock=sock)
    except BaseException:
        sock.close()
        raise


def log_udp_error(event, error):
    # Do not log packet contents, client addresses, API tokens or BLE keys.
    print(json.dumps({'event': event, 'error_type': type(error).__name__,
                      'winerror': getattr(error, 'winerror', None),
                      'errno': getattr(error, 'errno', None)}), flush=True)


class UdpApi(asyncio.DatagramProtocol):
    def __init__(self, bridge):
        self.bridge = bridge; self.transport = None; self.tasks = set()
        self.failed = asyncio.Event(); self.closed = asyncio.Event()
        self.stopping = False

    def connection_made(self, transport):self.transport = transport

    def error_received(self, error):
        if not self.stopping and not self.failed.is_set():
            log_udp_error('udp-error', error)
            self.failed.set()

    def connection_lost(self, error):
        self.closed.set()
        if not self.stopping:
            self.error_received(error)

    def datagram_received(self, data, address):
        if self.stopping or self.failed.is_set() or address[0] != '127.0.0.1' or len(data) > 4096 or len(self.tasks) >= 32:
            return
        task = asyncio.create_task(self.respond(data,address))
        self.tasks.add(task); task.add_done_callback(self.tasks.discard)

    async def respond(self,data,address):
        identity = None
        try:
            message = json.loads(data)
            if isinstance(message,dict):identity = message.get('id')
            response = await self.bridge.handle(message)
        except Exception as ex:
            response = {'id':identity,'ok':False,'error':str(ex)}
        if self.transport is not None and not self.transport.is_closing():
            self.transport.sendto(json.dumps(response,separators=(',',':')).encode(),address)


async def close_udp_endpoint(transport, api):
    api.stopping = True
    # abort() closes UDP immediately; close() can strand a pending Proactor write.
    # Dropping a queued API ACK does not affect the separate BLE sessions.
    transport.abort()
    if api.tasks:
        _, pending = await asyncio.wait(tuple(api.tasks), timeout=1)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    await asyncio.wait_for(api.closed.wait(), 1)


async def serve(config,key,interval,lease,seconds=None,stop_file=None):
    discovery = Discovery([d['ble_address'] for d in config['devices']],config.get('windows_direct_address') is True)
    from classic_transport import ClassicTransport
    transports = {
        'h6008': lambda d,p: BleTransport(d,key,discovery),
        'classic': lambda d,p: ClassicTransport(d,p,discovery),
    }
    def make_transport(device):
        profile = profile_for_device(device)
        return transports[profile.family](device,profile)
    bridge = Bridge(config['devices'],make_transport,interval,lease,config.get('api_token'))
    transport,api = await create_loopback_udp_endpoint(lambda:UdpApi(bridge), config['port'])
    bridge.start()
    print(json.dumps({'listening':'127.0.0.1:'+str(config['port']),'devices':len(bridge.sessions),'auto_power':False}),flush=True)
    try:
        started=time.monotonic()
        reopen_times = deque()
        while (seconds is None or time.monotonic()-started<seconds) and (stop_file is None or not Path(stop_file).exists()):
            await asyncio.sleep(.1)
            if api.failed.is_set():
                await close_udp_endpoint(transport, api)
                while True:
                    now = time.monotonic()
                    while reopen_times and now-reopen_times[0] >= 30:
                        reopen_times.popleft()
                    if len(reopen_times) >= 3:
                        raise RuntimeError('Local UDP API recovery limit reached')
                    reopen_times.append(now)
                    await asyncio.sleep(.1)
                    try:
                        transport,api = await create_loopback_udp_endpoint(
                            lambda:UdpApi(bridge), config['port'])
                    except OSError as error:
                        log_udp_error('udp-rebind-error', error)
                        continue
                    print(json.dumps({'event':'udp-listener-recovered',
                                      'attempts_in_30_seconds':len(reopen_times)}),flush=True)
                    break
    finally:
        try:
            await close_udp_endpoint(transport, api)
        finally:
            await bridge.close()
            print(json.dumps({'stopped':True,'devices':[s.status() for s in bridge.sessions.values()]}),flush=True)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True)
    parser.add_argument('--serve',action='store_true')
    parser.add_argument('--seconds',type=float,help='Stop cleanly after this many seconds (optional)')
    parser.add_argument('--stop-file',help='Stop cleanly when this local file exists (optional)')
    args = parser.parse_args()
    config,key,interval,lease = load_config(args.config)
    if args.seconds is not None and not 1<=args.seconds<=86400:
        parser.error('--seconds must be between 1 and 86400')
    if args.serve:
        try:asyncio.run(serve(config,key,interval,lease,args.seconds,args.stop_file))
        except KeyboardInterrupt:pass
    else:
        print(json.dumps({'configuration_valid':True,'configured_devices':len(config['devices']),'listening':False}))

if __name__ == '__main__':main()
