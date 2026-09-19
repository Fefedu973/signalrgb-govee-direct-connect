"""Profile selection and API isolation tests; no BLE or network I/O."""
import json
from pathlib import Path
import tempfile
import unittest
from dataclasses import replace

from bridge import Bridge, BulbSession, load_config

BULB = {'device':'test-bulb','ble_address':'00:00:00:00:00:01','wifi_mac':'00:00:00:00:00:02'}
STRIP = {'device':'test-strip','ble_address':'00:00:00:00:00:03','profile':'h6159-classic-v1','name':'Shelf strip'}


class ConfigTests(unittest.TestCase):
    def load(self, devices, **extra):
        config = {'port':47684,'devices':devices, **extra}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'example.json'
            path.write_text(json.dumps(config), encoding='utf-8')
            return load_config(path)

    def test_classic_config_requires_no_oem_key_or_wifi_mac(self):
        config, key, interval, lease = self.load([STRIP.copy()])
        self.assertEqual(key, b'')
        self.assertEqual(config['devices'][0]['profile'], 'h6159-classic-v1')

    def test_existing_three_bulbs_and_optional_strip(self):
        bulbs = [dict(BULB, device='test-bulb-'+str(i), ble_address='00:00:00:00:01:%02X'%i) for i in range(3)]
        config, key, _, _ = self.load(bulbs + [STRIP.copy()], communication_key_hex=bytes(range(16)).hex())
        self.assertEqual(len(config['devices']),4)
        self.assertEqual(len(key),16)

    def test_unknown_profile_duplicate_address_and_missing_key_rejected(self):
        for devices in ([dict(STRIP, profile='unknown')], [STRIP,dict(STRIP,device='duplicate')], [BULB]):
            with self.subTest(devices=devices), self.assertRaises(ValueError):
                self.load(devices)


class ApiTests(unittest.IsolatedAsyncioTestCase):
    def make(self):
        def no_io(device):
            raise AssertionError('API metadata and validation must not connect hardware')
        return Bridge([BULB.copy(), STRIP.copy()], no_io)

    async def test_catalog_excludes_existing_lan_bulbs_and_private_fields(self):
        bridge=self.make()
        result=await bridge.handle({'id':1,'op':'catalog'})
        self.assertEqual(len(result['devices']),1)
        item=result['devices'][0]
        self.assertEqual(item['device'],'test-strip')
        self.assertEqual(item['profile'],'h6159-classic-v1')
        self.assertEqual(item['leds'],1)
        self.assertFalse(item['capabilities']['addressable'])
        self.assertNotIn('ble_address',json.dumps(result))
        self.assertIsInstance(bridge.sessions['test-bulb'],BulbSession)

    async def test_strip_acquisition_uses_same_api_without_changing_bulb(self):
        bridge=self.make()
        reply=await bridge.handle({'id':2,'op':'strip_colors','device':'test-strip','rgb':[10,20,30],'brightness':40,'on':True})
        self.assertTrue(reply['ok'])
        self.assertEqual(reply['devices'][0]['device'],'test-strip')
        self.assertTrue(bridge.sessions['test-strip'].active())
        self.assertFalse(bridge.sessions['test-bulb'].active())

    async def test_profiles_cannot_be_controlled_through_wrong_protocol(self):
        bridge=self.make()
        invalid=[
            {'op':'strip_colors','device':'test-bulb','rgb':[1,2,3],'brightness':50,'on':True},
            {'op':'colors','colors':[{'device':'test-strip','rgb':[1,2,3],'on':True}]},
            {'op':'strip_colors','device':'test-strip','rgb':[1,2,3],'brightness':True,'on':True},
            {'op':'strip_release','device':'test-strip','mode':'unknown'},
            {'op':'heartbeat','devices':[{}]},
        ]
        for message in invalid:
            with self.subTest(message=message), self.assertRaises(ValueError):
                await bridge.handle(message)
        self.assertFalse(any(s.active() for s in bridge.sessions.values()))

    async def test_catalog_obeys_optional_api_authentication(self):
        bridge=self.make();bridge.token='synthetic-test-token'
        with self.assertRaises(ValueError):
            await bridge.handle({'op':'catalog'})
        self.assertTrue((await bridge.handle({'op':'catalog','token':'synthetic-test-token'}))['ok'])

    async def test_omitted_brightness_does_not_force_one_hundred_percent(self):
        bridge=self.make()
        await bridge.handle({'op':'strip_colors','device':'test-strip','rgb':[1,2,3],'on':True})
        self.assertIsNone(bridge.sessions['test-strip'].desired_brightness)

    async def test_optional_profile_capabilities_are_enforced_before_acquisition(self):
        bridge=self.make()
        session=bridge.sessions['test-strip']
        profile=replace(session.profile, capabilities=dict(session.profile.capabilities,
                        power=False,brightness=False,restore=False),power=None,brightness=None)
        bridge.profiles['test-strip']=session.profile=profile
        base={'op':'strip_colors','device':'test-strip','rgb':[1,2,3]}
        for extra in ({'on':True},{'brightness':50},{'brightness':None}):
            with self.subTest(extra=extra),self.assertRaises(ValueError):
                await bridge.handle(dict(base,**extra))
        for mode in ('off','restore'):
            with self.assertRaises(ValueError):
                await bridge.handle({'op':'strip_release','device':'test-strip','mode':mode})
        self.assertFalse(session.active())
        await bridge.handle(base)
        self.assertTrue(session.active())
        self.assertIsNone(session.desired_brightness)
        self.assertIsNone(session.desired_on)


if __name__ == '__main__':
    unittest.main()
