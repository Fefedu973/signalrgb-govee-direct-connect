"""Serial, reversible plain-BLE RGB session. No radio I/O before acquire/run.

Profiles own all packet layouts and snapshot validation. This worker never sends
H6008 authentication or realtime-mode commands.
"""
import asyncio
from collections import deque
import time


class ClassicSession:
    def __init__(self, device, factory, profile, interval=.1, lease=2, clock=time.monotonic):
        if profile.family != 'classic':
            raise ValueError('ClassicSession requires a classic BLE profile')
        if interval < max(.1, profile.minimum_interval):
            raise ValueError('Classic BLE cadence cannot exceed the profile limit')
        for name in ('color', 'decode_snapshot', 'restore_commands'):
            if not callable(getattr(profile, name, None)):
                raise ValueError('Classic profile is missing required builder/decoder: ' + name)
        for name in ('power', 'brightness'):
            if profile.capabilities.get(name) is True and not callable(getattr(profile, name, None)):
                raise ValueError('Classic profile declares ' + name + ' but its builder is missing')
        self.device, self.factory, self.profile = device, factory, profile
        self.interval, self.lease, self.clock = interval, lease, clock
        self.transport = None
        self.snapshot = None
        self.modified = False
        self.initialized = False
        self.requested = False
        self.deadline = 0
        self.desired = None
        self.desired_brightness = None
        self.desired_on = True
        self.initial_power_consumed = False
        self.power_seen_on = False
        self.external_power_off = False
        self.external_power_preserved = False
        self.blackout_owned = False
        self.last_sent = None
        self.last_brightness_sent = None
        self.last_sent_at = -1e9
        self.last_power_at = -1e9
        self.power = None
        self.state = 'idle'
        self.error = None
        self.restore_error = None
        self.restored_exact = None
        self.failures = 0
        self.next_retry = 0
        self.restore_on_release = True
        self.restore_explicit = False
        self.final_rgb = None
        self.turn_off = False
        self.release_revision = 0
        self.releasing = False
        self.released = asyncio.Event()
        self.released.set()
        self.stopping = False
        self.operation_task = None
        self.frame_writes = 0
        self.frame_history = deque(maxlen=256)

    def acquire(self, rgb=None, brightness=None, on=True):
        supports_power = self.profile.capabilities.get('power') is True
        if supports_power and type(on) is not bool:
            raise ValueError('on must be boolean for a power-capable profile')
        if not supports_power and on is not None:
            raise ValueError('Profile does not support power control; on must be None')
        # Builders validate without performing I/O; do not partially update state.
        if rgb is not None:
            self.profile.color(rgb)
        if brightness is not None:
            if self.profile.capabilities.get('brightness') is not True or not callable(self.profile.brightness):
                raise ValueError('Profile does not support brightness control')
            self.profile.brightness(brightness)
        if self.releasing or self.state == 'releasing':
            raise RuntimeError('Release in progress; retry acquisition after its ACK')
        if not self.requested:
            self.failures = 0
            self.next_retry = 0
            self.error = None
            self.restore_error = None
            self.initial_power_consumed = False
            if self.snapshot is None:
                self.power_seen_on = False
                self.external_power_off = False
            self.external_power_preserved = False
            self.state = 'connecting'
        self.requested = True
        self.deadline = self.clock() + self.lease
        self.restore_on_release = True
        self.restore_explicit = False
        self.final_rgb = None
        self.turn_off = False
        self.desired_on = on
        if rgb is not None:
            self.desired = tuple(rgb)
        if brightness is not None:
            self.desired_brightness = brightness
        self.released.clear()

    def heartbeat(self):
        if self.requested:
            self.deadline = self.clock() + self.lease

    def request_release(self, restore=True, final_rgb=None, turn_off=False):
        if type(restore) is not bool or type(turn_off) is not bool:
            raise ValueError('Release policy must be boolean')
        if final_rgb is not None:
            self.profile.color(final_rgb)
        if final_rgb is not None and turn_off:
            raise ValueError('final_rgb and turn_off are mutually exclusive')
        if turn_off and (self.profile.capabilities.get('power') is not True or not callable(self.profile.power)):
            raise ValueError('Profile does not support power control')
        self.release_revision += 1
        self.requested = False
        self.deadline = 0
        self.restore_on_release = restore
        self.restore_explicit = True
        self.final_rgb = tuple(final_rgb) if final_rgb is not None else None
        self.turn_off = turn_off
        self.desired = None
        self.desired_brightness = None
        self.state = 'releasing'
        self.released.clear()
        if not self.releasing and self.operation_task is not None and not self.operation_task.done():
            self.operation_task.cancel()

    def active(self):
        return self.requested and not self.stopping and self.clock() < self.deadline

    def status(self):
        ready = bool(self.active() and self.state in ('ready', 'streaming', 'blackout') and
                     self.transport is not None and self.transport.connected)
        return {
            'device': self.device['device'], 'profile': self.profile.id,
            'state': self.state, 'ready': ready, 'power': self.power,
            'last_sent_rgb': list(self.last_sent) if self.last_sent is not None else None,
            'last_sent_brightness': self.last_brightness_sent,
            'error': self.error, 'restore_error': self.restore_error,
            'restored_exact': self.restored_exact, 'failures': self.failures,
            'external_power_preserved': self.external_power_preserved,
            'blackout_owned': self.blackout_owned,
            'initial_state': None if self.snapshot is None else {
                'mode_hex': self.snapshot['mode'].hex(),
                'power': self.snapshot['power'],
                'brightness': self.snapshot['brightness'],
                'brightness_raw': self.snapshot['brightness_raw']},
            'frame_writes': self.frame_writes,
            'last_write_monotonic': self.frame_history[-1][1] if self.frame_history else None,
            'lease_remaining_ms': max(0, round((self.deadline-self.clock())*1000)) if self.requested else 0}

    async def _snapshot(self):
        power = await self.transport.query(1)
        brightness = await self.transport.query(4)
        mode = await self.transport.query(5)
        return self.profile.decode_snapshot(power, brightness, mode)

    @staticmethod
    def _same(left, right):
        return all(left[key] == right[key] for key in ('power', 'brightness_raw', 'mode'))

    def _observe_power(self, value):
        # The first OFF snapshot precedes our authorized initial power decision.
        # Only a subsequent OFF after a known ON is an external power override.
        if self.initial_power_consumed:
            if value == 0 and self.power_seen_on and not self.blackout_owned:
                self.external_power_off = True
            elif value == 1:
                self.power_seen_on = True
                self.external_power_off = False
        self.power = value

    def _power_black_policy(self):
        return (getattr(self.profile, 'power_off_on_black', False) and
                self.profile.capabilities.get('power') is True)

    def _effective_rgb(self):
        if getattr(self.profile, 'black_at_zero_brightness', False) and self.desired_brightness == 0:
            return (0, 0, 0)
        return self.desired

    async def _read_power(self):
        reply = await self.transport.query(1)
        if len(reply) != 20 or reply[:2] != b'\xaa\x01' or reply[2] not in (0, 1):
            raise ValueError('Invalid classic power confirmation')
        self._observe_power(reply[2])
        self.last_power_at = self.clock()

    async def _set_power_confirmed(self, on):
        await self._write(self.profile.power(on))
        await self._read_power()
        if self.power != int(on):
            raise RuntimeError('Power change was not confirmed')

    async def _send_rgb(self, rgb):
        await self._write(self.profile.color(rgb))
        self.last_sent = rgb
        self.last_sent_at = self.clock()
        self.frame_writes += 1
        self.frame_history.append([self.frame_writes, round(self.last_sent_at, 6)])

    async def _disconnect(self):
        if self.transport is not None:
            try:
                await asyncio.wait_for(self.transport.disconnect(), 2)
            finally:
                self.transport = None
        self.initialized = False

    async def _connect(self):
        if self.transport is not None and not self.transport.connected:
            await self._disconnect()
        if self.transport is None:
            self.transport = self.factory(self.device)
        if not self.transport.connected:
            await asyncio.wait_for(self.transport.connect(), 10)

    async def _write(self, command):
        if command[1] == 1 and self.profile.capabilities.get('power') is not True:
            raise ValueError('Profile does not support power writes')
        if command[1] == 4 and self.profile.capabilities.get('brightness') is not True:
            raise ValueError('Profile does not support brightness writes')
        # Mark before await: a cancelled/failed write may have reached hardware.
        self.modified = True
        await self.transport.send(command)

    async def _cleanup(self, revision):
        needs_write = self.turn_off or self.final_rgb is not None or (
            self.modified and self.snapshot is not None and self.restore_on_release)
        if not needs_write:
            return True
        if self.transport is None or not self.transport.connected:
            if not self.restore_explicit:
                raise ConnectionError('Lease expired without BLE; no late restoration reconnect')
            await self._connect()
        if revision != self.release_revision:
            return False

        if self.turn_off or self.final_rgb is not None:
            # Preserve the CURRENT power and brightness for final-color handoff.
            before = await self._snapshot()
            if revision != self.release_revision:
                return False
            final_rgb = self.final_rgb
            if final_rgb is not None and getattr(self.profile, 'black_at_zero_brightness', False) and before['brightness_raw'] == 0:
                final_rgb = (0, 0, 0)
            final_blackout = self._power_black_policy() and final_rgb == (0, 0, 0)
            if self.turn_off or final_blackout:
                command = self.profile.power(False)
                target = dict(before, power=0)
            else:
                command = self.profile.color(final_rgb)
                target = dict(before, mode=bytes(command[2:19]), rgb=final_rgb)
            await self._write(command)
            if revision != self.release_revision:
                return False
            after = await self._snapshot()
            if revision != self.release_revision:
                return False
            matcher = getattr(self.profile, 'matches_color_mode', None)
            if self.final_rgb is not None and not final_blackout and callable(matcher):
                matches = (after['power'] == target['power'] and
                           after['brightness_raw'] == target['brightness_raw'] and
                           matcher(after['mode'], target['rgb']))
            else:
                matches = self._same(after, target)
            if not matches:
                raise RuntimeError('Final state readback differs from the requested handoff')
            self.power = after['power']
            self.restored_exact = None  # A deliberate handoff is not restoration.
            return True

        target = dict(self.snapshot)
        # Optional controls are read-only: do not overwrite external values or
        # demand that they match a snapshot which this session cannot restore.
        supports_power = self.profile.capabilities.get('power') is True
        supports_brightness = self.profile.capabilities.get('brightness') is True
        if not supports_power or not supports_brightness:
            current = await self._snapshot()
            self._observe_power(current['power'])
            if not supports_power:
                target['power'] = current['power']
            if not supports_brightness:
                target['brightness_raw'] = current['brightness_raw']
                target['brightness'] = current['brightness']
        preserve_external_off = self.external_power_off
        self.external_power_preserved = False
        if self.external_power_off:
            # Preserve the immutable ORIGINAL snapshot for evidence, but a later
            # observed external OFF takes priority over restoring its power bit.
            target['power'] = 0
        for command in self.profile.restore_commands(target):
            if revision != self.release_revision:
                return False
            if (command[1] == 1 and not supports_power) or (command[1] == 4 and not supports_brightness):
                continue
            await self._write(command)
        if revision != self.release_revision:
            return False
        after = await self._snapshot()
        self.restored_exact = self._same(after, self.snapshot)
        if not self._same(after, target):
            raise RuntimeError('Restoration readback differs from the ORIGINAL snapshot or preserved external OFF')
        self.external_power_preserved = preserve_external_off
        self.power = after['power']
        return True

    async def release(self):
        if self.released.is_set() and self.transport is None:
            return
        self.state = 'releasing'
        self.releasing = True
        self.requested = False
        revision = self.release_revision
        completed = False
        self.restore_error = None
        try:
            # Leaves room for bounded disconnect before the API's release ACK.
            completed = await asyncio.wait_for(self._cleanup(revision), 9)
        except Exception as ex:
            self.restore_error = str(ex) or type(ex).__name__
            self.restored_exact = False if self.restore_on_release and self.modified else None
        finally:
            try:
                await self._disconnect()
            except Exception as ex:
                self.error = 'Disconnect: ' + str(ex)
                completed = False
            self.last_sent = None
            self.last_brightness_sent = None
            self.releasing = False
            if revision == self.release_revision:
                self.desired = None
                self.desired_brightness = None
                self.final_rgb = None
                self.turn_off = False
                if completed:
                    self.snapshot = None
                    self.modified = False
                    self.blackout_owned = False
                    self.state = 'idle'
                    self.error = None
                else:
                    self.state = 'failed'
                self.released.set()
            else:
                # A shutdown policy can supersede cleanup while one write is in
                # flight. Keep original snapshot; the next serial step applies it.
                self.state = 'releasing'

    async def step(self):
        if not self.active():
            await self.release()
            return
        if self.failures >= 3 or self.clock() < self.next_retry:
            return
        try:
            if self.transport is None or not self.transport.connected:
                self.state = 'connecting'
                await self._connect()
                if not self.active():
                    await self.release()
                    return
                self.state = 'initializing'
                current = await self._snapshot()
                if not self.active():
                    await self.release()
                    return
                if self.snapshot is None:
                    self.snapshot = current
                    self.modified = False
                    self.restored_exact = None
                self._observe_power(current['power'])
                self.last_power_at = self.clock()
                self.last_sent = None
                self.last_brightness_sent = None
                self.initialized = True
                self.error = None

            # Exactly one initial explicit power decision per acquisition, even
            # if a write fails or BLE reconnects later. Streaming does not rearm it.
            if not self.initial_power_consumed:
                self.initial_power_consumed = True
                target_power = self.power if self.desired_on is None else int(self.desired_on)
                initial_black = self._power_black_policy() and self._effective_rgb() == (0, 0, 0) and self.desired_on is True
                if initial_black and self.power == 0 and not self.external_power_off:
                    # Defer the authorized initial ON instead of flashing old RGB.
                    self.blackout_owned = True
                if self.desired_on is not None and self.power != target_power and not initial_black and not self.blackout_owned:
                    if not self.active():
                        await self.release()
                        return
                    await self._write(self.profile.power(self.desired_on))
                    if not self.active():
                        await self.release()
                        return
                    current = await self._snapshot()
                    self.power = current['power']
                    self.last_power_at = self.clock()
                    if self.power != target_power:
                        raise RuntimeError('Initial power command was not confirmed')
                self.power_seen_on = self.power == 1

            if self.clock() - self.last_power_at >= 1.5:
                # Full strict decoder avoids interpreting unknown/corrupt replies.
                current = await self._snapshot()
                self._observe_power(current['power'])
                self.last_power_at = self.clock()
            if not self.active():
                await self.release()
                return
            rgb = self._effective_rgb()
            wants_black = self._power_black_policy() and rgb == (0, 0, 0) and self.desired_on is True
            if wants_black:
                if not self.blackout_owned:
                    # Read immediately before claiming OFF; a prior manual OFF
                    # must never become permission to turn the strip back on.
                    await self._read_power()
                    if not self.active():
                        await self.release()
                        return
                    if self.power == 0 or self.external_power_off:
                        self.state = 'paused_off'
                        return
                    self.blackout_owned = True
                if self.power != 0:
                    await self._set_power_confirmed(False)
                self.state = 'blackout'
                return
            if self.blackout_owned and self.desired_on is True:
                if self.clock() - self.last_sent_at < self.interval:
                    self.state = 'blackout'
                    return
                # Preload the newest brightness/RGB while OFF before returning
                # power. This prevents an old-white flash on supported hardware.
                if self.desired_brightness is not None:
                    await self._write(self.profile.brightness(self.desired_brightness))
                    self.last_brightness_sent = self.desired_brightness
                if not self.active():
                    await self.release()
                    return
                if rgb is not None:
                    await self._send_rgb(rgb)
                if not self.active():
                    await self.release()
                    return
                await self._set_power_confirmed(True)
                self.blackout_owned = False
                self.state = 'streaming' if rgb is not None else 'ready'
                return
            if self.power != 1 or self.desired_on is False:
                self.last_sent = None
                self.last_brightness_sent = None
                self.state = 'paused_off'
                return
            self.state = 'ready' if self.last_sent is None else 'streaming'
            if self.clock() - self.last_sent_at < self.interval:
                return
            if self.desired_brightness is not None and self.desired_brightness != self.last_brightness_sent:
                target_brightness = self.desired_brightness
                await self._write(self.profile.brightness(target_brightness))
                self.last_brightness_sent = target_brightness
                self.last_sent_at = self.clock()
                if not self.active():
                    await self.release()
                    return
            rgb = self._effective_rgb()
            if rgb is not None and rgb != self.last_sent:
                await self._send_rgb(rgb)
                self.state = 'streaming'
        except Exception as ex:
            self.error = str(ex) or type(ex).__name__
            self.failures = 3 if isinstance(ex, (ValueError, PermissionError)) else self.failures + 1
            self.next_retry = self.clock() + (.5, 1.5, 4)[min(self.failures-1, 2)]
            self.state = 'failed' if self.failures >= 3 else 'reconnecting'
            try:
                await self._disconnect()
            except Exception:
                pass

    async def run(self):
        try:
            while not self.stopping:
                self.operation_task = asyncio.create_task(self.step())
                try:
                    while not self.operation_task.done():
                        await asyncio.wait({self.operation_task}, timeout=.05)
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
            if self.operation_task is not None and not self.operation_task.done():
                self.operation_task.cancel()
                await asyncio.gather(self.operation_task, return_exceptions=True)
            await self.release()
