"""Offline GATT recovery tests; synthetic radio/keys, no Windows BLE access."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from protocol import SERVICE, NOTIFY, WRITE, crypt, packet
from transport import BleTransport
from test_auth_retry import (
    DEVICE, OEM_KEY, SESSION_KEY, FakeClient, FakeDiscovery, FakeEncryptedRadio,
)


class FakeServices:
    """A discovered attribute database, independently configurable per client."""
    def __init__(self, defect=None):
        notify = SimpleNamespace(uuid=NOTIFY, handle=2, service_uuid=SERVICE,
                                 properties=['read'] if defect == 'notify-properties' else ['notify'])
        write = SimpleNamespace(uuid=WRITE, handle=3, service_uuid=SERVICE,
                                properties=['write'] if defect == 'write-properties' else ['write-without-response'])
        chars = [c for c in (notify, write)
                 if not (defect == 'notify' and c.uuid == NOTIFY)
                 and not (defect == 'write' and c.uuid == WRITE)]
        if defect == 'service':
            chars = []
        self.characteristics = {c.handle: c for c in chars}
        self.services = {} if defect == 'service' else {
            1: SimpleNamespace(uuid=SERVICE, handle=1, characteristics=chars)}

    def __iter__(self):
        return iter(self.services.values())

    def get_service(self, uuid):
        return next((s for s in self.services.values() if s.uuid == uuid), None)

    def get_characteristic(self, uuid):
        return next((c for c in self.characteristics.values() if c.uuid == uuid), None)


class GattClient(FakeClient):
    def __init__(self, radio, found, kwargs):
        super().__init__(radio, found)
        self.kwargs = kwargs
        uncached = kwargs.get('winrt', {}).get('use_cached_services') is False
        # The simulated OS cache is incomplete; reading the peer can recover it.
        self.services = FakeServices(radio.defect if uncached else 'notify')
        self.notify_attempts = 0

    async def connect(self):
        await super().connect()
        self.radio.connect_entered.set()
        if self.radio.block_connect:
            await self.radio.gate.wait()

    async def start_notify(self, uuid, callback):
        self.notify_attempts += 1
        self.radio.notify_entered.set()
        if self.radio.block_notify:
            await self.radio.gate.wait()
        if self.radio.notify_error is not None:
            raise self.radio.notify_error
        if self.services.get_characteristic(uuid) is None:
            raise LookupError('Characteristic missing in simulated cached GATT')
        await super().start_notify(uuid, callback)

    async def stop_notify(self, uuid):
        await super().stop_notify(uuid)
        if self.radio.stop_error is not None:
            raise self.radio.stop_error

    async def disconnect(self):
        await super().disconnect()
        if self.radio.disconnect_error is not None:
            raise self.radio.disconnect_error


class GattRadio(FakeEncryptedRadio):
    def __init__(self, *, defect=None, notify_error=None, stop_error=None,
                 disconnect_error=None, block_connect=False, block_notify=False,
                 gated=False):
        super().__init__(lost_key_replies=0, gated=gated)
        self.defect = defect
        self.notify_error, self.stop_error = notify_error, stop_error
        self.disconnect_error = disconnect_error
        self.block_connect, self.block_notify = block_connect, block_notify
        self.connect_entered, self.notify_entered = asyncio.Event(), asyncio.Event()
        self.gate = asyncio.Event()

    def make_client(self, found, **kwargs):
        client = GattClient(self, found, kwargs)
        self.clients.append(client)
        return client


class GattRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def make_transport(self, radio):
        fake_bleak = patch.dict('sys.modules', {'bleak': SimpleNamespace(BleakClient=radio.make_client)})
        fake_bleak.start()
        self.addCleanup(fake_bleak.stop)
        return BleTransport(DEVICE, OEM_KEY, FakeDiscovery())

    async def test_uncached_discovery_recovers_missing_cached_notify_then_authenticates(self):
        radio = GattRadio()
        cached = radio.make_client(SimpleNamespace(address=DEVICE['ble_address']))
        self.assertIsNone(cached.services.get_characteristic(NOTIFY))
        radio.clients.clear()
        transport = self.make_transport(radio)
        try:
            await asyncio.wait_for(transport.connect(), 2)
            client = radio.clients[0]
            self.assertIs(client.kwargs['winrt']['use_cached_services'], False)
            self.assertEqual(len(radio.clients), 1)
            self.assertEqual([p[:2] for p in client.writes], [b'\xe7\x01', b'\xe7\x02', b'\xaa\x14'])
            self.assertEqual(transport.session_key, SESSION_KEY)
            self.assertTrue(transport.connected)
        finally:
            await transport.disconnect()
        self.assertEqual(client.stop_notify_count, 1)
        self.assertEqual(client.disconnect_count, 1)

    async def test_incomplete_or_incompatible_gatt_never_subscribes_or_authenticates(self):
        for defect in ('service', 'notify', 'write', 'notify-properties', 'write-properties'):
            with self.subTest(defect=defect):
                radio = GattRadio(defect=defect)
                transport = self.make_transport(radio)
                with self.assertRaises(Exception) as raised:
                    await asyncio.wait_for(transport.connect(), 2)
                self.assertNotIsInstance(raised.exception, TimeoutError)
                self.assertTrue(str(raised.exception), 'Missing GATT must have a useful diagnostic')
                client = radio.clients[0]
                self.assertEqual(client.notify_attempts, 0)
                self.assertEqual(client.writes, [])
                self.assertEqual(client.stop_notify_count, 0)
                self.assertEqual(client.disconnect_count, 1)
                self.assertIsNone(transport.client)
                self.assertIsNone(transport.session_key)

    async def test_failed_subscription_preserves_original_error_when_disconnect_fails(self):
        original = RuntimeError('Synthetic subscription failure')
        radio = GattRadio(notify_error=original, disconnect_error=OSError('Synthetic close failure'))
        transport = self.make_transport(radio)
        with self.assertRaises(RuntimeError) as raised:
            await transport.connect()
        self.assertIs(raised.exception, original)
        client = radio.clients[0]
        self.assertEqual(client.notify_attempts, 1)
        self.assertEqual(client.stop_notify_count, 0)
        self.assertEqual(client.disconnect_count, 1)
        self.assertEqual(client.writes, [])
        self.assertIsNone(transport.client)
        self.assertIsNone(transport.session_key)

    async def test_successful_subscription_still_disconnects_when_stop_notify_fails(self):
        original = RuntimeError('Synthetic unsubscribe failure')
        radio = GattRadio(stop_error=original)
        transport = self.make_transport(radio)
        await transport.connect()
        with self.assertRaises(RuntimeError) as raised:
            await transport.disconnect()
        self.assertIs(raised.exception, original)
        client = radio.clients[0]
        self.assertEqual(client.stop_notify_count, 1)
        self.assertEqual(client.disconnect_count, 1)
        self.assertIsNone(transport.client)
        self.assertIsNone(transport.session_key)
        await transport.disconnect()
        self.assertEqual(client.disconnect_count, 1, 'Repeated cleanup must be harmless')

    async def test_cancel_before_subscription_finishes_closes_without_unsubscribe(self):
        for phase in ('connect', 'subscribe'):
            with self.subTest(phase=phase):
                radio = GattRadio(block_connect=phase == 'connect', block_notify=phase == 'subscribe')
                transport = self.make_transport(radio)
                task = asyncio.create_task(transport.connect())
                try:
                    event = radio.connect_entered if phase == 'connect' else radio.notify_entered
                    await asyncio.wait_for(event.wait(), 2)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                client = radio.clients[0]
                self.assertEqual(client.writes, [])
                self.assertEqual(client.stop_notify_count, 0)
                self.assertEqual(client.disconnect_count, 1)
                self.assertIsNone(transport.client)
                self.assertIsNone(transport.session_key)

    async def test_cancel_after_subscription_preserved_even_when_cleanup_fails(self):
        radio = GattRadio(gated=True, stop_error=OSError('Synthetic unsubscribe failure'),
                          disconnect_error=RuntimeError('Synthetic disconnect failure'))
        transport = self.make_transport(radio)
        task = asyncio.create_task(transport.connect())
        try:
            await asyncio.wait_for(radio.e702_requested.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        client = radio.clients[0]
        self.assertEqual([p[:2] for p in client.writes], [b'\xe7\x01', b'\xe7\x02'])
        self.assertEqual(client.stop_notify_count, 1)
        self.assertEqual(client.disconnect_count, 1)
        self.assertIsNone(transport.client)
        self.assertIsNone(transport.session_key)

    async def test_old_key_response_cannot_seed_a_new_connection(self):
        radio = GattRadio()
        transport = self.make_transport(radio)
        stale_key = bytes(range(32, 48))
        transport.notification(None, crypt(packet(0xe7, 1, stale_key), OEM_KEY))
        try:
            await asyncio.wait_for(transport.connect(), 2)
            self.assertEqual(transport.session_key, SESSION_KEY)
            self.assertEqual([p[:2] for p in radio.clients[0].writes],
                             [b'\xe7\x01', b'\xe7\x02', b'\xaa\x14'])
            self.assertTrue(transport.queue.empty())
        finally:
            await transport.disconnect()


if __name__ == '__main__':
    unittest.main()
