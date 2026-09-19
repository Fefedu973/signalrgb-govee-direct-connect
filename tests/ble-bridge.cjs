'use strict';
// Synthetic fixtures only. Execute production classes with fake time and UDP.
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const cp = require('node:child_process'), assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const LAN_BASELINE = 'd2657314f961319e37d9aca8cf94f10e56a3ecad';
const id = '00:00:00:00:00:00:00:01';
let checks = 0;
function check(name, run) { run(); checks++; console.log('PASS ' + name); }
function fixture({sku = 'H6008', type = 3, enabled = true, baseline = false} = {}) {
    const clock = {now: 100000}, writes = [], logs = [], sockets = [];
    const host = {log: value => logs.push(value), error: value => {throw Error(value);}, pause() {}};
    const context = vm.createContext({
        Date: class extends Date { static now() { return clock.now; } },
        encode: data => Buffer.from(data).toString('base64'), decode: data => [...Buffer.from(data, 'base64')],
        device: host, udp: {createSocket() {
            const socket = {closed: false, on() {}, bind() {}, disconnect() {},
                close() {this.closed = true;}, write(data, address, port) {
                    assert(!this.closed, 'write after socket close');
                    writes.push(JSON.parse(JSON.stringify({data, address, port})));
                }};
            sockets.push(socket); return socket;
        }}
    });
    const load = (file, exported) => {
        let src = baseline ? cp.execFileSync('git', ['show', LAN_BASELINE + ':' + file], {cwd: root, encoding: 'utf8'}) :
            fs.readFileSync(path.join(root, file), 'utf8');
        src = src.replace(/^import .*;\r?\n/gm, '').replace(/export default class /g, 'class ');
        vm.runInContext('this.' + exported + ' = (function(){\n' + src + '\nreturn ' + exported + ';})();', context, {filename: file});
    };
    if (!baseline) load('GoveeRealtimeBridge.test.js', 'GoveeRealtimeBridge');
    load('GoveeDevice.test.js', 'GoveeDevice');
    load('GoveeDeviceUI.test.js', 'GoveeDeviceUI');
    const bulb = new context.GoveeDevice({id, ip:'192.0.2.21', sku, type, leds:1, split:1, uniquePort:47001});
    bulb.setupUdpServer(); bulb.onOff = 1; bulb.hasReceivedStatus = true; bulb.lastStatus = clock.now; bulb.lastDeviceDataCheck = clock.now;
    if (!baseline) bulb.realtimeBridge.setEnabled(enabled);
    const ui = Object.create(context.GoveeDeviceUI.prototype); ui.device = host; ui.goveeDevice = bulb;
    const render = (rgb = [12,34,56], advance = 0) => { clock.now += advance; bulb.sendRGB([rgb], clock.now, 0); };
    const bridge = () => writes.filter(w => w.address === '127.0.0.1' && w.port === 47684);
    const lan = () => writes.filter(w => w.address === '192.0.2.21');
    const reply = (state = {}, request = bridge().at(-1), extra = {}) => {
        assert(request, 'expected a request to acknowledge');
        bulb.handleSocketMessage({address:'127.0.0.1', port:47684,
            data: JSON.stringify({id:request.data.id, ok:true, devices:[{
                device:id, state:'streaming', ready:true, last_sent_rgb:[12,34,56], error:null, ...state
            }], ...extra})});
    };
    return {clock, writes, logs, sockets, bulb, ui, render, bridge, lan, reply};
}
check('disabled option and non-H6008 preserve actual LAN commands', () => {
    for (const sku of ['H6008', 'H6104', 'H9999']) for (const type of [1,2,3,4,5]) {
        if (sku === 'H6008' && type === 3) continue;
        const old = fixture({sku,type,baseline:true}), current = fixture({sku,type,enabled:true});
        old.render(); current.render();
        old.bulb.singleColor([25,40,99], old.clock.now); current.bulb.singleColor([25,40,99], current.clock.now);
        assert.deepEqual(current.writes, old.writes);
    }
    const old = fixture({baseline:true}), current = fixture({enabled:false});
    old.render(); current.render(); assert.deepEqual(current.writes, old.writes);
});
check('public option defaults to false and the real UI forwards opt-in for Canvas and Forced', () => {
    const source=fs.readFileSync(path.join(root,'GoveeDirectConnect.js'),'utf8');
    const section=source.slice(source.indexOf('export function ControllableParameters'),source.indexOf('export function SubdeviceController'));
    const parameters=vm.runInNewContext(section.replace('export ','')+'; ControllableParameters();');
    assert.equal(parameters.find(p=>p.property==='H6008Realtime').default,false);
    for(const mode of ['Canvas','Forced']) {
        const s=fixture({enabled:false}); s.ui.getDeviceRGB=()=>[[1,2,3]];
        s.ui.render(mode,'#010203',s.clock.now,0,true);
        assert.equal(s.bridge()[0].data.op,'colors'); assert.equal(s.lan().length,0);
    }
});
check('H6008 acquires on its bound socket and suppresses all LAN during initialization', () => {
    const s = fixture(); s.render();
    assert.equal(s.sockets.length, 1); assert.equal(s.lan().length, 0);
    assert.deepEqual(s.bridge()[0].data.colors, [{device:id,rgb:[12,34,56],on:true}]);
    s.reply({state:'connecting',ready:false}); s.render([30,20,10],500);
    assert.equal(s.lan().length, 0); assert(s.bridge().some(w => w.data.op === 'heartbeat'));
    s.reply(); assert.equal(s.bulb.realtimeBridge.phase,'streaming');
});
check('initially off startup requests LAN status and powers on once before BLE takeover', () => {
    const s=fixture(); s.bulb.hasReceivedStatus=false; s.bulb.onOff=0; s.bulb.lastStatus=0;
    s.render(); assert.equal(s.bridge().length,0); assert.equal(s.lan()[0].data.msg.cmd,'status');
    s.bulb.updateStatus({onOff:0,pt:null}); s.render();
    assert.equal(s.bridge().length,0);
    assert.equal(s.lan().filter(w=>w.data.msg.cmd==='turn' && w.data.msg.data.value===1).length,1);
    s.render([1,2,3],100); assert.equal(s.lan().filter(w=>w.data.msg.cmd==='turn').length,1);
    s.bulb.updateStatus({onOff:1,pt:null}); s.render(); assert.equal(s.bridge()[0].data.op,'colors'); s.reply();
    const before=s.lan().length;
    s.bulb.updateStatus({onOff:0,pt:null}); s.render([4,5,6],500); s.reply({state:'paused_off',ready:false});
    s.render([7,8,9],500); assert.equal(s.lan().length,before);
    assert.equal(s.bulb.realtimeBridge.phase,'paused');
});
check('missing startup LAN status is bounded and never starts BLE or forces power blindly', () => {
    const s=fixture(); s.bulb.hasReceivedStatus=false; s.bulb.onOff=0; s.bulb.lastStatus=0;
    s.render(); s.render([1,2,3],2000);
    assert.equal(s.bridge().length,0); assert.equal(s.bulb.realtimeBridge.phase,'cooldown');
    assert(!s.lan().some(w=>w.data.msg.cmd==='turn'));
});
check('100ms maximum color cadence, changed-color coalescing and 500ms heartbeat', () => {
    const s = fixture(); s.render(); s.reply();
    s.render([1,2,3],40); s.render([4,5,6],59);
    assert.equal(s.bridge().filter(w => w.data.op==='colors').length,1);
    s.render([7,8,9],1);
    assert.deepEqual(s.bridge().at(-1).data.colors[0].rgb,[7,8,9]);
    s.render([7,8,9],400);
    assert.equal(s.bridge().at(-1).data.op,'heartbeat'); assert.equal(s.lan().length,0);
});
check('wrong address, wrong ID, wrong port, stale or unknown replies cannot seize transport', () => {
    const s = fixture(); s.render(); const request=s.bridge()[0];
    for (const [address,port,replyId,device] of [['192.0.2.99',47684,1,id],['127.0.0.1',1234,1,id],
        ['127.0.0.1',47684,99,id],['127.0.0.1',47684,1,'00:00:00:00:00:00:00:02']]) {
        s.bulb.handleSocketMessage({address,port,data:JSON.stringify({id:replyId,ok:true,devices:[{device,ready:true,state:'streaming'}]})});
    }
    assert.equal(s.bulb.realtimeBridge.phase,'pending');
    // Use a fresh request because a matching malformed reply is consumed.
    s.render([1,2,3],500); s.reply();
    s.reply({ready:false,state:'connecting'},request);
    assert.equal(s.bulb.realtimeBridge.phase,'streaming');
});
check('missing bridge replies release then fall back after the lease expires', () => {
    const s = fixture(); s.render(); s.render([2,3,4],2000);
    assert.equal(s.bridge().at(-1).data.op,'release');
    assert.equal(s.bridge().at(-1).data.restore,false); assert.equal(s.lan().length,0);
    s.render([2,3,4],1999); assert.equal(s.lan().length,0);
    s.render([2,3,4],1); assert(s.lan().some(w=>w.data.msg.cmd==='colorwc'));
    assert.equal(s.bulb.realtimeBridge.phase,'cooldown');
});
check('healthy connecting acknowledgements get a bounded 15s window, never infinite waiting', () => {
    const s = fixture(); s.render();
    for(let i=0;i<29;i++) {
        s.reply({state:'connecting',ready:false}); s.render([12,34,56],500);
    }
    assert.equal(s.bulb.realtimeBridge.phase,'pending'); assert.equal(s.lan().length,0);
    s.reply({state:'initializing',ready:false}); s.render([12,34,56],500);
    assert.equal(s.bridge().at(-1).data.op,'release');
});
check('explicit bridge errors wait for release acknowledgement before LAN fallback', () => {
    const s = fixture(); s.render(); s.reply({state:'failed',ready:false,error:'Synthetic authentication failure'},{...s.bridge()[0]},{ok:false});
    assert.equal(s.bulb.realtimeBridge.phase,'releasing'); assert.equal(s.lan().length,0);
    s.reply({state:'idle',ready:false}); s.render();
    assert(s.lan().some(w=>w.data.msg.cmd==='colorwc'));
});
check('option disabled while streaming releases without restoration and resumes LAN', () => {
    const s = fixture(); s.render(); s.reply(); s.bulb.realtimeBridge.setEnabled(false); s.render();
    assert.equal(s.bridge().at(-1).data.restore,false); assert.equal(s.lan().length,0);
    s.reply({state:'idle',ready:false}); s.render(); assert(s.lan().length>0);
});
check('paused_off keeps the lease without LAN power-on and can return to streaming', () => {
    const s = fixture(); s.render(); s.reply({state:'paused_off',ready:false});
    for(let i=0;i<5;i++) { s.render([1,2,3],500); s.reply({state:'paused_off',ready:false}); }
    assert.equal(s.bulb.realtimeBridge.phase,'paused'); assert.equal(s.lan().length,0);
    s.render([4,5,6],500); s.reply(); assert.equal(s.bulb.realtimeBridge.phase,'streaming');
});
check('transient reconnection errors keep a bounded acquisition window', () => {
    const s = fixture(); s.render(); s.reply(); s.render([1,2,3],500);
    s.reply({state:'reconnecting',ready:false,error:'Synthetic transient disconnect'});
    assert.equal(s.bulb.realtimeBridge.phase,'pending'); assert.equal(s.lan().length,0);
    s.render([4,5,6],500); s.reply({state:'connecting',ready:false,error:'Synthetic transient disconnect'});
    assert.equal(s.bulb.realtimeBridge.phase,'pending');
    s.render([7,8,9],500); s.reply(); assert.equal(s.bulb.realtimeBridge.phase,'streaming');
});
check('bridge retry is delayed for 30s after release, then reacquires without LAN', () => {
    const s = fixture(); s.render(); s.reply({state:'failed',ready:false,error:'Synthetic failure'});
    s.reply({state:'idle',ready:false}); s.render();
    const requestCount=s.bridge().length;
    s.render([1,2,3],29999); assert.equal(s.bridge().length,requestCount);
    const before=s.lan().length; s.render([1,2,3],1);
    assert.equal(s.lan().length,before); assert.equal(s.bridge().at(-1).data.op,'colors');
});
check('Forced mode uses BLE and resends the LAN color after fallback', () => {
    const s = fixture(); s.bulb.singleColor([20,30,40],s.clock.now); s.reply();
    assert.equal(s.lan().length,0); s.bulb.realtimeBridge.setEnabled(false);
    s.bulb.singleColor([20,30,40],s.clock.now); s.reply({state:'idle',ready:false});
    s.bulb.singleColor([20,30,40],s.clock.now); assert(s.lan().some(w=>w.data.msg.cmd==='colorwc'));
});
check('shutdown Release control requests restoration and closes its only socket', () => {
    const s = fixture(); s.render(); s.reply(); s.ui.shutDown('Release control','#010203');
    assert.equal(s.bridge().at(-1).data.restore,true); assert.equal(s.lan().length,0); assert(s.sockets[0].closed);
});
check('shutdown Single color delegates final color to serialized bridge cleanup', () => {
    const s = fixture(); s.render(); s.reply(); s.ui.shutDown('Single color','#010203');
    assert.deepEqual(s.bridge().at(-1).data.final_rgb,[1,2,3]); assert.equal(s.bridge().at(-1).data.restore,false);
    assert.equal(s.lan().length,0); assert(s.sockets[0].closed);
});
check('shutdown Turn device off disables BLE restoration before existing LAN turn-off', () => {
    const s = fixture(); s.render(); s.reply(); s.ui.shutDown('Turn device off',undefined);
    const releaseIndex=s.writes.findIndex(w=>w.data.op==='release');
    const offIndex=s.writes.findIndex(w=>w.data.msg && w.data.msg.cmd==='turn');
    assert(releaseIndex>=0 && offIndex>releaseIndex); assert.equal(s.writes[releaseIndex].data.restore,false);
    assert(s.lan().filter(w=>w.data.msg.cmd==='turn').every(w=>w.data.msg.data.value===0));
    assert(s.sockets[0].closed);
});
for (const mode of ['Single color', 'Release control']) {
    check(`shutdown ${mode} promotes an in-flight fallback release`, () => {
        const s = fixture(); s.render(); s.reply(); s.render([3,4,5],2000);
        const fallback=s.bridge().at(-1);
        assert.equal(fallback.data.op,'release'); assert.equal(fallback.data.restore,false);
        s.ui.shutDown(mode,'#010203');
        const promoted=s.bridge().at(-1);
        assert(promoted.data.id>fallback.data.id); assert.equal(promoted.data.op,'release');
        assert.equal(promoted.data.restore,mode==='Release control');
        if(mode==='Single color') assert.deepEqual(promoted.data.final_rgb,[1,2,3]);
        s.reply({state:'idle',ready:false},fallback);
        assert.equal(s.bulb.realtimeBridge.phase,'releasing');
        s.reply({state:'idle',ready:false},promoted);
        assert.equal(s.bulb.realtimeBridge.phase,'cooldown');
        assert.equal(s.lan().length,0); assert(s.sockets[0].closed);
    });
}
console.log(`${checks} BLE integration checks passed; all UDP and host I/O stubbed.`);
