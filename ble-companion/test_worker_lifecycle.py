"""Deterministic session-worker races; only in-memory transports are used."""
import asyncio
import unittest

from bridge import BulbSession
from classic_session import ClassicSession
from protocol import realtime
from test_bridge import PhysicalBulb, FakeTransport
from test_classic_session import Profile, Strip, Transport


DEVICE = {'device': 'synthetic-worker-device', 'ble_address': '02:00:00:00:00:01',
          'wifi_mac': '02:00:00:00:00:02'}
TARGET = (91, 82, 73)


class Clock:
    now = 0
    def __call__(self):
        return self.now


class Gate:
    def __init__(self):
        self.entered = asyncio.Event()
        self.cancel_seen = asyncio.Event()
        self.finish_cleanup = asyncio.Event()
        self.used = False
        self.query_task = None
        self.active_query = False
        self.cancellations = 0
        self.disconnect_overlapped_query = False
        self.disconnects = 0
        self.session = None
        self.color_without_snapshot = False

    async def query(self, wrapped, command, payload=b''):
        if command == 5 and not self.used:
            self.used = True
            self.query_task = asyncio.current_task()
            self.active_query = True
            self.entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancellations += 1
                self.cancel_seen.set()
                try:
                    await self.finish_cleanup.wait()
                except asyncio.CancelledError:
                    self.cancellations += 1
                    raise
                raise
            finally:
                self.active_query = False
        return await wrapped.query(command, payload)


class MemoryTransport:
    def __init__(self, wrapped, gate):
        self.wrapped, self.gate = wrapped, gate

    @property
    def connected(self):
        return self.wrapped.connected

    async def connect(self):
        await self.wrapped.connect()

    async def query(self, command, payload=b''):
        return await self.gate.query(self.wrapped, command, payload)

    async def send(self, data):
        if data[:2] == b'\x33\x05' and self.gate.session.snapshot is None:
            self.gate.color_without_snapshot = True
        await self.wrapped.send(data)

    async def disconnect(self):
        self.gate.disconnects += 1
        self.gate.disconnect_overlapped_query |= self.gate.active_query
        await self.wrapped.disconnect()


class WorkerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def make_session(self, family):
        gate, clock = Gate(), Clock()
        if family == 'h6008':
            physical = PhysicalBulb()
            factory = lambda _: MemoryTransport(FakeTransport(physical), gate)
            session = BulbSession(DEVICE, factory, clock=clock)
            expected = realtime(TARGET)
        else:
            physical = Strip()
            physical.power = 1
            factory = lambda _: MemoryTransport(Transport(physical), gate)
            session = ClassicSession(DEVICE, factory, Profile(), clock=clock)
            expected = Profile.color(TARGET)
        gate.session = session
        session.acquire((1, 2, 3))
        worker = asyncio.create_task(session.run(), name='synthetic-' + family)
        self.addAsyncCleanup(self.finish, session, worker, gate)
        await asyncio.wait_for(gate.entered.wait(), 1)
        return session, worker, gate, clock, physical, expected

    async def finish(self, session, worker, gate):
        # Always leave no child task behind, including on the buggy baseline.
        session.stopping = True
        gate.finish_cleanup.set()
        if gate.query_task is not None and not gate.query_task.done():
            gate.query_task.cancel()
        if not worker.done():
            worker.cancel()
        tasks = [worker]
        if gate.query_task is not None:
            tasks.append(gate.query_task)
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)

    async def assert_resumed(self, session, worker, gate, physical, expected):
        gate.finish_cleanup.set()
        # Let the parent consume the child's CancelledError before retrying.
        await asyncio.sleep(.08)
        self.assertFalse(worker.done(), 'A cancelled operation must not permanently stop its worker')
        if session.releasing or session.state == 'releasing':
            await asyncio.wait_for(session.released.wait(), 1)
        # Only a completed explicit release requires a client retry. Renewal
        # during expiry cancellation must preserve the already accepted RGB.
        if not session.active():
            session.acquire(TARGET)
        async def written():
            while expected not in physical.writes:
                self.assertFalse(worker.done(), 'Worker exited instead of sending the new requested color')
                session.heartbeat()
                await asyncio.sleep(.01)
        await asyncio.wait_for(written(), 1)
        self.assertTrue(session.active())
        self.assertFalse(gate.disconnect_overlapped_query)
        self.assertFalse(gate.color_without_snapshot, 'Reconnect must capture restoration state before any color write')
        self.assertIsNotNone(session.snapshot)

    async def test_expiry_then_reacquire_before_cancel_is_handled_keeps_workers_alive(self):
        for family in ('h6008', 'classic'):
            with self.subTest(family=family):
                session, worker, gate, clock, physical, expected = await self.make_session(family)
                clock.now = 3
                await asyncio.wait_for(gate.cancel_seen.wait(), 1)
                session.acquire(TARGET)
                session.heartbeat()
                self.assertTrue(session.active())
                await self.assert_resumed(session, worker, gate, physical, expected)
                await self.finish(session, worker, gate)

    async def test_explicit_release_and_immediate_reacquire_never_kills_workers(self):
        for family in ('h6008', 'classic'):
            with self.subTest(family=family):
                session, worker, gate, clock, physical, expected = await self.make_session(family)
                session.request_release(restore=False)
                await asyncio.wait_for(gate.cancel_seen.wait(), 1)
                try:
                    session.acquire(TARGET)
                except RuntimeError as error:
                    # Refusing acquisition until cleanup ACK is a valid policy.
                    self.assertIn('release', str(error).lower())
                await self.assert_resumed(session, worker, gate, physical, expected)
                await self.finish(session, worker, gate)

    async def test_external_worker_cancel_drains_child_before_disconnect(self):
        for family in ('h6008', 'classic'):
            with self.subTest(family=family):
                session, worker, gate, clock, physical, expected = await self.make_session(family)
                worker.cancel()
                await asyncio.wait_for(gate.cancel_seen.wait(), .3)
                self.assertFalse(worker.done(), 'External cancellation must wait for the child cleanup')
                gate.finish_cleanup.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(worker, 1)
                self.assertTrue(gate.query_task.done())
                self.assertFalse(gate.active_query)
                self.assertFalse(gate.disconnect_overlapped_query)
                self.assertGreaterEqual(gate.disconnects, 1)
                self.assertFalse(session.active())
                self.assertIsNone(session.transport)
                self.assertEqual(physical.writes, [])

    async def test_expired_lease_cancels_child_only_once_while_cleanup_is_pending(self):
        for family in ('h6008', 'classic'):
            with self.subTest(family=family):
                session, worker, gate, clock, physical, expected = await self.make_session(family)
                clock.now = 3
                await asyncio.wait_for(gate.cancel_seen.wait(), 1)
                await asyncio.sleep(.16)  # More than three existing 50-ms polls.
                self.assertEqual(gate.cancellations, 1, 'Repeated cancellation interrupts BLE disconnect cleanup')
                self.assertFalse(gate.query_task.done())
                gate.finish_cleanup.set()
                await asyncio.wait_for(session.released.wait(), 1)
                self.assertFalse(worker.done())
                self.assertFalse(gate.disconnect_overlapped_query)
                self.assertEqual(physical.writes, [])
                await self.finish(session, worker, gate)


if __name__ == '__main__':
    unittest.main()
