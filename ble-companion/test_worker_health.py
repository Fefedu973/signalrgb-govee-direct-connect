"""Offline worker supervision and API shutdown tests; no real sockets or BLE."""
import asyncio
import contextlib
import io
import time
import unittest
from unittest.mock import patch

from bridge import Bridge, serve
from test_bridge import FakeTransport, PhysicalBulb


DEVICE = {'device': 'synthetic-health-bulb', 'ble_address': '02:00:00:00:00:01',
          'wifi_mac': '02:00:00:00:00:02'}


def no_radio(_):
    raise AssertionError('Worker-health tests must not acquire a physical transport')


class WorkerHealthTests(unittest.IsolatedAsyncioTestCase):
    def make_bridge(self):
        bridge = Bridge([DEVICE], no_radio)
        self.addAsyncCleanup(self.finish_bridge, bridge)
        return bridge, bridge.sessions[DEVICE['device']]

    async def finish_bridge(self, bridge):
        for task in bridge.tasks:
            if not task.done():
                task.cancel()
        if bridge.tasks:
            await asyncio.wait_for(asyncio.gather(*bridge.tasks, return_exceptions=True), 1)
        await asyncio.wait_for(bridge.close(), 1)

    async def controlled_worker(self, behavior):
        bridge, session = self.make_bridge()
        entered = asyncio.Event()
        gate = asyncio.Event()
        async def run():
            entered.set()
            await gate.wait()
            if behavior == 'exception':
                raise ValueError('synthetic-private-payload-not-for-status')
        session.run = run
        bridge.start()
        await asyncio.wait_for(entered.wait(), 1)
        return bridge, session, gate, bridge.worker_tasks[DEVICE['device']]

    async def assert_unhealthy(self, bridge, session, expected):
        response = await bridge.handle({'id': 1, 'op': 'status'})
        status = response['devices'][0]
        self.assertFalse(status['worker_alive'])
        self.assertFalse(status['ready'])
        self.assertIn(expected, status['worker_error'])
        self.assertEqual(status['error'], status['worker_error'])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(RuntimeError, expected):
                bridge.check_workers()
        self.assertEqual(session.state, 'failed')
        self.assertIn('ble-worker-unhealthy', output.getvalue())
        self.assertNotIn('synthetic-private-payload-not-for-status', output.getvalue())
        self.assertNotIn('synthetic-private-payload-not-for-status', str(response))

    async def test_unexpected_cancelled_worker_is_visible_and_fatal(self):
        bridge, session, gate, task = await self.controlled_worker('wait')
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await self.assert_unhealthy(bridge, session, 'cancelled unexpectedly')

    async def test_worker_exception_is_retrieved_and_only_its_type_is_reported(self):
        bridge, session, gate, task = await self.controlled_worker('exception')
        gate.set()
        await asyncio.wait({task}, timeout=1)
        self.assertTrue(task.done())
        await self.assert_unhealthy(bridge, session, 'exited unexpectedly: ValueError')

    async def test_normal_early_return_is_also_an_unhealthy_worker(self):
        bridge, session, gate, task = await self.controlled_worker('return')
        gate.set()
        await asyncio.wait_for(task, 1)
        await self.assert_unhealthy(bridge, session, 'exited unexpectedly')

    async def test_operation_age_limit_is_strict_and_status_overrides_ready(self):
        bridge, session, gate, task = await self.controlled_worker('wait')
        session.operation_started = 100.0
        session.state = 'streaming'
        session.requested = True
        session.deadline = time.monotonic() + 10
        physical = PhysicalBulb()
        session.transport = FakeTransport(physical)
        await session.transport.connect()
        self.assertIsNone(bridge.worker_status(session, now=130)['worker_error'])
        overdue = bridge.worker_status(session, now=130.001)
        self.assertTrue(overdue['worker_alive'])
        self.assertEqual(overdue['operation_age_ms'], 30001)
        self.assertIn('exceeded 30 seconds', overdue['worker_error'])
        with patch('bridge.time.monotonic', return_value=130.001):
            response = await bridge.handle({'op': 'status'})
            self.assertFalse(response['devices'][0]['ready'])
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, 'exceeded 30 seconds'):
                    bridge.check_workers()
        self.assertEqual(physical.writes, [])
        await session.transport.disconnect()
        session.transport = None
        session.operation_started = None

    async def test_paused_off_worker_remains_healthy_without_restart_or_power_write(self):
        physical = PhysicalBulb()
        physical.power = 0
        bridge = Bridge([DEVICE], lambda _: FakeTransport(physical))
        self.addAsyncCleanup(self.finish_bridge, bridge)
        session = bridge.sessions[DEVICE['device']]
        session.acquire((9, 8, 7))
        bridge.start()
        task = bridge.worker_tasks[DEVICE['device']]
        async def paused():
            while session.state != 'paused_off':
                await asyncio.sleep(.005)
        await asyncio.wait_for(paused(), 1)
        for _ in range(5):
            session.heartbeat()
            bridge.check_workers()
            response = await bridge.handle({'op': 'status'})
            state = response['devices'][0]
            self.assertEqual(state['state'], 'paused_off')
            self.assertTrue(state['worker_alive'])
            self.assertIsNone(state['worker_error'])
            self.assertFalse(state['ready'])
            self.assertIs(bridge.worker_tasks[DEVICE['device']], task)
            await asyncio.sleep(.025)
        self.assertEqual(physical.connections, 1)
        self.assertEqual(physical.writes, [])
        self.assertEqual(physical.power, 0)

    async def test_intentional_close_does_not_classify_finished_workers_as_faults(self):
        classic = {'device': 'synthetic-health-strip', 'ble_address': '02:00:00:00:00:03',
                   'profile': 'h6159-classic-v1'}
        bridge = Bridge([DEVICE, classic], no_radio)
        self.addAsyncCleanup(self.finish_bridge, bridge)
        bridge.start()
        await asyncio.sleep(.025)
        await asyncio.wait_for(bridge.close(), 1)
        self.assertTrue(bridge.closing)
        self.assertTrue(all(task.done() for task in bridge.tasks))
        bridge.check_workers()
        response = await bridge.handle({'op': 'status'})
        for state in response['devices']:
            self.assertFalse(state['worker_alive'])
            self.assertIsNone(state['worker_error'])
            self.assertIsNone(state['error'])

    async def test_second_start_cannot_create_duplicate_workers(self):
        bridge, session = self.make_bridge()
        bridge.start()
        original = tuple(bridge.tasks)
        with self.assertRaisesRegex(RuntimeError, 'already started'):
            bridge.start()
        self.assertEqual(tuple(bridge.tasks), original)

    async def test_fatal_worker_check_closes_api_and_bridge_through_real_serve_finally(self):
        endpoints = []
        class Endpoint:
            def __init__(self, protocol):
                self.protocol = protocol
                self.closed = False
            def close(self):
                self.closed = True
                self.protocol.connection_lost(None)
            def abort(self):
                self.close()
            def sendto(self, *_):
                raise AssertionError('No request or packet should be sent in this test')
        async def endpoint_factory(protocol_factory, port):
            self.assertEqual(port, 47684)
            api = protocol_factory()
            endpoint = Endpoint(api)
            api.connection_made(endpoint)
            endpoints.append((endpoint, api))
            return endpoint, api
        async def failed_worker(_):
            raise RuntimeError('synthetic-private-payload-not-for-status')
        config = {'devices': [DEVICE], 'port': 47684}
        output = io.StringIO()
        with patch('bridge.create_loopback_udp_endpoint', endpoint_factory), \
             patch('bridge.BulbSession.run', failed_worker), \
             contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(RuntimeError, 'Worker exited unexpectedly: RuntimeError'):
                await asyncio.wait_for(serve(config, bytes(range(16)), .1, 2, seconds=1), 2)
        self.assertEqual(len(endpoints), 1)
        endpoint, api = endpoints[0]
        self.assertTrue(endpoint.closed)
        self.assertTrue(api.stopping)
        self.assertTrue(api.closed.is_set())
        self.assertTrue(api.bridge.closing)
        self.assertTrue(all(task.done() for task in api.bridge.tasks))
        self.assertNotIn('synthetic-private-payload-not-for-status', output.getvalue())


if __name__ == '__main__':
    unittest.main()
