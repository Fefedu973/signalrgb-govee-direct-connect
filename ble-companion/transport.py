"""Persistent BLE transport. All communication starts only on connect()."""
import asyncio
import sys
import time
from protocol import SERVICE, NOTIFY, WRITE, crypt, handshake, packet, valid

class Discovery:
    def __init__(self,allowlisted_addresses=(),windows_direct=False):
        self.lock = asyncio.Lock()
        self.cache = {}
        self.scanned = 0
        self.allowlisted_addresses = set(allowlisted_addresses)
        self.windows_direct = windows_direct
        self.sources = {}

    def direct(self,address):
        if sys.platform != 'win32' or not self.windows_direct or address not in self.allowlisted_addresses:
            return None
        # Bleak3.0.2 WinRT accepts BLEDevice.address directly and calls
        # BluetoothLEDevice.from_bluetooth_address_async, without a new scan.
        # AA14 still authenticates the identity before any mode/color write.
        from bleak.backends.device import BLEDevice
        self.sources[address]='windows_direct_address'
        return BLEDevice(address,name=None,details=None)

    async def find(self, address):
        from bleak import BleakScanner
        async with self.lock:
            if address in self.cache:
                return self.cache[address]
            remaining=5-(time.monotonic()-self.scanned)
            if self.scanned and remaining>0:
                direct=self.direct(address)
                if direct is not None:return direct
                # Do not charge repeated retries to one missed advertisement.
                # A release/lease expiry can cancel this wait.
                await asyncio.sleep(remaining)
            devices = await BleakScanner.discover(timeout=5, scanning_mode='passive')
            self.cache = {d.address.upper(): d for d in devices}
            self.scanned = time.monotonic()
            for item in self.cache:self.sources[item]='advertisement'
            if address not in self.cache:
                direct=self.direct(address)
                if direct is not None:return direct
                raise ConnectionError('Authorized bulb not found')
            return self.cache[address]

class BleTransport:
    def __init__(self, device, key, discovery):
        self.device = device
        self.key = key
        self.discovery = discovery
        self.client = None
        self.session_key = None
        self._notifying = False
        self.queue = asyncio.Queue(maxsize=64)
        self.query_lock = asyncio.Lock()

    @property
    def connected(self):
        return self.client is not None and self.client.is_connected

    def notification(self, _, value):
        if self.queue.full():
            self.queue.get_nowait()
        self.queue.put_nowait(bytes(value))

    async def connect(self):
        from bleak import BleakClient
        found = await self.discovery.find(self.device['ble_address'])
        # A new BleakClient can still reuse an incomplete Windows GATT cache.
        # Rediscover services AND characteristics from the device on reconnect.
        self.client = BleakClient(found, timeout=10,
                                  winrt={'use_cached_services': False})
        self._notifying = False
        self.session_key = None
        while not self.queue.empty():
            self.queue.get_nowait()
        try:
            await self.client.connect()
            service = self.client.services.get_service(SERVICE)
            chars = {} if service is None else {c.uuid.lower(): c for c in service.characteristics}
            missing = []
            if service is None:
                missing.append('service')
            if NOTIFY not in chars:
                missing.append('notify')
            elif not set(chars[NOTIFY].properties).intersection({'notify', 'indicate'}):
                missing.append('notify property')
            if WRITE not in chars:
                missing.append('write')
            elif 'write-without-response' not in chars[WRITE].properties:
                missing.append('write-without-response property')
            if missing:
                raise ConnectionError('H6008 GATT discovery incomplete (uncached): missing ' + ', '.join(missing))
            await self.client.start_notify(NOTIFY, self.notification)
            self._notifying = True
            # A freshly enabled subscription can lose the first response on
            # Windows. Retry the key request once on the SAME connection;
            # reconnecting immediately can repeat that first-packet loss.
            for attempt in range(2):
                await self._send(handshake(1), self.key)
                try:
                    reply = await self._receive(b'\xe7\x01', self.key)
                    break
                except TimeoutError:
                    if attempt:
                        raise
            candidate = reply[2:18]
            await self._send(handshake(2), self.key)
            await self._receive(b'\xe7\x02', self.key)
            self.session_key = candidate
            identity = await self.query(0x14)
            expected = bytes.fromhex(self.device['wifi_mac'].replace(':', ''))
            if identity[2:8] != expected:
                raise ValueError('AA14 identity does not match the authorized bulb')
        except BaseException:
            try:
                await self.disconnect()
            except Exception:
                # A cleanup failure must not replace the discovery/auth error.
                pass
            raise

    async def _send(self, plain, key):
        if not self.connected:
            raise ConnectionError('BLE connection unavailable')
        await self.client.write_gatt_char(WRITE, crypt(plain, key), response=False)

    async def _receive(self, prefix, key):
        deadline = asyncio.get_running_loop().time() + 3
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError('Expected BLE reply not received: ' + prefix.hex())
            try:
                raw = await asyncio.wait_for(self.queue.get(), remaining)
            except TimeoutError as error:
                raise TimeoutError('Expected BLE reply not received: ' + prefix.hex()) from error
            if len(raw) != 20:
                continue
            plain = crypt(raw, key, decrypt=True)
            if valid(plain) and plain.startswith(prefix):
                return plain

    async def query(self, command, payload=b''):
        async with self.query_lock:
            # Old notifications must not satisfy a fresh status request.
            while not self.queue.empty():
                self.queue.get_nowait()
            await self.send(packet(0xaa, command, payload))
            return await self._receive(bytes([0xaa, command]), self.session_key)

    async def send(self, plain):
        if self.session_key is None:
            raise ConnectionError('BLE session is not authenticated')
        await self._send(plain, self.session_key)

    async def disconnect(self):
        client, self.client = self.client, None
        notifying, self._notifying = self._notifying, False
        self.session_key = None
        if client is not None:
            try:
                if client.is_connected and notifying:
                    await asyncio.wait_for(client.stop_notify(NOTIFY), 2)
            finally:
                await asyncio.wait_for(client.disconnect(),2)
