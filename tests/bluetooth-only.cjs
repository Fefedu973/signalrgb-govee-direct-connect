// Execute the actual ES plugin in isolated SignalRGB-style VM contexts.
// No sockets, Bluetooth, LAN, registry writes or hardware are used by this test.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'GoveeBluetoothOnly.js'), 'utf8');
let passed = 0;
const entry = (extra={}) => ({device:'synthetic-shelf', profile:'synthetic-classic-v1', model:'Synthetic',
    name:'Shelf fixture', transport:'ble', leds:1,
    capabilities:{rgb:true, addressable:false, brightness:true, power:true, restore:true}, ...extra});
function fixture(catalogEntry=entry()) {
    const clock={now:100000}, writes=[], sockets=[], live=new Map(), announcements=[], logs=[], alerts=[], cleared=[];
    const context=vm.createContext({console, Date:{now:()=>clock.now},
        udp:{createSocket() { const handlers={}; const sock={handlers, closed:false, on(event, fn){handlers[event]=fn;},
            bind(port){this.port=port;}, close(){this.closed=true;},
            write(data,address,port){ assert(!this.closed); assert.equal(typeof data,'string');
                assert.equal(address,'127.0.0.1'); assert.equal(port,47684);
                writes.push({data:JSON.parse(data),address,port,sock}); }}; sockets.push(sock); return sock;}},
        service:{log:s=>logs.push(s), hasController:id=>live.has(id),
            addController:item=>{assert(!live.has(item.id));live.set(item.id,JSON.parse(JSON.stringify(item)));},
            updateController:item=>live.set(item.id,JSON.parse(JSON.stringify(item))),
            announceController:item=>announcements.push(item.id), removeController:item=>live.delete(item.id)},
        device:{log:s=>logs.push(s), setName(){}, setSize(){}, setControllableLeds(){}, color:()=>[10,20,30],
            notify:(...args)=>{alerts.push(args);return alerts.length;}, denotify:id=>cleared.push(id), addMessage(){}},
        controller:{...JSON.parse(JSON.stringify(catalogEntry)), id:'govee-ble:'+catalogEntry.device},
        lightingMode:'Canvas', stripBrightness:75, turnOff:'Release control', shutDownColor:'#010203'});
    vm.runInContext(source.replace(/^import .*;\r?\n/gm,'').replace(/export /g,''),context);
    const run=s=>vm.runInContext(s,context);
    const reply=(request,state={},extra={})=>request.sock.handlers.message({address:'127.0.0.1',port:47684,
        data:JSON.stringify({id:request.data.id,ok:true,devices:[{device:catalogEntry.device,state:'streaming',ready:true,...state}],...extra})});
    const render=(ms=0)=>{clock.now+=ms;run('Render()');};
    return {context,clock,writes,sockets,live,announcements,logs,alerts,cleared,run,reply,render};
}
function check(name,fn){fn();passed++;console.log('PASS '+name);}
check('catalogue registers serializable generic profiles and preserves stable identity',()=>{
    const f=fixture(); f.run('var discovery = new DiscoveryService(); discovery.Initialize();');
    assert.equal(f.sockets[0].port,0); assert.equal(f.writes[0].data.op,'catalog');
    const r=f.writes[0]; r.sock.handlers.message({address:'127.0.0.1',port:47684,
        data:JSON.stringify({id:r.data.id,ok:true,devices:[entry(),entry({device:'future-device',model:'Future',profile:'future-profile'})]})});
    assert.equal(f.live.size,2); assert.equal(f.announcements.length,2);
    f.context.controller=JSON.parse(JSON.stringify(f.live.get('govee-ble:synthetic-shelf')));
    assert.equal(f.run('Validate()'),true); f.run('Initialize()'); f.render();
    assert.equal(f.writes.find(x=>x.data.op==='strip_colors').data.device,'synthetic-shelf');
    f.clock.now+=4999; f.run('discovery.Update()'); assert.equal(f.writes.filter(x=>x.data.op==='catalog').length,1);
    f.clock.now++; f.run('discovery.Update()'); assert.equal(f.writes.filter(x=>x.data.op==='catalog').length,2);
});
check('discovery rejects mismatched sender, request ID, model topology and capabilities',()=>{
    const f=fixture();f.run('var discovery=new DiscoveryService();discovery.Initialize();');const req=f.writes[0];
    const send=(devices,extra={})=>req.sock.handlers.message({address:'127.0.0.1',port:47684,
        data:JSON.stringify({id:req.data.id,ok:true,devices}),...extra});
    send([entry()],{address:'192.0.2.1'}); assert.equal(f.live.size,0);
    send([entry()],{data:JSON.stringify({id:'wrong',ok:true,devices:[entry()]})}); assert.equal(f.live.size,0);
    send([entry({leds:5}),entry({capabilities:{rgb:true,addressable:true}}),entry({capabilities:{rgb:true}}),entry({transport:'lan'})]);
    assert.equal(f.live.size,0);
});
check('explicit catalogue revocation removes devices, missing replies do not',()=>{
    const f=fixture(); f.run('var discovery=new DiscoveryService();discovery.Initialize();');
    const respond=devices=>{const r=f.writes.at(-1);r.sock.handlers.message({address:'127.0.0.1',data:JSON.stringify({id:r.data.id,ok:true,devices})});};
    respond([entry()]);f.clock.now+=5000;f.run('discovery.Update()');assert.equal(f.live.size,1);
    respond([]);assert.equal(f.live.size,0);
});
check('Canvas sends one zone, brightness and power intent only over local bridge',()=>{
    const f=fixture(); f.run('Initialize()'); f.render();const c=f.writes.find(x=>x.data.op==='strip_colors');
    assert.deepEqual(c.data,{id:c.data.id,op:'strip_colors',device:'synthetic-shelf',rgb:[10,20,30],brightness:75,on:true});
    assert.equal(f.writes.filter(x=>x.data.op==='heartbeat').length,1);
});
check('one pending color coalesces frames and no more than 10 color requests/s',()=>{
    const f=fixture();f.run('Initialize()');f.render();const first=f.writes.find(x=>x.data.op==='strip_colors');
    f.context.device.color=()=>[90,80,70]; f.render(100);assert.equal(f.writes.filter(x=>x.data.op==='strip_colors').length,1);
    f.reply(first);f.render();assert.deepEqual(f.writes.at(-1).data.rgb,[90,80,70]);
    f.reply(f.writes.at(-1));f.context.device.color=()=>[70,80,90];f.render(99);
    assert.equal(f.writes.filter(x=>x.data.op==='strip_colors').length,2);f.render(1);
    assert.equal(f.writes.filter(x=>x.data.op==='strip_colors').length,3);
});
check('Forced suppresses identical colors but heartbeats every 500ms',()=>{
    const f=fixture();f.context.lightingMode='Forced';f.context.forcedColor='#123456';f.run('Initialize()');f.render();
    for(const w of [...f.writes])f.reply(w); f.render(499);
    assert.equal(f.writes.filter(x=>x.data.op==='strip_colors').length,1);f.render(1);
    assert.equal(f.writes.filter(x=>x.data.op==='heartbeat').length,2);
    assert.deepEqual(f.writes[0].data.rgb,[18,52,86]);
});
check('foreign or stale ACK does not release color ownership',()=>{
    const f=fixture();f.run('Initialize()');f.render();const first=f.writes[0];
    f.reply(first,{}, {id:'wrong'}); f.reply(first,{device:'wrong-device'});
    f.context.device.color=()=>[99,99,99];f.render(100);assert.equal(f.writes.filter(x=>x.data.op==='strip_colors').length,1);
    f.reply(first);f.render();assert.equal(f.writes.filter(x=>x.data.op==='strip_colors').length,2);
});
check('missing ACK fails closed, alerts once, and retries only after 30 seconds',()=>{
    const f=fixture();f.run('Initialize()');f.render();f.render(2000);
    assert.equal(f.alerts.length,1);assert.equal(f.writes.at(-1).data.op,'strip_release');const n=f.writes.length;
    f.render(29999);assert.equal(f.writes.length,n);f.render(1);
    assert.equal(f.writes.filter(x=>x.data.op==='strip_colors').length,2);
});
check('acquisition deadline remains bounded even with valid initializing replies',()=>{
    const f=fixture();f.run('Initialize()');f.render();let handled=0;
    for(let i=0;i<30;i++){
        for(const w of f.writes.slice(handled)){f.reply(w,{state:'initializing',ready:false});}handled=f.writes.length;f.render(500);
    }
    assert.equal(f.alerts.length,1);assert.equal(f.writes.at(-1).data.op,'strip_release');
});
check('bridge restart idle reply triggers fresh color instead of heartbeat-only dead state',()=>{
    const f=fixture();f.run('Initialize()');f.render();for(const w of [...f.writes])f.reply(w);
    f.render(500);f.reply(f.writes.at(-1),{state:'idle',ready:false});f.render();
    assert.equal(f.writes.at(-1).data.op,'strip_colors');
});
check('externally off device stays paused for 120 seconds without release or acquisition timeout',()=>{
    const f=fixture();f.run('Initialize()');f.render();let handled=0;
    for(let i=0;i<240;i++){
        for(const w of f.writes.slice(handled))f.reply(w,{state:'paused_off',ready:false,power:0});
        handled=f.writes.length;f.render(500);
    }
    for(const w of f.writes.slice(handled))f.reply(w,{state:'paused_off',ready:false,power:0});
    assert.equal(f.alerts.length,0);
    assert.equal(f.writes.filter(x=>x.data.op==='strip_release').length,0);
    assert.equal(f.writes.filter(x=>x.data.op==='strip_colors').length,1);
    assert(f.writes.filter(x=>x.data.op==='heartbeat').length>=240);
    assert.equal(f.run('client.phase'),'paused_off');assert.equal(f.run('client.startedAt'),null);
});
check('transient reconnecting error keeps the bounded window and later ready state recovers',()=>{
    const f=fixture();f.run('Initialize()');f.render();let handled=0;
    for(let i=0;i<20;i++){
        for(const w of f.writes.slice(handled))f.reply(w,{state:'reconnecting',ready:false,error:'Synthetic BLE timeout'});
        handled=f.writes.length;f.render(500);
    }
    assert.equal(f.alerts.length,0);assert.equal(f.writes.filter(x=>x.data.op==='strip_release').length,0);
    assert.equal(f.run('client.phase'),'reconnecting');assert.equal(f.run('client.startedAt'),100000);
    for(const w of f.writes.slice(handled))f.reply(w,{state:'streaming',ready:true,error:null});
    assert.equal(f.run('client.phase'),'streaming');assert.equal(f.run('client.startedAt'),null);
    f.context.device.color=()=>[11,22,33];f.render();
    assert.equal(f.writes.at(-1).data.op,'strip_colors');
    assert.equal(f.alerts.length,0);assert.equal(f.writes.filter(x=>x.data.op==='strip_release').length,0);
});
for(const [setting,mode]of[['Release control','restore'],['Single color','color'],['Turn device off','off']])
check('shutdown '+setting+' uses serialized strip_release and closes socket',()=>{
    const f=fixture();f.context.turnOff=setting;f.run('Initialize()');f.render();f.run('Shutdown()');
    const end=f.writes.at(-1).data;assert.equal(end.op,'strip_release');assert.equal(end.mode,mode);
    if(mode==='color')assert.deepEqual(end.rgb,[1,2,3]);else assert.equal(end.rgb,undefined);
    assert.equal(f.sockets[0].closed,true);const n=f.writes.length;f.run('Shutdown()');assert.equal(f.writes.length,n);
});
check('optional brightness and power capabilities are respected',()=>{
    const f=fixture(entry({capabilities:{rgb:true,addressable:false,brightness:false,power:false,restore:true}}));
    f.run('Initialize()');f.render();assert.equal(f.writes[0].data.brightness,undefined);assert.equal(f.writes[0].data.on,undefined);
});
check('capability-aware parameters hide unsupported controls and keep a safe default',()=>{
    const f=fixture(entry({capabilities:{rgb:true,addressable:false,brightness:false,power:false,restore:false}}));
    const params=JSON.parse(JSON.stringify(f.run('ControllableParameters()')));
    assert(!params.some(p=>p.property==='stripBrightness'));
    const shutdown=params.find(p=>p.property==='turnOff');
    assert.deepEqual(shutdown.values,['Hold current color','Single color']);assert.equal(shutdown.default,'Hold current color');
    const full=fixture();const supported=JSON.parse(JSON.stringify(full.run('ControllableParameters()')));
    assert(supported.some(p=>p.property==='stripBrightness'));
    assert.deepEqual(supported.find(p=>p.property==='turnOff').values,['Release control','Hold current color','Single color','Turn device off']);
    delete f.context.controller;
    assert.equal(f.run('ControllableParameters().find(p=>p.property==="turnOff").default'),'Hold current color');
});
check('stale off and restore selections warn and hold the latest color without unsupported operations',()=>{
    for(const selection of ['Turn device off','Release control']){
        const f=fixture(entry({capabilities:{rgb:true,addressable:false,brightness:false,power:false,restore:false}}));
        f.context.turnOff=selection;f.run('Initialize()');f.render();f.run('Shutdown()');
        const releases=f.writes.filter(w=>w.data.op==='strip_release');
        assert.equal(releases.length,1);assert.equal(releases[0].data.mode,'color');
        assert.deepEqual(releases[0].data.rgb,[10,20,30]);assert.equal(f.alerts.length,1);
        assert(f.sockets[0].closed);
    }
});
check('a profile without restore uses current RGB on failure and never submits restore',()=>{
    const f=fixture(entry({capabilities:{rgb:true,addressable:false,brightness:false,power:false,restore:false}}));
    f.run('Initialize()');f.render();f.render(2000);
    const releases=f.writes.filter(w=>w.data.op==='strip_release');
    assert.equal(releases.length,1);assert.equal(releases[0].data.mode,'color');
    assert.deepEqual(releases[0].data.rgb,[10,20,30]);assert.equal(f.alerts.length,1);
});
check('hold without any rendered color warns and sends no invented color or restoration',()=>{
    const f=fixture(entry({capabilities:{rgb:true,addressable:false,brightness:false,power:false,restore:false}}));
    delete f.context.turnOff;f.run('Initialize()');f.run('Shutdown()');
    assert.equal(f.writes.length,0);assert.equal(f.alerts.length,1);assert(f.sockets[0].closed);
});
check('QML companion uses an existing public discovery method and documents one-zone behavior',()=>{
    const qml=fs.readFileSync(path.join(root,'GoveeBluetoothOnly.qml'),'utf8');
    assert(qml.includes('import QtQuick'));assert(qml.includes('discovery.Refresh()'));
    assert(qml.includes('Une seule zone RGB'));assert(qml.includes('transitions normales'));
    assert(!/H6159|[0-9A-F]{2}:[0-9A-F]{2}:[0-9A-F]{2}/.test(source));
});
console.log(`${passed} tests passed`);
