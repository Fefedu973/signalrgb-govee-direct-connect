"""Allowlisted H6159 classic GATT transport; no I/O occurs at import/init.

Connect only verifies the target/GATT and subscribes to notifications. It does
not authenticate with E7, change power or mode, or probe unrecognized opcodes.
"""
import asyncio
import re
from profiles import SERVICE, NOTIFY, WRITE, packet, valid


class ClassicTransport:
    query_timeout = 3.0

    def __init__(self, device, profile, discovery):
        if profile.family != 'classic':
            raise ValueError('Classic transport requires a classic profile')
        self.device = device
        self.profile = profile
        self.discovery = discovery
        self.client = None
        self.queue = asyncio.Queue(maxsize=64)
        self.query_lock = asyncio.Lock()
        self.write_response = False

    @property
    def connected(self):
        return self.client is not None and self.client.is_connected

    def notification(self, _, value):
        data = bytes(value)
        if not valid(data) or data[0] != 0xaa or data[1] not in (1, 4, 5):
            return
        if self.queue.full():
            self.queue.get_nowait()
        self.queue.put_nowait(data)

    async def connect(self):
        if self.connected:
            return
        from bleak import BleakClient
        address = self.device['ble_address'].upper()
        if address not in self.discovery.allowlisted_addresses:
            raise ValueError('Classic BLE address is not allowlisted')
        found = await self.discovery.find(address)
        if found.address.upper() != address:
            raise ValueError('Discovery returned a different BLE address')
        name = getattr(found, 'name', None)
        expected_name = self.device.get('advertised_name')
        if name:
            if expected_name:
                if name != expected_name:
                    raise ValueError('Advertised name does not match the configured target')
            elif not re.search(r'(?<![A-Z0-9])' + re.escape(self.profile.model.upper()) + r'(?![A-Z0-9])', name.upper()):
                raise ValueError('Advertised target does not match the configured model')
        self.client = BleakClient(found, timeout=10)
        try:
            await self.client.connect()
            service = self.client.services.get_service(SERVICE)
            if service is None:
                raise ValueError('Expected classic GATT service is missing')
            chars = {c.uuid.lower(): c for c in service.characteristics}
            if WRITE not in chars or NOTIFY not in chars:
                raise ValueError('Expected classic GATT characteristics are missing')
            properties = set(chars[WRITE].properties)
            if not properties.intersection({'write-without-response', 'write'}):
                raise ValueError('Classic control characteristic is not writable')
            if not set(chars[NOTIFY].properties).intersection({'notify', 'indicate'}):
                raise ValueError('Classic state characteristic has no notifications')
            # The verified H6159 accepts ATT write requests even though its
            # characteristic advertises only write-without-response/read.
            self.write_response = (self.profile.write_response
                                   if self.profile.write_response is not None
                                   else 'write-without-response' not in properties)
            await self.client.start_notify(NOTIFY, self.notification)
        except BaseException:
            await self.disconnect()
            raise

    async def send(self, plain):
        if not self.connected:
            raise ConnectionError('Classic BLE connection unavailable')
        if not valid(plain) or plain[0] not in (0xaa, 0x33) or plain[1] not in (1, 4, 5):
            raise ValueError('Unsupported classic packet')
        if plain[0] == 0xaa and any(plain[2:19]):
            raise ValueError('Classic queries do not accept an unverified payload')
        if plain[0] == 0x33:
            if plain[1] == 1 and (plain[2] not in (0, 1) or any(plain[3:19])):
                raise ValueError('Unsupported classic power payload')
            if plain[1] == 4 and any(plain[3:19]):
                raise ValueError('Unsupported classic brightness payload')
            if plain[1] == 5:
                # Restoring an alternate color state must retain its flag and
                # secondary RGB. Decode through the profile's state validator;
                # matches_color_mode instead tests a requested normal RGB.
                if self.profile.decode_snapshot is None:
                    raise ValueError('Unsupported classic RGB mode')
                self.profile.decode_snapshot(packet(0xaa, 1, b'\x01'),
                                             packet(0xaa, 4, b'\xff'),
                                             packet(0xaa, 5, plain[2:19]))
        await self.client.write_gatt_char(WRITE, plain, response=self.write_response)

    async def query(self, command, payload=b''):
        if command not in (1, 4, 5) or payload:
            raise ValueError('Only verified classic state queries are supported')
        async with self.query_lock:
            # One initial query was lost immediately after GATT subscription
            # on H6159 1.07.02. Retry state reads once; never retry writes here.
            for attempt in range(2):
                while not self.queue.empty():
                    self.queue.get_nowait()
                await self.send(packet(0xaa, command))
                deadline = asyncio.get_running_loop().time() + self.query_timeout
                while True:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    try:
                        reply = await asyncio.wait_for(self.queue.get(), remaining)
                    except TimeoutError:
                        break
                    if reply[1] == command:
                        return reply
            raise TimeoutError('Classic BLE state query timed out after two attempts')

    async def disconnect(self):
        client, self.client = self.client, None
        if client is not None:
            try:
                if client.is_connected:
                    await client.stop_notify(NOTIFY)
            finally:
                await asyncio.wait_for(client.disconnect(), 2)
