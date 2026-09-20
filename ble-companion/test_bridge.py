"""Offline tests only: fake BLE, fake clock, no sockets or radio access."""
import asyncio
import types
import unittest
from unittest.mock import patch
from bridge import Bridge, BulbSession
from protocol import crypt, normal, packet, realtime, start_realtime, valid
from transport import BleTransport, Discovery

KEY = bytes(range(16))  # Synthetic test key; not a device/OEM credential.
DEVICE = {'device':'test-bulb','ble_address':'00:00:00:00:00:01','wifi_mac':'00:00:00:00:00:02'}

class Clock:
    def __init__(self):self.value=0
    def __call__(self):return self.value

class PhysicalBulb:
    def __init__(self):
        self.mode=packet(0xaa,5,normal((10,20,30))[2:19])
        self.initial=self.mode
        self.power=1;self.brightness=60;self.writes=[];self.connections=0

class FakeTransport:
    def __init__(self,bulb,fail_connect=False):
        self.bulb=bulb;self.connected=False;self.fail_connect=fail_connect
    async def connect(self):
        self.bulb.connections+=1
        if self.fail_connect:raise ConnectionError('simulated unavailable BLE')
        self.connected=True
    async def disconnect(self):self.connected=False
    async def query(self,command,payload=b''):
        if command==1:return packet(0xaa,1,bytes([self.bulb.power]))
        if command==4:return packet(0xaa,4,bytes([self.bulb.brightness]))
        if command==5:return self.bulb.mode
        raise AssertionError('Unexpected query')
    async def send(self,plain):
        assert self.connected and valid(plain)
        self.bulb.writes.append(plain)
        if plain[:2]==b'\x33\x05':self.bulb.mode=packet(0xaa,5,plain[2:19])

class SessionTests(unittest.IsolatedAsyncioTestCase):
    def make(self,**kwargs):
        bulb=PhysicalBulb();clock=Clock()
        session=BulbSession(DEVICE,lambda d:FakeTransport(bulb,**kwargs),clock=clock)
        return session,bulb,clock

    async def test_cold_restart_orphan_realtime_establishes_requested_not_preboot_baseline(self):
        s,b,c=self.make();b.mode=packet(0xaa,5,realtime((70,80,90))[2:19])
        target=(1,2,3);baseline=packet(0xaa,5,normal(target)[2:19])
        s.acquire(target);await s.step()
        self.assertEqual(b.writes,[normal(target),start_realtime(),realtime(target)])
        self.assertEqual(s.snapshot['mode'],baseline)
        self.assertEqual(s.status()['last_mode_hex'],baseline.hex())
        self.assertEqual(s.status()['recovery_baseline'],'requested_rgb_after_orphan_realtime')
        self.assertFalse(s.status()['recovery_pending'])
        s.transport.connected=False;c.value=.2;s.acquire((8,9,10));await s.step()
        self.assertEqual(b.writes.count(normal(target)),1)
        self.assertEqual(s.snapshot['mode'],baseline)
        s.request_release();await s.step()
        self.assertEqual(b.mode,baseline)
        self.assertNotEqual(b.mode,b.initial)
        self.assertEqual((b.power,b.brightness),(1,60))

    async def test_orphan_without_rgb_and_unknown_scene_are_still_refused(self):
        for mode,rgb in ((packet(0xaa,5,b'\x05\x01'),None),
                         (packet(0xaa,5,b'\x04\x01'),(1,2,3))):
            s,b,c=self.make();b.mode=mode;s.acquire(rgb);await s.step()
            self.assertFalse(b.writes)
            self.assertIsNone(s.snapshot)
            self.assertEqual(s.status()['last_mode_hex'],mode.hex())
            self.assertTrue(s.error)

    async def test_orphan_recovery_mismatch_never_enters_realtime(self):
        b=PhysicalBulb();b.mode=packet(0xaa,5,b'\x05\x01')
        class Mismatch(FakeTransport):
            async def query(self,command,payload=b''):
                if command==5 and self.bulb.writes:
                    return packet(0xaa,5,normal((9,9,9))[2:19])
                return await super().query(command,payload)
        s=BulbSession(DEVICE,lambda d:Mismatch(b));s.acquire((1,2,3));await s.step()
        self.assertEqual(b.writes,[normal((1,2,3))])
        self.assertIsNone(s.snapshot)
        self.assertTrue(s.status()['recovery_pending'])
        self.assertIn('readback',s.error)
        self.assertEqual(s.last_mode_hex,packet(0xaa,5,normal((9,9,9))[2:19]).hex())

    async def test_lease_expiring_before_orphan_baseline_write_sends_nothing(self):
        b=PhysicalBulb();b.mode=packet(0xaa,5,b'\x05\x01');clock=Clock()
        class Expiring(FakeTransport):
            async def query(self,command,payload=b''):
                result=await super().query(command,payload)
                if command==4:clock.value=3
                return result
        s=BulbSession(DEVICE,lambda d:Expiring(b),clock=clock);s.acquire((1,2,3));await s.step()
        self.assertFalse(b.writes)
        self.assertIsNone(s.recovery_pending)
        self.assertEqual(s.state,'idle')

    async def test_release_or_expiry_during_recovery_readback_preserves_new_color(self):
        for expiry in (False,True):
            with self.subTest(expiry=expiry):
                b=PhysicalBulb();b.mode=packet(0xaa,5,b'\x05\x01');clock=Clock();readback=asyncio.Event()
                class Slow(FakeTransport):
                    async def query(self,command,payload=b''):
                        if command==5 and self.bulb.writes:
                            readback.set();await asyncio.Event().wait()
                        return await super().query(command,payload)
                s=BulbSession(DEVICE,lambda d:Slow(b),clock=clock);s.acquire((1,2,3))
                task=asyncio.create_task(s.run())
                try:
                    await asyncio.wait_for(readback.wait(),.5)
                    self.assertIsNone(s.snapshot)
                    self.assertTrue(s.status()['recovery_pending'])
                    if expiry:clock.value=3
                    else:s.request_release()
                    await asyncio.wait_for(s.released.wait(),.5)
                    self.assertTrue(all(value==normal((1,2,3)) for value in b.writes))
                    self.assertEqual(b.mode,packet(0xaa,5,normal((1,2,3))[2:19]))
                    self.assertEqual(s.state,'idle')
                    self.assertIsNone(s.restore_error)
                    self.assertEqual((b.power,b.brightness),(1,60))
                finally:
                    s.stopping=True
                    await asyncio.wait_for(task,.5)

    async def test_latest_frame_coalescing_and_cadence(self):
        s,b,c=self.make();s.acquire((1,2,3));await s.step()
        self.assertEqual(b.writes,[start_realtime(),realtime((1,2,3))])
        c.value=.05
        for i in range(100):s.acquire((i,0,0))
        await s.step();self.assertEqual(len(b.writes),2)
        c.value=.101;await s.step()
        self.assertEqual(b.writes[-1],realtime((99,0,0)))
        self.assertEqual(len(b.writes),3)

    async def test_duplicate_color_suppressed_with_heartbeat(self):
        s,b,c=self.make();s.acquire((1,2,3));await s.step()
        c.value=.6;s.heartbeat();await s.step()
        self.assertEqual(len(b.writes),2)
        self.assertTrue(s.status()['ready'])

    async def test_reconnect_reinitializes_and_keeps_original_snapshot(self):
        s,b,c=self.make();s.acquire((1,2,3));await s.step()
        s.transport.connected=False;c.value=.2;s.acquire((7,8,9));await s.step()
        self.assertEqual(b.connections,2)
        self.assertEqual(b.writes.count(start_realtime()),2)
        self.assertEqual(s.snapshot['mode'],b.initial)
        s.request_release();await s.step()
        self.assertEqual(b.mode,b.initial)
        self.assertEqual(s.state,'idle')

    async def test_three_failed_attempts_are_bounded(self):
        s,b,c=self.make(fail_connect=True)
        for _ in range(10):
            s.acquire((1,2,3));await s.step();c.value+=5
        self.assertEqual(b.connections,3)
        self.assertEqual(s.state,'failed')
        self.assertFalse(b.writes)

    async def test_white_6500k_snapshot_is_restored_byte_for_byte(self):
        s,b,c=self.make()
        original=packet(0xaa,5,bytes([13,0,0,0,0x19,0x64]))
        b.mode=original
        s.acquire((20,30,40));await s.step()
        self.assertTrue(s.status()['ready'])
        self.assertEqual(s.snapshot['mode'],original)
        self.assertEqual(b.writes,[start_realtime(),realtime((20,30,40))])
        s.request_release();await s.step()
        self.assertEqual(b.mode,original)
        self.assertEqual((b.power,b.brightness),(1,60))

    async def test_expiry_restores_only_color(self):
        s,b,c=self.make();s.acquire((1,2,3));await s.step()
        c.value=2.1;await s.step()
        self.assertEqual(b.mode,b.initial)
        self.assertEqual(b.power,1);self.assertEqual(b.brightness,60)
        self.assertTrue(all(p[:2]==b'\x33\x05' for p in b.writes))

    async def test_expiry_without_connection_never_reconnects_late(self):
        s,b,c=self.make();s.acquire((1,2,3));await s.step()
        s.transport.connected=False;c.value=2.1;before=b.connections
        await s.step()
        self.assertEqual(b.connections,before)
        self.assertIn('no late restoration',s.restore_error)
        self.assertEqual(s.state,'failed')

    async def test_frame_metrics_count_only_completed_color_writes_and_are_bounded(self):
        s,b,c=self.make()
        for i in range(300):
            c.value=i*.11;s.acquire((i%256,2,3));await s.step()
        self.assertEqual(s.frame_writes,300)
        self.assertEqual(len(s.frame_history),256)
        self.assertEqual(s.frame_history[-1],[300,round(c.value,6)])
        self.assertEqual(s.status()['frame_writes'],300)

    async def test_release_false_emits_no_restore(self):
        s,b,c=self.make();s.acquire((1,2,3));await s.step()
        count=len(b.writes);s.request_release(False);await s.step()
        self.assertEqual(len(b.writes),count)
        self.assertEqual(s.state,'idle')

    async def test_handoff_can_reacquire_its_own_realtime_mode(self):
        s,b,c=self.make();s.acquire((1,2,3));await s.step()
        s.request_release(False);await s.step()
        s.acquire((9,8,7));c.value=.2;await s.step()
        self.assertTrue(s.status()['ready'])
        self.assertEqual(b.writes[-1],realtime((9,8,7)))

    async def test_release_final_rgb_is_serialized_normal_color(self):
        s,b,c=self.make();s.acquire((1,2,3));await s.step()
        s.request_release(False,(90,80,70));await s.step()
        self.assertEqual(b.writes[-1],normal((90,80,70)))
        self.assertEqual(s.state,'idle')

    async def test_final_rgb_before_acquisition_connects_without_realtime_or_power(self):
        s,b,c=self.make();b.power=0
        s.request_release(False,(90,80,70));await s.step()
        self.assertEqual(b.writes,[normal((90,80,70))])
        self.assertEqual(b.power,0)
        self.assertEqual(s.state,'idle')

    async def test_initial_off_never_sends_color_or_power(self):
        s,b,c=self.make();b.power=0;s.acquire((1,2,3));await s.step()
        self.assertEqual(s.state,'paused_off');self.assertFalse(b.writes)
        b.power=1;c.value=.6;s.heartbeat();await s.step()
        self.assertEqual(b.writes,[start_realtime(),realtime((1,2,3))])

    async def test_off_then_on_restarts_color_even_when_rgb_unchanged(self):
        s,b,c=self.make();s.acquire((1,2,3));await s.step()
        b.power=0;c.value=.6;s.heartbeat();await s.step()
        self.assertEqual(s.state,'paused_off')
        b.power=1;c.value=1.2;s.heartbeat();await s.step()
        self.assertEqual(b.writes.count(start_realtime()),2)
        self.assertEqual(b.writes[-1],realtime((1,2,3)))

    async def test_unknown_mode_rejected_before_color(self):
        s,b,c=self.make();b.mode=packet(0xaa,5,b'\x04');s.acquire((1,2,3));await s.step()
        self.assertFalse(b.writes)
        self.assertIn('mode',s.error)

    async def test_restore_unavailable_is_reported_not_silent(self):
        s,b,c=self.make();s.acquire((1,2,3));await s.step()
        s.factory=lambda d:FakeTransport(b,fail_connect=True)
        s.transport.connected=False;s.request_release();await s.step()
        self.assertEqual(s.state,'failed')
        self.assertIsNotNone(s.restore_error)
        self.assertIsNotNone(s.snapshot)

    async def test_shutdown_upgrades_in_progress_handoff_without_losing_final_color(self):
        for final in ((90,80,70),None):
            b=PhysicalBulb();disconnect_started=asyncio.Event();continue_disconnect=asyncio.Event()
            class Slow(FakeTransport):
                async def disconnect(self):
                    disconnect_started.set()
                    await continue_disconnect.wait()
                    await super().disconnect()
            s=BulbSession(DEVICE,lambda d:Slow(b))
            s.acquire((1,2,3));await s.step()
            s.request_release(False)
            pending=asyncio.create_task(s.step())
            await disconnect_started.wait()
            s.request_release(final is None,final)
            continue_disconnect.set();await pending
            self.assertFalse(s.released.is_set())
            await s.step()
            self.assertTrue(s.released.is_set())
            expected=normal(final) if final else packet(0x33,5,b.initial[2:19])
            self.assertEqual(b.writes[-1],expected)
            self.assertEqual(s.state,'idle')

    async def test_release_interrupts_pending_connection_without_late_colors(self):
        bulb=PhysicalBulb();started=asyncio.Event()
        class Slow(FakeTransport):
            async def connect(self):
                started.set();await asyncio.Event().wait()
        s=BulbSession(DEVICE,lambda d:Slow(bulb))
        s.acquire((1,2,3));task=asyncio.create_task(s.run())
        await started.wait();s.request_release(False)
        await asyncio.wait_for(s.released.wait(),.5)
        self.assertFalse(bulb.writes)
        self.assertEqual(s.state,'idle')
        s.stopping=True;await task

    async def test_lease_interrupts_pending_initialization_without_late_colors(self):
        bulb=PhysicalBulb();clock=Clock();started=asyncio.Event()
        class Slow(FakeTransport):
            async def query(self,command,payload=b''):
                if command==5:
                    started.set();await asyncio.Event().wait()
                return await super().query(command,payload)
        s=BulbSession(DEVICE,lambda d:Slow(bulb),clock=clock)
        s.acquire((1,2,3));task=asyncio.create_task(s.run())
        await started.wait();clock.value=3
        await asyncio.wait_for(s.released.wait(),.5)
        self.assertFalse(bulb.writes)
        s.stopping=True;await task

class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_independent_bulbs_and_strict_validation(self):
        devices=[dict(DEVICE,device='bulb'+str(i)) for i in range(3)]
        bulbs={d['device']:PhysicalBulb() for d in devices}
        bridge=Bridge(devices,lambda d:FakeTransport(bulbs[d['device']]))
        response=await bridge.handle({'id':12,'op':'colors','colors':[{'device':d['device'],'rgb':[i,2,3],'on':True} for i,d in enumerate(devices)]})
        self.assertEqual(response['id'],12)
        await asyncio.gather(*(s.step() for s in bridge.sessions.values()))
        for i,d in enumerate(devices):self.assertEqual(bulbs[d['device']].writes[-1],realtime((i,2,3)))
        with self.assertRaises(ValueError):
            await bridge.handle({'op':'colors','colors':[{'device':'bulb0','rgb':[1,2,3],'on':True},{'device':'stranger','rgb':[1,2,3],'on':True}]})
        with self.assertRaises(ValueError):
            await bridge.handle({'op':'colors','colors':[{'device':'bulb0','rgb':[256,2,3],'on':True}]})

    async def test_optional_token_is_checked(self):
        bridge=Bridge([DEVICE],lambda d:None,token='synthetic-test-token')
        with self.assertRaises(ValueError):await bridge.handle({'op':'status'})
        self.assertTrue((await bridge.handle({'op':'status','token':'synthetic-test-token'}))['ok'])

    async def test_shutdown_respects_completed_lan_handoff(self):
        bulb=PhysicalBulb();bridge=Bridge([DEVICE],lambda d:FakeTransport(bulb))
        s=bridge.sessions[DEVICE['device']]
        s.acquire((1,2,3));await s.step()
        s.request_release(False);await s.step()
        count=len(bulb.writes)
        await bridge.close()
        self.assertEqual(len(bulb.writes),count)
        self.assertIsNone(s.restore_error)

class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_notification_queue_reports_expected_prefix_without_key(self):
        real_wait_for = asyncio.wait_for
        async def short_wait(awaitable, timeout):
            return await real_wait_for(awaitable, min(timeout, .01))
        for prefix in (b'\xe7\x01', b'\xe7\x02', b'\xaa\x14', b'\xaa\x01', b'\xaa\x05'):
            with self.subTest(prefix=prefix.hex()):
                transport = BleTransport(DEVICE, KEY, None)
                with patch('transport.asyncio.wait_for', short_wait):
                    with self.assertRaises(TimeoutError) as raised:
                        await transport._receive(prefix, KEY)
                self.assertEqual(str(raised.exception), 'Expected BLE reply not received: ' + prefix.hex())
                self.assertNotIn(KEY.hex(), str(raised.exception))

    async def test_invalid_notifications_are_ignored_until_matching_checked_reply(self):
        transport = BleTransport(DEVICE, KEY, None)
        expected = packet(0xaa, 5, b'\x0d\x01\x02\x03')
        corrupt = bytearray(expected); corrupt[-1] ^= 1
        transport.notification(None, b'short notification')
        transport.notification(None, crypt(bytes(corrupt), KEY))
        transport.notification(None, crypt(packet(0xaa, 4, b'\x64'), KEY))
        transport.notification(None, crypt(expected, KEY))
        reply = await transport._receive(b'\xaa\x05', KEY)
        self.assertEqual(reply, expected)
        self.assertTrue(transport.queue.empty())

    async def transport(self,wrong_identity=False):
        session=bytes(range(16,32));writes=[];notifications=[]
        class Client:
            def __init__(self,*a,**k):self.is_connected=False
            async def connect(self):self.is_connected=True
            async def disconnect(self):self.is_connected=False
            async def start_notify(self,uuid,callback):self.callback=callback
            async def stop_notify(self,*a):pass
            async def write_gatt_char(self,uuid,data,**kwargs):
                plain=crypt(data,KEY,decrypt=True);key=KEY
                if not(valid(plain) and plain[0]==0xe7):plain=crypt(data,session,decrypt=True);key=session
                self_outer.assertTrue(valid(plain));writes.append(plain[:2])
                if plain[:2]==b'\xe7\x01':reply=packet(0xe7,1,session)
                elif plain[:2]==b'\xe7\x02':reply=packet(0xe7,2)
                elif plain[:2]==b'\xaa\x14':reply=packet(0xaa,0x14,bytes.fromhex('000000000099' if wrong_identity else '000000000002'))
                else:raise AssertionError('Unexpected plaintext')
                self.callback(None,crypt(reply,key))
        class Discovery:
            async def find(self,address):return object()
        self_outer=self
        transport=BleTransport(DEVICE,KEY,Discovery())
        with patch.dict('sys.modules',{'bleak':types.SimpleNamespace(BleakClient=Client)}):
            if wrong_identity:
                with self.assertRaises(ValueError):await transport.connect()
                self.assertFalse(transport.connected)
            else:
                await transport.connect();self.assertTrue(transport.connected)
                self.assertEqual(writes,[b'\xe7\x01',b'\xe7\x02',b'\xaa\x14'])
                await transport.disconnect()

    async def test_current_encrypted_handshake_and_identity(self):await self.transport()
    async def test_wrong_aa14_refused(self):await self.transport(True)

class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_cached_advertisement_waits_then_really_rescans(self):
        clock=[100.0];scans=[];waits=[];found=types.SimpleNamespace(address=DEVICE['ble_address'])
        class Scanner:
            @staticmethod
            async def discover(**kwargs):
                scans.append(clock[0]);return [] if len(scans)==1 else [found]
        async def sleep(seconds):waits.append(seconds);clock[0]+=seconds
        d=Discovery()
        with patch.dict('sys.modules',{'bleak':types.SimpleNamespace(BleakScanner=Scanner)}),patch('transport.time.monotonic',lambda:clock[0]),patch('transport.asyncio.sleep',sleep):
            with self.assertRaises(ConnectionError):await d.find(DEVICE['ble_address'])
            self.assertIs(await d.find(DEVICE['ble_address']),found)
        self.assertEqual(scans,[100,105]);self.assertEqual(waits,[5])

    async def test_windows_direct_fallback_is_strictly_allowlisted(self):
        class Scanner:
            @staticmethod
            async def discover(**kwargs):return []
        class Device:
            def __init__(self,address,**kwargs):self.address=address
        d=Discovery([DEVICE['ble_address']],True)
        with patch.dict('sys.modules',{'bleak':types.SimpleNamespace(BleakScanner=Scanner),'bleak.backends.device':types.SimpleNamespace(BLEDevice=Device)}),patch('transport.sys.platform','win32'):
            result=await d.find(DEVICE['ble_address'])
            self.assertEqual(result.address,DEVICE['ble_address'])
            self.assertEqual(d.sources[result.address],'windows_direct_address')
            self.assertIsNone(d.direct('00:00:00:00:00:99'))

if __name__=='__main__':unittest.main(verbosity=2)
