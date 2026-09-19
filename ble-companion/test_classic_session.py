"""Classic session tests use an in-memory strip and synthetic BLE profile only."""
import asyncio
import unittest
from classic_session import ClassicSession


def packet(head, command, payload=b''):
    raw = bytearray([head, command]) + bytearray(payload)
    raw += bytes(19-len(raw))
    checksum = 0
    for value in raw:
        checksum ^= value
    return bytes(raw) + bytes([checksum])


class Profile:
    id = 'synthetic-classic'
    family = 'classic'
    minimum_interval = .1
    capabilities = {'rgb': True, 'power': True, 'brightness': True, 'restore': True}

    @staticmethod
    def color(rgb):
        if len(rgb) != 3 or any(type(value) is not int or not 0 <= value <= 255 for value in rgb):
            raise ValueError('Invalid RGB')
        return packet(0x33, 5, bytes([2, *rgb]))

    @staticmethod
    def power(on):
        return packet(0x33, 1, bytes([int(on)]))

    @staticmethod
    def brightness(percent):
        if type(percent) is not int or not 0 <= percent <= 100:
            raise ValueError('Invalid brightness')
        return packet(0x33, 4, bytes([round(percent*255/100)]))

    @staticmethod
    def decode_snapshot(power, brightness, mode):
        for raw, command in ((power, 1), (brightness, 4), (mode, 5)):
            if len(raw) != 20 or raw[:2] != bytes([0xaa, command]) or packet(0xaa, command, raw[2:19]) != raw:
                raise ValueError('Invalid reply')
        if power[2] not in (0, 1) or mode[2] != 2 or any(mode[6:19]):
            raise ValueError('Unknown state/mode')
        return {'power': power[2], 'brightness_raw': brightness[2],
                'brightness': round(brightness[2]*100/255), 'mode': mode[2:19], 'rgb': tuple(mode[3:6])}

    @staticmethod
    def restore_commands(snapshot):
        return [packet(0x33, 5, snapshot['mode']),
                packet(0x33, 4, bytes([snapshot['brightness_raw']])),
                packet(0x33, 1, bytes([snapshot['power']]))]


class Clock:
    def __init__(self):
        self.now = 0
    def __call__(self):
        return self.now


class Strip:
    def __init__(self):
        self.power = 0
        self.brightness = 137  # Deliberately not an exact percentage roundtrip.
        self.mode = Profile.color((20, 30, 40))[2:19]
        self.original = (self.power, self.brightness, self.mode)
        self.writes = []
        self.queries = []
        self.connections = 0
        self.ignore_writes = False
        self.fail_once = False


class Transport:
    def __init__(self, strip):
        self.strip = strip
        self.connected = False
    async def connect(self):
        self.strip.connections += 1
        self.connected = True
    async def disconnect(self):
        self.connected = False
    async def query(self, command, payload=b''):
        self.strip.queries.append(command)
        if command == 1:
            return packet(0xaa, 1, bytes([self.strip.power]))
        if command == 4:
            return packet(0xaa, 4, bytes([self.strip.brightness]))
        if command == 5:
            return packet(0xaa, 5, self.strip.mode)
        raise AssertionError('Unexpected query')
    async def send(self, raw):
        assert self.connected and raw[0] == 0x33
        assert packet(0x33, raw[1], raw[2:19]) == raw
        assert raw[1] in (1, 4, 5)
        if raw[1] == 5:
            assert raw[2] == 2  # Never realtime05 or H6008 normal0D.
        self.strip.writes.append(raw)
        if not self.strip.ignore_writes:
            if raw[1] == 1:
                self.strip.power = raw[2]
            elif raw[1] == 4:
                self.strip.brightness = raw[2]
            else:
                self.strip.mode = raw[2:19]
        if self.strip.fail_once:
            self.strip.fail_once = False
            raise ConnectionError('synthetic partial write')


DEVICE = {'device': 'synthetic-strip', 'ble_address': '00:00:00:00:00:11'}


class Tests(unittest.IsolatedAsyncioTestCase):
    def make(self, factory=None):
        strip, clock = Strip(), Clock()
        session = ClassicSession(DEVICE, factory or (lambda _: Transport(strip)), Profile(), clock=clock)
        return session, strip, clock

    async def test_snapshot_precedes_power_and_original_raw_state_restores(self):
        session, strip, clock = self.make()
        session.acquire((100, 50, 10), brightness=40)
        await session.step()
        self.assertEqual(strip.queries[:3], [1, 4, 5])
        self.assertEqual([raw[1] for raw in strip.writes], [1, 4, 5])
        self.assertEqual(session.snapshot['brightness_raw'], 137)
        session.request_release()
        await session.step()
        self.assertEqual((strip.power, strip.brightness, strip.mode), strip.original)
        self.assertTrue(session.restored_exact)
        self.assertEqual(session.state, 'idle')

    async def test_unknown_scene_refused_before_any_write(self):
        session, strip, clock = self.make()
        strip.mode = bytes([4]) + bytes(16)
        session.acquire((1, 2, 3))
        await session.step()
        self.assertEqual(session.state, 'failed')
        self.assertEqual(session.failures, 3)
        self.assertFalse(strip.writes)
        self.assertIn('mode', session.error)

    async def test_latest_color_coalesced_at_ten_fps(self):
        session, strip, clock = self.make()
        strip.power = 1
        session.acquire((1, 2, 3))
        await session.step()
        clock.now = .05
        for value in range(100):
            session.acquire((value, 0, 0))
        await session.step()
        self.assertEqual(session.frame_writes, 1)
        clock.now = .11
        await session.step()
        self.assertEqual(session.frame_writes, 2)
        self.assertEqual(strip.writes[-1], Profile.color((99, 0, 0)))

    async def test_external_off_pauses_and_reconnect_does_not_turn_on(self):
        session, strip, clock = self.make()
        session.acquire((1, 2, 3))
        await session.step()
        power_writes = [raw for raw in strip.writes if raw[1] == 1]
        strip.power = 0
        clock.now = 1.6
        session.acquire((4, 5, 6))
        await session.step()
        self.assertEqual(session.state, 'paused_off')
        session.transport.connected = False
        clock.now = 1.7
        session.acquire((7, 8, 9))
        await session.step()
        self.assertEqual(session.state, 'paused_off')
        self.assertEqual([raw for raw in strip.writes if raw[1] == 1], power_writes)

    async def test_manual_on_resumes_unchanged_color_without_power_write(self):
        session, strip, clock = self.make()
        session.acquire((1, 2, 3))
        await session.step()
        strip.power = 0
        clock.now = 1.6
        session.heartbeat()
        await session.step()
        strip.power = 1
        clock.now = 3.2
        session.heartbeat()
        await session.step()
        self.assertEqual(session.frame_writes, 2)
        self.assertEqual(len([raw for raw in strip.writes if raw[1] == 1]), 1)

    async def test_reconnect_keeps_original_snapshot_after_partial_write(self):
        session, strip, clock = self.make()
        strip.fail_once = True
        session.acquire((1, 2, 3))
        await session.step()
        original = dict(session.snapshot)
        self.assertEqual(session.state, 'reconnecting')
        clock.now = .6
        session.heartbeat()
        await session.step()
        self.assertEqual(session.snapshot, original)
        self.assertEqual(len([raw for raw in strip.writes if raw[1] == 1]), 1)
        session.request_release()
        await session.step()
        self.assertEqual((strip.power, strip.brightness, strip.mode), strip.original)

    async def test_connection_attempts_bounded_to_three(self):
        attempts = []
        class Absent(Transport):
            async def connect(self):
                attempts.append(1)
                raise ConnectionError('absent')
        session, strip, clock = self.make()
        session.factory = lambda _: Absent(strip)
        for _ in range(10):
            session.acquire((1, 2, 3))
            await session.step()
            clock.now += 1.6
        self.assertEqual(len(attempts), 3)
        self.assertEqual(session.state, 'failed')
        self.assertFalse(strip.writes)

    async def test_expiry_restores_while_connected_without_late_reconnect(self):
        for connected in (True, False):
            session, strip, clock = self.make()
            session.acquire((1, 2, 3))
            await session.step()
            before = strip.connections
            session.transport.connected = connected
            clock.now = 2.1
            await session.step()
            self.assertEqual(strip.connections, before)
            if connected:
                self.assertTrue(session.restored_exact)
                self.assertEqual((strip.power, strip.brightness, strip.mode), strip.original)
            else:
                self.assertFalse(session.restored_exact)
                self.assertEqual(session.state, 'failed')
                self.assertIsNotNone(session.snapshot)
                self.assertIn('no late restoration', session.restore_error)

    async def test_final_color_preserves_current_power_and_brightness(self):
        session, strip, clock = self.make()
        session.acquire((1, 2, 3), brightness=30)
        await session.step()
        strip.power, strip.brightness = 0, 181
        before = len(strip.writes)
        session.request_release(False, final_rgb=(9, 8, 7))
        await session.step()
        self.assertEqual(strip.writes[before:], [Profile.color((9, 8, 7))])
        self.assertEqual((strip.power, strip.brightness), (0, 181))
        self.assertIsNone(session.restored_exact)
        self.assertEqual(session.state, 'idle')

    async def test_turn_off_does_not_restore_color_or_brightness(self):
        session, strip, clock = self.make()
        session.acquire((1, 2, 3), brightness=30)
        await session.step()
        before = len(strip.writes)
        session.request_release(False, turn_off=True)
        await session.step()
        self.assertEqual(strip.writes[before:], [Profile.power(False)])
        self.assertEqual(strip.mode, Profile.color((1, 2, 3))[2:19])
        self.assertEqual(strip.brightness, round(30*255/100))

    async def test_restore_mismatch_is_failed_not_success(self):
        session, strip, clock = self.make()
        session.acquire((1, 2, 3))
        await session.step()
        strip.ignore_writes = True
        session.request_release()
        await session.step()
        self.assertEqual(session.state, 'failed')
        self.assertFalse(session.restored_exact)
        self.assertIsNotNone(session.snapshot)
        self.assertIn('ORIGINAL', session.restore_error)

    async def test_release_during_blocked_connection_cancels_before_any_color(self):
        session, strip, clock = self.make()
        entered = asyncio.Event()
        class Slow(Transport):
            async def connect(self):
                entered.set()
                await asyncio.Event().wait()
        session.factory = lambda _: Slow(strip)
        session.acquire((1, 2, 3))
        worker = asyncio.create_task(session.run())
        await asyncio.wait_for(entered.wait(), 1)
        session.request_release()
        await asyncio.wait_for(session.released.wait(), 1)
        session.stopping = True
        await asyncio.wait_for(worker, 1)
        self.assertFalse(strip.writes)
        self.assertEqual(session.state, 'idle')

    async def test_lease_expires_during_initial_query_and_cancels_no_late_write(self):
        session, strip, clock = self.make()
        entered = asyncio.Event()
        class Slow(Transport):
            async def query(self, command, payload=b''):
                entered.set()
                await asyncio.Event().wait()
        session.factory = lambda _: Slow(strip)
        session.acquire((1, 2, 3))
        worker = asyncio.create_task(session.run())
        await asyncio.wait_for(entered.wait(), 1)
        clock.now = 2.1
        await asyncio.wait_for(session.released.wait(), 1)
        session.stopping = True
        await asyncio.wait_for(worker, 1)
        self.assertFalse(strip.writes)

    async def test_release_policy_promotion_keeps_original_until_latest_policy(self):
        session, strip, clock = self.make()
        session.acquire((1, 2, 3))
        await session.step()
        entered, proceed = asyncio.Event(), asyncio.Event()
        original_disconnect = session.transport.disconnect
        async def paused_disconnect():
            entered.set()
            await proceed.wait()
            await original_disconnect()
        session.transport.disconnect = paused_disconnect
        session.request_release(False)
        cleanup = asyncio.create_task(session.step())
        await asyncio.wait_for(entered.wait(), 1)
        session.request_release(True)
        proceed.set()
        await cleanup
        self.assertFalse(session.released.is_set())
        self.assertIsNotNone(session.snapshot)
        await session.step()
        self.assertTrue(session.restored_exact)
        self.assertEqual((strip.power, strip.brightness, strip.mode), strip.original)

    async def test_acquisition_during_release_is_rejected_without_overwriting_policy(self):
        session, strip, clock = self.make()
        session.acquire((1, 2, 3))
        await session.step()
        session.request_release(False, turn_off=True)
        with self.assertRaises(RuntimeError):
            session.acquire((9, 9, 9))
        await session.step()
        self.assertEqual(strip.power, 0)
        self.assertEqual(session.state, 'idle')

    async def test_cancelled_inflight_power_write_still_restores_original_snapshot(self):
        session, strip, clock = self.make()
        entered = asyncio.Event()
        class Partial(Transport):
            async def send(self, raw):
                await super().send(raw)
                if not entered.is_set():
                    entered.set()
                    await asyncio.Event().wait()
        session.factory = lambda _: Partial(strip)
        session.acquire((1, 2, 3))
        worker = asyncio.create_task(session.run())
        await asyncio.wait_for(entered.wait(), 1)
        self.assertEqual(strip.power, 1)  # Write reached hardware before cancellation.
        session.request_release()
        await asyncio.wait_for(session.released.wait(), 1)
        session.stopping = True
        await asyncio.wait_for(worker, 1)
        self.assertTrue(session.restored_exact)
        self.assertEqual((strip.power, strip.brightness, strip.mode), strip.original)

    async def test_real_profile_interoperates_and_restores_unrounded_brightness(self):
        from profiles import get_profile
        session, strip, clock = self.make()
        session.profile = get_profile('h6159-classic-v1')
        session.acquire((120, 3, 17), brightness=75)
        await session.step()
        self.assertEqual(strip.mode, session.profile.color((120, 3, 17))[2:19])
        session.request_release()
        await session.step()
        self.assertEqual((strip.power, strip.brightness, strip.mode), strip.original)
        self.assertTrue(session.restored_exact)

    async def test_observed_external_off_survives_lease_expiry_and_restore_release(self):
        for explicit in (False, True):
            session, strip, clock = self.make()
            strip.power = 1
            initial = (strip.power, strip.brightness, strip.mode)
            session.acquire((1, 2, 3), brightness=20)
            await session.step()
            original_snapshot = dict(session.snapshot)
            strip.power = 0
            clock.now = 1.6
            session.heartbeat()
            await session.step()
            self.assertEqual(session.state, 'paused_off')
            self.assertTrue(session.external_power_off)
            count = len(strip.writes)
            if explicit:
                session.request_release(True)
            else:
                clock.now = 3.7
            await session.step()
            self.assertEqual((strip.power, strip.brightness, strip.mode), (0, initial[1], initial[2]))
            self.assertEqual(original_snapshot['power'], 1)
            self.assertFalse(any(raw[1] == 1 and raw[2] == 1 for raw in strip.writes[count:]))
            self.assertEqual(session.state, 'idle')
            self.assertTrue(session.status()['external_power_preserved'])
            self.assertFalse(session.restored_exact)
            self.assertIsNone(session.restore_error)

    async def test_external_off_seen_on_reconnect_survives_release(self):
        session, strip, clock = self.make()
        strip.power = 1
        session.acquire((1, 2, 3))
        await session.step()
        session.transport.connected = False
        strip.power = 0
        clock.now = .5
        await session.step()
        self.assertTrue(session.external_power_off)
        before = len(strip.writes)
        session.request_release(True)
        await session.step()
        self.assertFalse(any(raw[1] == 1 and raw[2] == 1 for raw in strip.writes[before:]))
        self.assertFalse(session.restored_exact)
        self.assertTrue(session.status()['external_power_preserved'])

    async def test_later_observed_manual_on_clears_external_off_override(self):
        session, strip, clock = self.make()
        strip.power = 1
        session.acquire((1, 2, 3))
        await session.step()
        strip.power = 0
        clock.now = 1.6
        session.heartbeat()
        await session.step()
        strip.power = 1
        clock.now = 3.2
        session.heartbeat()
        await session.step()
        session.request_release(True)
        await session.step()
        self.assertEqual(strip.power, 1)
        self.assertTrue(session.restored_exact)
        self.assertFalse(session.status()['external_power_preserved'])

    async def test_optional_power_brightness_none_streams_rgb_without_forced_controls(self):
        from dataclasses import replace
        from profiles import get_profile
        profile = get_profile('h6159-classic-v1')
        limited = replace(profile, capabilities=dict(profile.capabilities, power=False, brightness=False),
                          power=None, brightness=None)
        session, strip, clock = self.make()
        strip.power = 1
        session = ClassicSession(DEVICE, lambda _: Transport(strip), limited, clock=clock)
        session.acquire((70, 20, 10), on=None)
        await session.step()
        self.assertEqual(strip.writes, [limited.color((70, 20, 10))])
        self.assertTrue(session.status()['ready'])
        self.assertEqual(strip.brightness, 137)
        # An unsupported control may change externally; release preserves it.
        strip.brightness = 181
        session.request_release()
        await session.step()
        self.assertTrue(all(raw[1] == 5 for raw in strip.writes))
        self.assertEqual(strip.brightness, 181)
        self.assertEqual(strip.power, 1)
        self.assertEqual(strip.mode, strip.original[2])
        self.assertEqual(session.state, 'idle')
        self.assertFalse(session.restored_exact)
        self.assertIsNone(session.restore_error)

    async def test_optional_power_none_does_not_turn_on_initially_off_strip(self):
        from dataclasses import replace
        from profiles import get_profile
        profile = get_profile('h6159-classic-v1')
        profile = replace(profile, capabilities=dict(profile.capabilities, power=False, brightness=False),
                          power=None, brightness=None)
        session, strip, clock = self.make()
        session = ClassicSession(DEVICE, lambda _: Transport(strip), profile, clock=clock)
        session.acquire((1, 2, 3), on=None)
        await session.step()
        self.assertEqual(session.state, 'paused_off')
        self.assertFalse(strip.writes)

    async def test_unsupported_explicit_controls_rejected_atomically(self):
        from dataclasses import replace
        from profiles import get_profile
        profile = get_profile('h6159-classic-v1')
        profile = replace(profile, capabilities=dict(profile.capabilities, power=False, brightness=False),
                          power=None, brightness=None)
        session, strip, clock = self.make()
        session = ClassicSession(DEVICE, lambda _: Transport(strip), profile, clock=clock)
        for arguments in ({'on': True}, {'on': False}, {'on': None, 'brightness': 10}):
            with self.assertRaises(ValueError):
                session.acquire((1, 2, 3), **arguments)
        with self.assertRaises(ValueError):
            session.request_release(False, turn_off=True)
        self.assertFalse(session.requested)
        self.assertTrue(session.released.is_set())
        self.assertFalse(strip.writes)

    async def test_missing_declared_builder_has_meaningful_error(self):
        from dataclasses import replace
        from profiles import get_profile
        profile = get_profile('h6159-classic-v1')
        for field in ('power', 'brightness', 'color', 'decode_snapshot', 'restore_commands'):
            with self.assertRaisesRegex(ValueError, field):
                ClassicSession(DEVICE, lambda _: None, replace(profile, **{field: None}))

    async def test_final_color_rejects_alternate_flag_but_restore_is_byte_exact(self):
        from profiles import get_profile
        profile = get_profile('h6159-classic-v1')
        for final in (None, (90, 80, 70)):
            strip, clock = Strip(), Clock()
            mode = bytearray(strip.mode)
            mode[4] = 1
            mode[5:8] = bytes.fromhex('d6e1ff')
            strip.mode = bytes(mode)
            strip.original = (strip.power, strip.brightness, strip.mode)
            class CanonicalTransport(Transport):
                async def send(self, raw):
                    await super().send(raw)
                    if raw[1] == 5:
                        mode = bytearray(self.strip.mode)
                        # Simulate a command which failed to leave alternate mode.
                        mode[4] = 1
                        mode[5:8] = bytes.fromhex('d6e1ff')
                        self.strip.mode = bytes(mode)
            session = ClassicSession(DEVICE, lambda _: CanonicalTransport(strip), profile, clock=clock)
            session.acquire((1, 2, 3))
            await session.step()
            session.request_release(final_rgb=final)
            await session.step()
            self.assertEqual(strip.mode[4], 1)
            if final is None:
                self.assertEqual(session.state, 'idle')
                self.assertIsNone(session.restore_error)
                self.assertEqual((strip.power, strip.brightness, strip.mode), strip.original)
                self.assertTrue(session.restored_exact)
            else:
                self.assertEqual(tuple(strip.mode[1:4]), final)
                self.assertEqual(session.state, 'failed')
                self.assertIn('Final state readback differs', session.restore_error)
                self.assertFalse(session.restored_exact)
                self.assertIsNotNone(session.snapshot)


if __name__ == '__main__':
    unittest.main(verbosity=2)
