'use strict';
// Execute the real addon code in isolated VM contexts. Only SignalRGB host I/O
// is substituted: no UDP sockets, registry writes, or light commands occur.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const patched = path.resolve(__dirname, '..');
// Synthetic documentation addresses and invented device identifiers only.
// No device is contacted: all SignalRGB I/O is stubbed below.
const fixtures = [1, 2, 3].map(index => {
    const deviceId = '00:00:00:00:00:00:00:0' + index;
    const ip = '192.0.2.' + (20 + index);
    return {device_id: deviceId, cached_ip: '192.0.2.' + (10 + index),
        cached_sku: 'H6008', cached_firmware: '1.0.0', unique_port: 47000 + index,
        first_scan_reply: {ip_src: ip, response: {ip, device: deviceId,
            sku: 'H6008', bleVersionSoft: '1.0.0'}}};
});
let checks = 0;
function check(name, fn) { fn(); checks++; console.log('PASS ' + name); }
function load(candidate) {
    const settings = new Map();
    const events = [], writes = [], logs = [], live = new Map();
    const socket = () => ({ on() {}, bind() {}, close() {}, disconnect() {}, write(data, ip, port) { writes.push({ data, ip, port }); } });
    const context = vm.createContext({
        udp: { createSocket: socket }, goveeProducts: { default: { base64Image: '' } },
        encode: data => Buffer.from(data).toString('base64'),
        decode: data => [...Buffer.from(data, 'base64')],
        service: {
            log: text => logs.push(text),
            getSetting: (id, key) => settings.get(id + '/' + key),
            saveSetting: (id, key, value) => settings.set(id + '/' + key, value),
            hasController: id => live.has(id),
            addController: c => { assert(!live.has(c.id)); live.set(c.id, c); events.push(['add', c.id]); },
            updateController: () => {},
            removeController: c => { assert(live.has(c.id), 'remove must use registered old ID'); live.delete(c.id); events.push(['remove', c.id]); },
            announceController: c => events.push(['announce', c.id]),
        },
        device: { log() {}, error: text => { throw Error(text); }, pause() {} },
    });
    const run = (filename, expose) => {
        const candidatePath = path.join(candidate, filename);
        const src = fs.readFileSync(candidatePath, 'utf8')
            .replace(/^import .*;\r?\n/gm, '').replace(/export default class /g, 'class ').replace(/export function /g, 'function ');
        vm.runInContext(src + '\n' + expose, context, { filename });
    };
    run('GoveeDevice.test.js', 'this.GoveeDevice = GoveeDevice;');
    run('GoveeController.test.js', 'this.GoveeController = GoveeController;');
    run('GoveeDirectConnect.js', 'this.DiscoveryService = DiscoveryService;');
    const discovery = new context.DiscoveryService();
    context.discovery = discovery;
    const cache = {};
    for (const f of fixtures) {
        const d = { id: f.device_id, ip: f.cached_ip, leds: 1, type: 3, split: 1, sku: f.cached_sku, bleVersionSoft: f.cached_firmware, uniquePort: f.unique_port };
        const dev = new context.GoveeDevice(d); dev.save(); cache[d.ip] = dev.toCacheJSON();
    }
    settings.set('ipCache/cache', JSON.stringify(cache));
    settings.set('ipCache/lastUniquePort', 47010);
    discovery.Initialize(); discovery.loadForcedDevices();
    return { context, discovery, settings, events, writes, logs, live };
}
const replay = (s, f, overrides = {}, source) => s.discovery.handleSocketMessage({
    address: source || f.first_scan_reply.ip_src,
    data: JSON.stringify({ msg: { cmd: 'scan', data: { ...f.first_scan_reply.response, ...overrides } } }),
});
check('three synthetic device IDs recover to their new addresses', () => {
    const s = load(patched); fixtures.forEach(f => replay(s, f));
    assert.deepEqual([...s.live.keys()].sort(), fixtures.map(f => f.cached_ip).sort());
    const cache = JSON.parse(s.settings.get('ipCache/cache'));
    for (const f of fixtures) {
        const ip = f.first_scan_reply.ip_src, c = s.live.get(f.cached_ip);
        assert.equal(c.id, f.cached_ip); assert.equal(c.device.ip, ip);
        assert.equal(c.device.controllerId, f.cached_ip);
        assert.equal(c.device.id, f.device_id); assert.equal(c.device.type, 3);
        assert.equal(c.device.leds, 1); assert.equal(c.device.split, 1);
        assert.equal(cache[ip].id, f.device_id); assert.equal(cache[f.cached_ip], undefined);
        assert.equal(s.settings.get(f.device_id + '/ip'), ip);
        assert(c.device.uniquePort > 47010);
        assert(s.events.some(e => e[0] === 'remove' && e[1] === f.cached_ip));
    }
    assert.equal(new Set([...s.live.values()].map(c => c.device.uniquePort)).size, 3);
});
check('duplicate scan is idempotent and status reaches replacement renderer port', () => {
    const s = load(patched), f = fixtures[1]; replay(s, f);
    const count = s.events.length; replay(s, f); assert.equal(s.events.length, count);
    const c = s.live.get(f.cached_ip);
    const status = {msg: {cmd: 'status', data: {onOff: 1, brightness: 100, color: {r: 255, g: 255, b: 255}, colorTemInKelvin: 0}}};
    s.discovery.handleSocketMessage({ address: c.device.ip, data: JSON.stringify(status) });
    const relay = s.writes.at(-1); assert.equal(relay.ip, '127.0.0.1'); assert.equal(relay.port, c.device.uniquePort);
    const renderer = new s.context.GoveeDevice(c.device); renderer.setupUdpServer();
    renderer.handleSocketMessage({ data: JSON.stringify(relay.data) });
    renderer.lastDeviceDataCheck = Date.now(); renderer.sendRGB([[12, 34, 56]], Date.now(), 0);
    const rgb = s.writes.at(-1); assert.equal(rgb.ip, f.first_scan_reply.ip_src); assert.equal(rgb.port, 4003);
    assert.equal(JSON.stringify(rgb.data), JSON.stringify({msg:{cmd:'colorwc',data:{color:{r:12,g:34,b:56},colorTemInKelvin:0}}}));
});
check('unknown ID, mismatched SKU/address, and malformed input cannot move a device', () => {
    const s = load(patched), before = s.events.length, f = fixtures[0];
    replay(s, f, {device:'00:00:00:00:00:00:00:00'}); replay(s, f, {sku:'H9999'});
    replay(s, f, {ip:'192.0.2.200'});
    for (const value of [null, {}, {address:'192.0.2.21',data:'{'}, {address:'192.0.2.21',data:'null'}]) s.discovery.handleSocketMessage(value);
    assert.equal(s.events.length, before); assert.equal(s.live.size, 3);
});
check('address collision preserves both registered devices and settings', () => {
    const s = load(patched), before = s.settings.get('ipCache/cache'), f = fixtures[0], occupied = fixtures[1].cached_ip;
    replay(s, f, {ip:occupied}, occupied);
    assert.equal(s.settings.get('ipCache/cache'), before); assert.equal(s.live.size, 3);
});
check('manual Update rekeys discovery, persistent cache, registration and relay port', () => {
    const s = load(patched), f = fixtures[0], c = s.live.get(f.cached_ip), ip = '192.0.2.201';
    assert.equal(c.updateDevice('1','3','1',ip), true);
    assert(s.live.has(f.cached_ip)); assert.equal(s.discovery.GoveeDeviceControllers[ip].device.ip, ip);
    assert.equal(s.live.get(f.cached_ip).device.controllerId, f.cached_ip);
    assert.equal(s.settings.get(f.device_id + '/ip'), ip);
    assert.equal(JSON.parse(s.settings.get('ipCache/cache'))[ip].id, f.device_id);
});
check('discovery refresh emits one standard scan and respects 60 second cadence', () => {
    const s = load(patched); s.discovery.lastPollTime = 0; s.discovery.Update(false);
    let scans = s.writes.filter(w => w.ip === '239.255.255.250'); assert.equal(scans.length, 1);
    assert.equal(scans[0].port, 4001); assert.equal(scans[0].data.msg.cmd, 'scan');
    s.discovery.Update(true); scans = s.writes.filter(w => w.ip === '239.255.255.250'); assert.equal(scans.length, 1);
});
check('reload from persistent settings retains endpoint ID and migrated address', () => {
    const s = load(patched); fixtures.forEach(f => replay(s, f));
    s.live.clear();
    const d = new s.context.DiscoveryService(); s.context.discovery = d;
    d.Initialize(); d.loadForcedDevices();
    for (const f of fixtures) {
        const c = s.live.get(f.cached_ip); assert.equal(c.device.ip, f.first_scan_reply.ip_src);
        assert.equal(d.GoveeDeviceControllers[c.device.ip], c);
    }
});
check('Delete by stable UI identifier removes migrated address from both caches', () => {
    const s = load(patched), f = fixtures[0]; replay(s, f);
    s.discovery.Delete(f.cached_ip);
    assert(!s.live.has(f.cached_ip)); assert.equal(s.discovery.GoveeDeviceControllers[f.first_scan_reply.ip_src], undefined);
    assert.equal(JSON.parse(s.settings.get('ipCache/cache'))[f.first_scan_reply.ip_src], undefined);
});
console.log(`${checks} tests passed; host I/O stubbed, production addon methods and synthetic fixtures executed.`);
