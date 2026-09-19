"""Windows loopback/Proactor regression: ephemeral UDP, no LAN or BLE I/O.

Client closure did not generate ICMP 10054 on this PC, even with reporting
explicitly enabled. The real-socket churn test therefore does not claim to
reproduce that OS error. The other tests inject an OSError at Proactor.recvfrom
to exercise the actual CPython read callback and the bridge's socket recovery.

Microsoft defines SIO_UDP_CONNRESET (FALSE suppresses PORT_UNREACHABLE reports):
https://learn.microsoft.com/en-us/windows/win32/winsock/winsock-ioctls
"""
import asyncio
import contextlib
import io
import json
import socket
import sys
import unittest
from unittest.mock import patch

import bridge


class ObservedDatagrams(asyncio.DatagramProtocol):
    def __init__(self):
        self.received = asyncio.Queue()
        self.errors = asyncio.Queue()
        self.closed = asyncio.Event()

    def datagram_received(self, data, address):
        self.received.put_nowait((data, address))

    def error_received(self, error):
        self.errors.put_nowait(error)

    def connection_lost(self, error):
        self.closed.set()


class FakeBridge:
    instances = []

    def __init__(self, *args):
        self.sessions = {}
        self.start_count = self.close_count = self.calls = 0
        self.instances.append(self)

    def start(self):
        self.start_count += 1

    async def close(self):
        self.close_count += 1

    async def handle(self, message):
        self.calls += 1
        return {'ok': True, 'id': message['id'], 'calls': self.calls}


async def until(predicate, timeout=2):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.01)


@unittest.skipUnless(sys.platform == 'win32', 'Windows UDP/Proactor regression')
class WindowsUdpTests(unittest.IsolatedAsyncioTestCase):
    def loop(self):
        loop = asyncio.get_running_loop()
        self.assertIsInstance(loop, asyncio.ProactorEventLoop)
        return loop

    async def test_real_socket_closed_clients_do_not_stop_receiver(self):
        loop = self.loop()
        transport, protocol = await bridge.create_loopback_udp_endpoint(ObservedDatagrams, 0)
        address = transport.get_extra_info('sockname')
        self.assertEqual(address[0], '127.0.0.1')
        try:
            for index in range(20):
                # Receive a request, then ACK only after its client has closed.
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
                    client.bind(('127.0.0.1', 0))
                    client.sendto(str(index).encode(), address)
                data, vanished = await asyncio.wait_for(protocol.received.get(), 1)
                self.assertEqual(data, str(index).encode())
                transport.sendto(b'late-ack', vanished)
                await asyncio.sleep(.01)
                # A distinct live client must still receive a full round-trip.
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as fresh:
                    fresh.bind(('127.0.0.1', 0)); fresh.setblocking(False)
                    await loop.sock_sendto(fresh, b'next-client', address)
                    data, peer = await asyncio.wait_for(protocol.received.get(), 1)
                    self.assertEqual(data, b'next-client')
                    transport.sendto(b'ack', peer)
                    reply, _ = await asyncio.wait_for(loop.sock_recvfrom(fresh, 100), 1)
                    self.assertEqual(reply, b'ack')
                    await asyncio.sleep(0)
            self.assertTrue(protocol.errors.empty())
        finally:
            transport.abort()
            await asyncio.wait_for(protocol.closed.wait(), 1)

    async def test_injected_proactor_error_reproduces_open_but_muted_socket(self):
        loop = self.loop()
        transport, protocol = await loop.create_datagram_endpoint(
            ObservedDatagrams, local_addr=('127.0.0.1', 0))
        address = transport.get_extra_info('sockname')
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
                # The next real completed receive calls the real CPython callback,
                # whose attempt to rearm WSARecvFrom fails here deterministically.
                with patch.object(loop._proactor, 'recvfrom', side_effect=OSError(10054, 'injected')):
                    client.sendto(b'trigger', address)
                    error = await asyncio.wait_for(protocol.errors.get(), 1)
                    self.assertEqual(error.errno, 10054)
                    await asyncio.wait_for(protocol.received.get(), 1)
                client.sendto(b'cannot-be-received', address)
                with self.assertRaises(TimeoutError):
                    await asyncio.wait_for(protocol.received.get(), .1)
                self.assertFalse(transport.is_closing())
        finally:
            transport.abort()
            await asyncio.wait_for(protocol.closed.wait(), 1)

    async def test_proactor_error_reopens_udp_and_keeps_same_bridge(self):
        loop = self.loop()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reserve:
            reserve.bind(('127.0.0.1', 0))
            port = reserve.getsockname()[1]
        opened = []
        create = bridge.create_loopback_udp_endpoint
        async def record_endpoint(factory, requested_port):
            endpoint = await create(factory, requested_port)
            opened.append(endpoint)
            return endpoint
        FakeBridge.instances.clear()
        output = io.StringIO()
        config = {'devices': [], 'port': port}
        with patch.object(bridge, 'Bridge', FakeBridge), \
             patch.object(bridge, 'Discovery', return_value=None), \
             patch.object(bridge, 'create_loopback_udp_endpoint', record_endpoint), \
             contextlib.redirect_stdout(output):
            runner = asyncio.create_task(bridge.serve(config, b'', .1, 2))
            try:
                await until(lambda: len(opened) == 1)
                endpoint = ('127.0.0.1', port)
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
                    client.bind(('127.0.0.1', 0)); client.setblocking(False)
                    async def exchange(identity):
                        await loop.sock_sendto(client, json.dumps({'id': identity}).encode(), endpoint)
                        data, _ = await asyncio.wait_for(loop.sock_recvfrom(client, 1000), 1)
                        return json.loads(data)
                    self.assertEqual((await exchange('before'))['calls'], 1)
                    with patch.object(loop._proactor, 'recvfrom', side_effect=OSError(10054, 'injected')):
                        await loop.sock_sendto(client, b'{}', endpoint)
                        await asyncio.wait_for(opened[0][1].failed.wait(), 1)
                    await until(lambda: len(opened) == 2)
                    self.assertTrue(opened[0][1].closed.is_set())
                    self.assertEqual(await exchange('after'), {'ok': True, 'id': 'after', 'calls': 2})
                    self.assertEqual(len(FakeBridge.instances), 1)
                    instance = FakeBridge.instances[0]
                    self.assertEqual(instance.start_count, 1)
                    self.assertEqual(instance.close_count, 0)
            finally:
                runner.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await runner
        self.assertEqual(instance.close_count, 1)
        logs = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertTrue(any(x.get('event') == 'udp-error' and x.get('errno') == 10054 for x in logs))
        self.assertTrue(any(x.get('event') == 'udp-listener-recovered' for x in logs))
        self.assertTrue(logs[-1]['stopped'])

    async def test_three_failed_rebinds_close_bridge_and_release_port(self):
        self.loop()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reserve:
            reserve.bind(('127.0.0.1', 0))
            port = reserve.getsockname()[1]
        endpoints = []
        create = bridge.create_loopback_udp_endpoint
        attempts = 0
        async def bind_then_fail(factory, requested_port):
            nonlocal attempts
            attempts += 1
            if attempts > 1:
                raise OSError(10048, 'injected bind failure')
            endpoint = await create(factory, requested_port)
            endpoints.append(endpoint)
            return endpoint
        FakeBridge.instances.clear()
        output = io.StringIO()
        with patch.object(bridge, 'Bridge', FakeBridge), \
             patch.object(bridge, 'Discovery', return_value=None), \
             patch.object(bridge, 'create_loopback_udp_endpoint', bind_then_fail), \
             contextlib.redirect_stdout(output):
            runner = asyncio.create_task(bridge.serve({'devices': [], 'port': port}, b'', .1, 2))
            await until(lambda: bool(endpoints))
            endpoints[0][1].error_received(OSError(10054, 'injected'))
            with self.assertRaisesRegex(RuntimeError, 'recovery limit'):
                await asyncio.wait_for(runner, 2)
        self.assertEqual(attempts, 4)  # Initial bind plus exactly three retries.
        self.assertTrue(endpoints[0][1].closed.is_set())
        self.assertEqual(len(FakeBridge.instances), 1)
        self.assertEqual(FakeBridge.instances[0].start_count, 1)
        self.assertEqual(FakeBridge.instances[0].close_count, 1)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as available:
            available.bind(('127.0.0.1', port))
        logs = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(sum(x.get('event') == 'udp-rebind-error' for x in logs), 3)
        self.assertTrue(logs[-1]['stopped'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
