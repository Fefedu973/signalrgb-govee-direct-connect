"""Offline E701 retry tests with a fake radio and real synthetic-key encryption."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from protocol import SERVICE, NOTIFY, WRITE, crypt, packet, realtime, valid
from transport import BleTransport


OEM_KEY = bytes(range(16))  # Synthetic fixture, never a real device credential.
SESSION_KEY = bytes(range(16, 32))
WIFI_MAC = bytes.fromhex('020000000002')
DEVICE = {
    'device': 'synthetic-auth-bulb',
    'ble_address': '02:00:00:00:00:01',
    'wifi_mac': '02:00:00:00:00:02',
}
REAL_WAIT_FOR = asyncio.wait_for


class FakeDiscovery:
    def __init__(self):
        self.calls = []
        self.found = SimpleNamespace(address=DEVICE['ble_address'])

    async def find(self, address):
        self.calls.append(address)
        return self.found


class FakeEncryptedRadio:
    def __init__(self, *, lost_key_replies=1, identity=WIFI_MAC, gated=False):
        self.lost_key_replies = lost_key_replies
        self.identity = identity
        self.gated = gated
        self.clients = []
        self.e702_requested = asyncio.Event()
        self.identity_requested = asyncio.Event()

    def make_client(self, found, **kwargs):
        client = FakeClient(self, found)
        self.clients.append(client)
        return client


class FakeClient:
    def __init__(self, radio, found):
        self.radio, self.found = radio, found
        self.is_connected = False
        self.callback = None
        self.connect_count = self.disconnect_count = self.stop_notify_count = 0
        self.writes = []
        self.e702_acknowledged = False
        service = SimpleNamespace(characteristics=[
            SimpleNamespace(uuid=NOTIFY, properties=['notify']),
            SimpleNamespace(uuid=WRITE, properties=['write-without-response'])])
        self.services = SimpleNamespace(get_service=lambda uuid: service if uuid == SERVICE else None)

    async def connect(self):
        self.connect_count += 1
        self.is_connected = True

    async def start_notify(self, uuid, callback):
        assert uuid == NOTIFY
        self.callback = callback

    async def stop_notify(self, uuid):
        assert uuid == NOTIFY
        self.stop_notify_count += 1
        self.callback = None

    async def disconnect(self):
        self.disconnect_count += 1
        self.is_connected = False

    def reply_e702(self):
        self.e702_acknowledged = True
        self.callback(None, crypt(packet(0xe7, 2), OEM_KEY))

    def reply_identity(self):
        self.callback(None, crypt(packet(0xaa, 0x14, self.radio.identity), SESSION_KEY))

    async def write_gatt_char(self, uuid, encrypted, response):
        assert uuid == WRITE and response is False
        key = SESSION_KEY if self.e702_acknowledged else OEM_KEY
        plain = crypt(encrypted, key, decrypt=True)
        assert valid(plain), 'The host must encrypt a valid packet with the right phase key'
        self.writes.append(plain)
        if plain[:2] == b'\xe7\x01':
            if self.radio.lost_key_replies:
                self.radio.lost_key_replies -= 1
                return
            self.callback(None, crypt(packet(0xe7, 1, SESSION_KEY), OEM_KEY))
        elif plain[:2] == b'\xe7\x02':
            self.radio.e702_requested.set()
            if not self.radio.gated:
                self.reply_e702()
        elif plain[:2] == b'\xaa\x14':
            assert self.e702_acknowledged, 'Identity must be queried only after E702 succeeds'
            self.radio.identity_requested.set()
            if not self.radio.gated:
                self.reply_identity()
        else:
            raise AssertionError('Authentication must not write power, mode or colors')


class AuthRetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Exercise the real receive/checksum/decryption path, shortening only
        # its wall-clock wait. Never scan, open a BLE client, or read a real key.
        async def short_wait_for(awaitable, timeout):
            return await REAL_WAIT_FOR(awaitable, min(timeout, .2))
        self.timeouts = patch('transport.asyncio.wait_for', new=short_wait_for)
        self.timeouts.start()
        self.addCleanup(self.timeouts.stop)

    def make_transport(self, radio):
        fake = patch.dict('sys.modules', {'bleak': SimpleNamespace(BleakClient=radio.make_client)})
        fake.start()
        self.addCleanup(fake.stop)
        discovery = FakeDiscovery()
        return BleTransport(DEVICE, OEM_KEY, discovery), discovery

    async def test_first_e701_lost_retries_same_connection_and_waits_for_e702_and_aa14(self):
        radio = FakeEncryptedRadio(gated=True)
        transport, discovery = self.make_transport(radio)
        connecting = asyncio.create_task(transport.connect())
        try:
            await REAL_WAIT_FOR(radio.e702_requested.wait(), 2)
            self.assertFalse(connecting.done(), 'E701 alone must not complete initialization')
            self.assertIsNone(transport.session_key)
            client = radio.clients[0]
            self.assertEqual([p[:2] for p in client.writes], [b'\xe7\x01', b'\xe7\x01', b'\xe7\x02'])
            client.reply_e702()
            await REAL_WAIT_FOR(radio.identity_requested.wait(), 2)
            self.assertFalse(connecting.done(), 'E702 alone must not complete initialization')
            client.reply_identity()
            await REAL_WAIT_FOR(connecting, 2)
            self.assertTrue(transport.connected)
            self.assertEqual(transport.session_key, SESSION_KEY)
            self.assertEqual(discovery.calls, [DEVICE['ble_address']])
            self.assertEqual(len(radio.clients), 1)
            self.assertEqual(client.connect_count, 1)
            self.assertEqual(client.disconnect_count, 0)
            self.assertEqual([p[:2] for p in client.writes], [b'\xe7\x01', b'\xe7\x01', b'\xe7\x02', b'\xaa\x14'])
        finally:
            if not connecting.done():
                connecting.cancel()
                await asyncio.gather(connecting, return_exceptions=True)
            await transport.disconnect()

    async def test_two_e701_timeouts_disconnect_without_any_mode_or_color_write(self):
        radio = FakeEncryptedRadio(lost_key_replies=2)
        transport, discovery = self.make_transport(radio)
        with self.assertRaisesRegex(TimeoutError, 'e701'):
            await transport.connect()
        client = radio.clients[0]
        self.assertEqual([p[:2] for p in client.writes], [b'\xe7\x01', b'\xe7\x01'])
        self.assertEqual(discovery.calls, [DEVICE['ble_address']])
        self.assertEqual(client.connect_count, 1)
        self.assertEqual(client.disconnect_count, 1)
        self.assertEqual(client.stop_notify_count, 1)
        self.assertFalse(transport.connected)
        self.assertIsNone(transport.client)
        self.assertIsNone(transport.session_key)
        with self.assertRaisesRegex(ConnectionError, 'not authenticated'):
            await transport.send(realtime((1, 2, 3)))
        self.assertEqual(len(client.writes), 2)

    async def test_wrong_aa14_after_retry_is_rejected_and_disconnects_before_color(self):
        radio = FakeEncryptedRadio(identity=bytes.fromhex('020000000099'))
        transport, _ = self.make_transport(radio)
        with self.assertRaisesRegex(ValueError, 'AA14 identity'):
            await transport.connect()
        client = radio.clients[0]
        self.assertEqual([p[:2] for p in client.writes], [b'\xe7\x01', b'\xe7\x01', b'\xe7\x02', b'\xaa\x14'])
        self.assertEqual(len(radio.clients), 1)
        self.assertEqual(client.disconnect_count, 1)
        self.assertFalse(transport.connected)
        self.assertIsNone(transport.session_key)
        with self.assertRaisesRegex(ConnectionError, 'not authenticated'):
            await transport.send(realtime((9, 8, 7)))
        self.assertTrue(all(p[0] in (0xe7, 0xaa) for p in client.writes))


if __name__ == '__main__':
    unittest.main()
