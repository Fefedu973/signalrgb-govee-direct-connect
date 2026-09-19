import unittest
from configure_device import prepare


class ConfigurationEditTests(unittest.TestCase):
    def test_add_preserves_existing_device_and_secret_without_mutating_input(self):
        old={'communication_key_hex':'synthetic', 'devices':[{'device':'bulb','ble_address':'00:00:00:00:00:01'}]}
        entry={'device':'strip','ble_address':'00:00:00:00:00:02'}
        new=prepare(old,entry)
        self.assertEqual(len(old['devices']),1)
        self.assertEqual(len(new['devices']),2)
        self.assertEqual(new['communication_key_hex'],'synthetic')

    def test_reconfiguration_keeps_identity_and_private_extension_fields(self):
        old={'devices':[{'device':'strip','ble_address':'00:00:00:00:00:02','custom':'keep'}]}
        new=prepare(old,{'device':'strip','ble_address':'00:00:00:00:00:02','name':'Shelf'})
        self.assertEqual(len(new['devices']),1)
        self.assertEqual(new['devices'][0]['custom'],'keep')

    def test_identity_cannot_be_silently_reassigned(self):
        with self.assertRaises(ValueError):
            prepare({'devices':[{'device':'strip','ble_address':'00:00:00:00:00:02'}]},
                    {'device':'strip','ble_address':'00:00:00:00:00:03'})


if __name__ == '__main__':unittest.main()
