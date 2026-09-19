"""Add an explicitly identified device to the private BLE allowlist.

Default is preview only. No Bluetooth, network or lighting I/O is performed.
Credentials are preserved locally and never printed.
"""
import argparse
import copy
import datetime
import json
import os
from pathlib import Path
import tempfile

from profiles import get_profile, list_profiles


def prepare(config, entry):
    candidate = copy.deepcopy(config)
    devices = candidate.setdefault('devices', [])
    matches = [i for i, device in enumerate(devices) if device.get('device') == entry['device']]
    if len(matches) > 1:
        raise ValueError('Existing duplicate device identity')
    if matches:
        previous = devices[matches[0]]
        if previous.get('ble_address', '').upper() != entry['ble_address'].upper():
            raise ValueError('Existing identity belongs to another address; use a new device ID')
        devices[matches[0]] = dict(previous, **entry)
    else:
        devices.append(entry)
    return candidate


def validate(candidate, directory):
    # Keep the temporary file beside the private config, so relative dependency
    # paths are resolved exactly as they will be during normal startup.
    from bridge import load_config
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.json',
                                     dir=directory, delete=False) as temporary:
        path = Path(temporary.name)
        json.dump(candidate, temporary, ensure_ascii=False, indent=2)
    try:
        load_config(path)
    finally:
        path.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--list-profiles', action='store_true')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--device', help='Stable local ID, distinct from the display name')
    parser.add_argument('--profile')
    parser.add_argument('--address', help='Bluetooth address confirmed for this device')
    parser.add_argument('--name')
    parser.add_argument('--advertised-name')
    parser.add_argument('--wifi-mac', help='Only for profiles requiring an authenticated Wi-Fi identity')
    parser.add_argument('--apply', action='store_true', help='Write after validation; preserve a private backup')
    args = parser.parse_args()
    if args.list_profiles:
        print(json.dumps([{'profile':p.id, 'model':p.model, 'family':p.family,
                           'authentication':p.authentication,
                           'bluetooth_only':p.bluetooth_only,
                           'capabilities':dict(p.capabilities)} for p in list_profiles()], indent=2))
        return
    if not all((args.config, args.device, args.profile, args.address, args.name)):
        parser.error('--config, --device, --profile, --address and --name are required')
    profile = get_profile(args.profile)
    path = args.config.resolve()
    if not path.parent.is_dir():
        parser.error('Create the private configuration directory first')
    if path.exists():
        original = path.read_bytes()
        config = json.loads(original.decode('utf-8-sig'))
    else:
        original = None
        config = {'bind':'127.0.0.1', 'port':47684, 'minimum_interval_ms':100,
                  'lease_ms':2000, 'devices':[]}
    entry = {'device':args.device, 'profile':profile.id, 'model':profile.model,
             'name':args.name, 'ble_address':args.address.upper()}
    if args.advertised_name:
        entry['advertised_name'] = args.advertised_name
    if args.wifi_mac:
        entry['wifi_mac'] = args.wifi_mac.upper()
    candidate = prepare(config, entry)
    validate(candidate, path.parent)
    backup = None
    if args.apply:
        if original is not None:
            stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
            backup = path.with_name(path.name + '.' + stamp + '.bak')
            with backup.open('xb') as destination:
                destination.write(original)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.json',
                                         dir=path.parent, delete=False) as temporary:
            staged = Path(temporary.name)
            json.dump(candidate, temporary, ensure_ascii=False, indent=2)
            temporary.write('\n')
        try:
            os.replace(staged, path)
        finally:
            if staged.exists():
                staged.unlink()
    print(json.dumps({'valid':True, 'applied':args.apply, 'name':args.name,
                      'profile':profile.id, 'configured_devices':len(candidate['devices']),
                      'backup_created':backup is not None,
                      'next_step':'Restart the BLE bridge after applying; SignalRGB discovers the catalogue automatically.'},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
