"""Explicit BLE profiles; metadata is not a claim of support for every revision.

The H6159 classic profile uses plain 20-byte XOR packets. The existing H6008
session retains its independently validated authenticated implementation.
"""
from dataclasses import dataclass
from typing import Callable, Optional

SERVICE = '00010203-0405-0607-0809-0a0b0c0d1910'
NOTIFY = '00010203-0405-0607-0809-0a0b0c0d2b10'
WRITE = '00010203-0405-0607-0809-0a0b0c0d2b11'


def packet(prefix, command, payload=b''):
    payload = bytes(payload)
    if len(payload) > 17:
        raise ValueError('Payload exceeds 17 bytes')
    data = bytes([prefix, command]) + payload + bytes(17-len(payload))
    checksum = 0
    for value in data:
        checksum ^= value
    return data + bytes([checksum])


def valid(data):
    if not isinstance(data, (bytes, bytearray)) or len(data) != 20:
        return False
    checksum = 0
    for value in data:
        checksum ^= value
    return checksum == 0


def _color(rgb):
    if not isinstance(rgb, (tuple, list)) or len(rgb) != 3 or any(type(v) is not int or not 0 <= v <= 255 for v in rgb):
        raise ValueError('RGB must contain three integers in 0..255')
    return packet(0x33, 5, bytes([2, *rgb]))


def _power(on):
    if type(on) is not bool:
        raise ValueError('Power must be a boolean')
    return packet(0x33, 1, bytes([int(on)]))


def _brightness(percent):
    if type(percent) is not int or not 0 <= percent <= 100:
        raise ValueError('Brightness must be an integer in 0..100')
    return packet(0x33, 4, bytes([round(percent * 255 / 100)]))


def _reply(data, command):
    if not valid(data) or data[:2] != bytes([0xaa, command]):
        raise ValueError('Invalid or mismatched classic state reply')
    return bytes(data[2:19])


def _plain_rgb_mode(mode):
    # H6159 SubModeColor defines [02,R,G,B,bool,R2,G2,B2]. Firmware 1.07.02
    # returns flag 01 and a cached secondary RGB; preserve all known fields.
    return (isinstance(mode, (bytes, bytearray)) and len(mode) == 17
            and mode[0] == 2 and mode[4] in (0, 1) and not any(mode[8:]))


def _matches_color_mode(mode, rgb):
    _color(rgb)  # Apply the same strict RGB input validation as the builder.
    # All verified normal RGB commands select flag 00. A flag-01 state may
    # use the alternate RGB and must not satisfy a normal color request.
    return _plain_rgb_mode(mode) and mode[4] == 0 and tuple(mode[1:4]) == tuple(rgb)


def _snapshot(power_reply, brightness_reply, mode_reply):
    power = _reply(power_reply, 1)[0]
    brightness_raw = _reply(brightness_reply, 4)[0]
    mode = _reply(mode_reply, 5)
    if power not in (0, 1):
        raise ValueError('Unknown power state')
    # Only the fields established by H6159 SubModeColor are accepted; do not
    # reinterpret unknown mode fields or silently destroy a scene snapshot.
    if not _plain_rgb_mode(mode):
        raise ValueError('Classic snapshot is not a supported plain RGB mode')
    return {'power': power, 'brightness_raw': brightness_raw,
            'brightness': round(brightness_raw * 100 / 255),
            'mode': mode, 'rgb': tuple(mode[1:4])}


def _restore(snapshot):
    mode = snapshot.get('mode')
    raw = snapshot.get('brightness_raw')
    power = snapshot.get('power')
    if not _plain_rgb_mode(mode):
        raise ValueError('Unknown classic restoration mode')
    if type(raw) is not int or not 0 <= raw <= 255 or type(power) is not int or power not in (0, 1):
        raise ValueError('Invalid classic restoration state')
    # Restore the exact raw level, never round-trip it through percent.
    return [packet(0x33, 5, mode), packet(0x33, 4, bytes([raw])), _power(bool(power))]


@dataclass(frozen=True)
class Profile:
    id: str
    model: str
    family: str
    bluetooth_only: bool
    capabilities: dict
    minimum_interval: float
    authentication: str
    color: Optional[Callable] = None
    power: Optional[Callable] = None
    brightness: Optional[Callable] = None
    decode_snapshot: Optional[Callable] = None
    restore_commands: Optional[Callable] = None
    matches_color_mode: Optional[Callable] = None
    write_response: Optional[bool] = None
    black_at_zero_brightness: bool = False
    power_off_on_black: bool = False


_PROFILES = {
    'h6159-classic-v1': Profile(
        id='h6159-classic-v1', model='H6159', family='classic', bluetooth_only=True,
        capabilities={'rgb': True, 'brightness': True, 'power': True,
                      'restore': True, 'addressable': False},
        minimum_interval=.1, authentication='none', color=_color, power=_power,
        brightness=_brightness, decode_snapshot=_snapshot, restore_commands=_restore,
        matches_color_mode=_matches_color_mode, write_response=True,
        black_at_zero_brightness=True, power_off_on_black=True),
    'h6008-realtime-v1': Profile(
        id='h6008-realtime-v1', model='H6008', family='h6008', bluetooth_only=False,
        capabilities={'rgb': True, 'brightness': False, 'power': False,
                      'restore': True, 'addressable': False},
        minimum_interval=.05, authentication='e7-aes-session'),
}


def get_profile(profile_id):
    if profile_id not in _PROFILES:
        raise ValueError('Unknown BLE profile')
    return _PROFILES[profile_id]


def list_profiles():
    """Return the registered Profile objects in stable ID order, without I/O."""
    return tuple(_PROFILES[key] for key in sorted(_PROFILES))


def profile_for_device(device):
    return get_profile(device.get('profile', 'h6008-realtime-v1'))
