"""Govee H6008 packet construction. No OEM or account credentials here."""
import functools
import operator
import secrets
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

SERVICE = '00010203-0405-0607-0809-0a0b0c0d1910'
NOTIFY = '00010203-0405-0607-0809-0a0b0c0d2b10'
WRITE = '00010203-0405-0607-0809-0a0b0c0d2b11'

def packet(protocol, command, payload=b''):
    if len(payload) > 17:
        raise ValueError('Payload exceeds 17 bytes')
    data = bytes([protocol, command]) + bytes(payload) + bytes(17-len(payload))
    return data + bytes([functools.reduce(operator.xor, data)])

def valid(data):
    return len(data) == 20 and functools.reduce(operator.xor, data[:19]) == data[19]

def start_realtime():
    return packet(0x33, 5, b'\x05\x01\0\0\0')

def realtime(rgb):
    if len(rgb) != 3 or any(type(v) is not int or not 0 <= v <= 255 for v in rgb):
        raise ValueError('RGB must contain three integers in 0..255')
    return packet(0x33, 5, bytes([5, 0, *rgb]))

def normal(rgb):
    realtime(rgb)  # Validate the same RGB domain.
    return packet(0x33, 5, bytes([13, *rgb, 0, 0, 0, 0, 0]))

def handshake(command):
    return packet(0xe7, command, secrets.token_bytes(17))

def rc4(data, key):
    state = list(range(256)); j = 0
    for i in range(256):
        j = (j + state[i] + key[i % len(key)]) % 256
        state[i], state[j] = state[j], state[i]
    i = j = 0; out = []
    for byte in data:
        i = (i + 1) % 256; j = (j + state[i]) % 256
        state[i], state[j] = state[j], state[i]
        out.append(byte ^ state[(state[i] + state[j]) % 256])
    return bytes(out)

def crypt(data, key, *, decrypt=False):
    if len(key) != 16:
        raise ValueError('The locally configured communication key must be 16 bytes')
    end = len(data) // 16 * 16
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    operation = cipher.decryptor() if decrypt else cipher.encryptor()
    return operation.update(data[:end]) + operation.finalize() + rc4(data[end:], key)
