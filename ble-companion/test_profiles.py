"""Offline profile and classic transport tests. No Bluetooth or network I/O."""
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from profiles import SERVICE, NOTIFY, WRITE, get_profile, list_profiles, profile_for_device, packet, valid
from classic_transport import ClassicTransport


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.profile = get_profile('h6159-classic-v1')

    def test_legacy_primary_rgb_fixture(self):
        self.assertEqual(self.profile.color([255, 0, 0]).hex(), '330502ff000000000000000000000000000000cb')
        self.assertEqual(self.profile.color([0, 255, 0]).hex(), '33050200ff0000000000000000000000000000cb')
        self.assertEqual(self.profile.color([0, 0, 255]).hex(), '3305020000ff00000000000000000000000000cb')

    def test_power_and_brightness_domain(self):
        self.assertEqual(self.profile.power(True).hex(), '3301010000000000000000000000000000000033')
        self.assertEqual(self.profile.power(False).hex(), '3301000000000000000000000000000000000032')
        for percent, raw in [(0, 0), (1, 3), (50, 128), (100, 255)]:
            value = self.profile.brightness(percent)
            self.assertEqual(value[:3], bytes([0x33, 4, raw]))
            self.assertTrue(valid(value))

    def test_input_validation(self):
        for rgb in [None, [], [1, 2], [True, 0, 0], [-1, 0, 0], [256, 0, 0], [1., 2, 3]]:
            with self.assertRaises(ValueError): self.profile.color(rgb)
        for value in [-1, 101, True, 1.1, '50']:
            with self.assertRaises(ValueError): self.profile.brightness(value)
        with self.assertRaises(ValueError): self.profile.power(1)

    def test_snapshot_and_exact_raw_restoration(self):
        mode = bytes([2, 12, 34, 56]) + bytes(13)
        for raw in range(256):
            snapshot = self.profile.decode_snapshot(packet(0xaa, 1, b'\x00'), packet(0xaa, 4, bytes([raw])), packet(0xaa, 5, mode))
            commands = self.profile.restore_commands(snapshot)
            self.assertEqual(snapshot['rgb'], (12, 34, 56))
            self.assertEqual(commands, [packet(0x33, 5, mode), packet(0x33, 4, bytes([raw])), self.profile.power(False)])

    def test_unknown_scene_and_rgb_extensions_refused(self):
        for mode in [bytes([4, 2]), bytes([13, 1, 2, 3]), bytes([2, 255, 255, 255, 2]), bytes([2, 255, 255, 255, 1, 10, 20, 30, 1])]:
            with self.assertRaises(ValueError):
                self.profile.decode_snapshot(packet(0xaa, 1, b'\x01'), packet(0xaa, 4, b'\x80'), packet(0xaa, 5, mode))

    def test_h6159_10702_observed_snapshot_preserves_mode_flag(self):
        snapshot = self.profile.decode_snapshot(
            bytes.fromhex('aa010100000000000000000000000000000000aa'),
            bytes.fromhex('aa04ff0000000000000000000000000000000051'),
            bytes.fromhex('aa0502ffffff0100000000000000000000000053'))
        self.assertEqual(snapshot['rgb'], (255, 255, 255))
        self.assertEqual(snapshot['mode'][4], 1)
        self.assertEqual(self.profile.restore_commands(snapshot)[0][2:19], snapshot['mode'])
        self.assertFalse(self.profile.matches_color_mode(snapshot['mode'], [255, 255, 255]))
        self.assertTrue(self.profile.matches_color_mode(self.profile.color([255, 255, 255])[2:19], [255, 255, 255]))
        self.assertFalse(self.profile.matches_color_mode(snapshot['mode'], [255, 255, 254]))

    def test_observed_secondary_rgb_is_restored_without_reinterpretation(self):
        snapshot = self.profile.decode_snapshot(
            packet(0xaa, 1, b'\x01'), packet(0xaa, 4, b'\xff'),
            bytes.fromhex('aa0502ffffff01d6e1ff0000000000000000009b'))
        self.assertEqual(snapshot['mode'][4:8], bytes.fromhex('01d6e1ff'))
        self.assertEqual(self.profile.restore_commands(snapshot)[0][2:19], snapshot['mode'])
        self.assertFalse(self.profile.matches_color_mode(snapshot['mode'], [255, 255, 255]))
        # The known layout also permits a stored second triplet with flag 00;
        # restoration must not delete those fields simply because it is RGB.
        mode = snapshot['mode'][:4] + b'\x00' + snapshot['mode'][5:]
        restored = self.profile.decode_snapshot(packet(0xaa, 1, b'\x01'), packet(0xaa, 4, b'\xff'), packet(0xaa, 5, mode))
        self.assertEqual(self.profile.restore_commands(restored)[0][2:19], mode)

    def test_corrupt_and_mismatched_snapshots_refused(self):
        good = packet(0xaa, 1, b'\x01')
        for power in [good[:-1]+bytes([good[-1]^1]), packet(0xaa, 4, b'\x01'), packet(0xaa, 1, b'\x03')]:
            with self.assertRaises(ValueError):
                self.profile.decode_snapshot(power, packet(0xaa, 4, b'\x80'), packet(0xaa, 5, b'\x02\x01\x02\x03'))

    def test_registry_metadata_and_backward_default(self):
        old = profile_for_device({})
        self.assertEqual(old.family, 'h6008')
        self.assertEqual(old.authentication, 'e7-aes-session')
        self.assertIsNone(old.color)
        self.assertFalse(old.capabilities['addressable'])
        self.assertIs(profile_for_device({'profile': self.profile.id}), self.profile)
        self.assertEqual(json.loads(json.dumps(self.profile.capabilities))['power'], True)
        with self.assertRaises(ValueError): get_profile('unverified-model')

    def test_profile_catalog_is_stable_and_returns_profile_objects(self):
        catalog = list_profiles()
        self.assertIsInstance(catalog, tuple)
        self.assertEqual([p.id for p in catalog], sorted(p.id for p in catalog))
        self.assertTrue(all(get_profile(p.id) is p for p in catalog))
        self.assertEqual({p.id for p in catalog}, {'h6008-realtime-v1', 'h6159-classic-v1'})


class FakeDiscovery:
    def __init__(self):
        self.allowlisted_addresses = {'00:00:00:00:00:01'}
        self.found = SimpleNamespace(address='00:00:00:00:00:01', name='ihoment_H6159_TEST')
    async def find(self, _): return self.found


class FakeClient:
    instances = []
    write_properties = ['write-without-response']
    service_present = True
    queries_to_drop = 0
    def __init__(self, found, **kwargs):
        self.found = found
        self.is_connected = False
        self.writes = []
        self.callback = None
        self.disconnects = 0
        service = SimpleNamespace(characteristics=[SimpleNamespace(uuid=WRITE, properties=self.write_properties), SimpleNamespace(uuid=NOTIFY, properties=['notify'])])
        self.services = SimpleNamespace(get_service=lambda uuid: service if uuid == SERVICE and self.service_present else None)
        self.instances.append(self)
    async def connect(self): self.is_connected = True
    async def disconnect(self): self.is_connected = False; self.disconnects += 1
    async def start_notify(self, uuid, callback): self.callback = callback
    async def stop_notify(self, uuid): self.callback = None
    async def write_gatt_char(self, uuid, data, response):
        self.writes.append((uuid, data, response))
        if data[0] == 0xaa:
            if self.queries_to_drop:
                self.queries_to_drop -= 1
                return
            self.callback(None, packet(0xaa, 1 if data[1] != 1 else 4, b'\x00'))
            self.callback(None, b'bad reply')
            self.callback(None, packet(0xaa, data[1], b'\x01'))


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FakeClient.instances = []; FakeClient.write_properties = ['write-without-response']; FakeClient.service_present = True; FakeClient.queries_to_drop = 0
        self.discovery = FakeDiscovery()
        self.profile = get_profile('h6159-classic-v1')
        self.device = {'ble_address': '00:00:00:00:00:01'}
        self.fake = patch.dict('sys.modules', {'bleak': SimpleNamespace(BleakClient=FakeClient)})
        self.fake.start(); self.addCleanup(self.fake.stop)
    def transport(self): return ClassicTransport(self.device, self.profile, self.discovery)

    async def test_connect_never_writes_handshake_or_mode(self):
        t = self.transport(); await t.connect(); await t.connect()
        self.assertTrue(t.connected); self.assertEqual(len(FakeClient.instances), 1)
        self.assertEqual(t.client.writes, [])
        client = t.client; await t.disconnect()
        self.assertFalse(t.connected); self.assertEqual(client.disconnects, 1)

    async def test_query_ignores_stale_wrong_command_and_malformed_replies(self):
        t = self.transport(); await t.connect()
        t.notification(None, packet(0xaa, 4, b'\xff'))
        reply = await t.query(4)
        self.assertEqual(reply, packet(0xaa, 4, b'\x01'))
        self.assertEqual(t.client.writes[0][1], packet(0xaa, 4))
        self.assertTrue(t.client.writes[0][2])
        await t.disconnect()

    async def test_first_query_timeout_is_retried_once(self):
        FakeClient.queries_to_drop = 1
        t = self.transport(); t.query_timeout = .005; await t.connect()
        self.assertEqual(await t.query(1), packet(0xaa, 1, b'\x01'))
        self.assertEqual(len(t.client.writes), 2)
        self.assertTrue(all(data == packet(0xaa, 1) and response for _, data, response in t.client.writes))
        await t.disconnect()

    async def test_persistent_query_timeout_is_bounded(self):
        FakeClient.queries_to_drop = 10
        t = self.transport(); t.query_timeout = .005; await t.connect()
        with self.assertRaisesRegex(TimeoutError, 'two attempts'):
            await t.query(1)
        self.assertEqual(len(t.client.writes), 2)
        await t.disconnect()

    async def test_restoration_mode_flag_is_sent_exactly(self):
        t = self.transport(); await t.connect()
        observed_mode = bytes.fromhex('02ffffff01d6e1ff000000000000000000')
        restore = packet(0x33, 5, observed_mode)
        await t.send(restore)
        self.assertEqual(t.client.writes[-1], (WRITE, restore, True))
        await t.disconnect()

    async def test_allowlist_address_name_and_service_checks(self):
        self.discovery.allowlisted_addresses.clear()
        with self.assertRaises(ValueError): await self.transport().connect()
        self.discovery.allowlisted_addresses.add(self.device['ble_address'])
        self.discovery.found.address = '00:00:00:00:00:02'
        with self.assertRaises(ValueError): await self.transport().connect()
        self.discovery.found.address = self.device['ble_address']; self.discovery.found.name = 'H6008_TEST'
        with self.assertRaises(ValueError): await self.transport().connect()
        self.discovery.found.name = 'ihoment_H6159_TEST'; FakeClient.service_present = False
        with self.assertRaises(ValueError): await self.transport().connect()
        self.assertFalse(FakeClient.instances[-1].is_connected)

    async def test_configured_exact_name_and_write_with_response(self):
        self.discovery.found.name = 'Shelf strip'; self.device['advertised_name'] = 'Shelf strip'
        FakeClient.write_properties = ['write']
        t = self.transport(); await t.connect(); await t.send(self.profile.color([1, 2, 3]))
        self.assertTrue(t.client.writes[-1][2]); await t.disconnect()

    async def test_no_invented_authentication_realtime_or_identity_probe(self):
        t = self.transport(); await t.connect()
        for invalid in [packet(0xe7, 1), packet(0x33, 5, b'\x05\x01'), packet(0x33, 5, b'\x0d\x01\x02\x03'), packet(0xaa, 0x14)]:
            with self.assertRaises(ValueError): await t.send(invalid)
        with self.assertRaises(ValueError): await t.query(0x14)
        self.assertEqual(t.client.writes, []); await t.disconnect()


if __name__ == '__main__': unittest.main()
