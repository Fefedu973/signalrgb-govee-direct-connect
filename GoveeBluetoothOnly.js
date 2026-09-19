import udp from "@SignalRGB/udp";

// Device identities and protocol profiles come only from the allowlisted local
// bridge catalogue. This file contains no Bluetooth addresses or pairing keys.
const ADDRESS = "127.0.0.1", PORT = 47684;
const COLOR_MS = 100, HEARTBEAT_MS = 500, REPLY_MS = 2000;
const ACQUIRE_MS = 15000, RETRY_MS = 30000, CATALOG_MS = 5000;
export function Name() { return "Govee Bluetooth Only"; }
export function Version() { return "1.0.0"; }
export function Type() { return "network"; }
export function Publisher() { return "Fefedu973"; }
export function Size() { return [1, 1]; }
export function DefaultPosition() { return [0, 70]; }
export function DefaultScale() { return 1; }
export function SubdeviceController() { return false; }
export function ControllableParameters() {
    const capabilities = typeof controller !== "undefined" && controller && controller.capabilities || {};
    const shutdown = ["Hold current color", "Single color"];
    if (capabilities.restore === true) shutdown.unshift("Release control");
    if (capabilities.power === true) shutdown.push("Turn device off");
    const parameters = [
        {property:"lightingMode", group:"lighting", label:"Lighting Mode", type:"combobox", values:["Canvas","Forced"], default:"Canvas"},
        {property:"forcedColor", group:"lighting", label:"Forced Color", type:"color", default:"#009bde"}
    ];
    if (capabilities.brightness === true) parameters.push(
        {property:"stripBrightness", group:"lighting", label:"Brightness", type:"number", min:0, max:100, step:1, default:100});
    parameters.push(
        {property:"turnOff", group:"lighting", label:"On shutdown", type:"combobox", values:shutdown, default:shutdown[0]},
        {property:"shutDownColor", group:"lighting", label:"Shutdown Color", type:"color", default:"#8000ff"}
    );
    return parameters;
}

function supported(entry) {
    const c = entry && entry.capabilities;
    return !!entry && typeof entry.device === "string" && entry.device.length > 0 && entry.device.length <= 100 &&
        typeof entry.profile === "string" && entry.profile.length > 0 && entry.profile.length <= 80 &&
        typeof entry.model === "string" && entry.model.length > 0 && entry.model.length <= 40 &&
        typeof entry.name === "string" && entry.name.length > 0 && entry.name.length <= 80 &&
        entry.transport === "ble" && entry.leds === 1 && !!c && c.rgb === true && c.addressable === false;
}
function packet(value) {
    if (!value || (value.address !== ADDRESS && value.address !== "::ffff:127.0.0.1")) return null;
    if (value.port !== undefined && Number(value.port) !== PORT) return null;
    if (typeof value.data !== "string" || value.data.length > 16384) return null;
    try { const data = JSON.parse(value.data); return data && typeof data === "object" ? data : null; }
    catch (_) { return null; }
}
function hexColor(value) {
    const match = /^#?([\da-f]{2})([\da-f]{2})([\da-f]{2})$/i.exec(String(value));
    return match ? match.slice(1).map(x => parseInt(x, 16)) : [0, 0, 0];
}
function rgbColor(value) {
    if (!value || value.length !== 3) return null;
    const rgb = Array.from(value, x => Math.round(Number(x)));
    return rgb.every(x => Number.isFinite(x) && x >= 0 && x <= 255) ? rgb : null;
}

class BleClient {
    constructor(entry, host) {
        this.entry = entry; this.host = host; this.socket = null;
        this.sequence = 0; this.prefix = "ble-" + Date.now() + "-";
        this.pending = {}; this.colorPending = null; this.latest = null;
        this.lastColor = null; this.lastColorTime = -Infinity;
        this.lastHeartbeat = -Infinity; this.lastStatus = "";
        this.startedAt = null; this.phase = "idle"; this.retryAt = 0;
        this.alert = undefined; this.closed = false;
        this.open();
    }
    open() {
        try {
            this.socket = udp.createSocket();
            this.socket.on("message", value => this.receive(value));
            this.socket.on("error", () => this.fail("Local UDP socket unavailable", Date.now()));
            this.socket.bind(0);
        } catch (_) { this.fail("Local UDP socket unavailable", Date.now()); }
    }
    status(state, detail) {
        if (state === this.lastStatus) return;
        this.lastStatus = state;
        this.host.log("Govee Bluetooth transport: " + state, {toFile:true});
        if (typeof this.host.addMessage === "function") this.host.addMessage(
            "govee-bluetooth-only", "Bluetooth: " + state,
            detail || "One RGB zone; ordinary device transitions remain active. Local bridge required.");
    }
    send(op, body, now) {
        if (!this.socket || this.closed) return null;
        const id = this.prefix + (++this.sequence);
        this.pending[id] = {op, time:now};
        try { this.socket.write(JSON.stringify(Object.assign({id, op}, body)), ADDRESS, PORT); }
        catch (_) { delete this.pending[id]; return null; }
        return id;
    }
    fail(reason, now) {
        if (this.closed || this.phase === "cooldown") return;
        this.phase = "cooldown"; this.retryAt = now + RETRY_MS;
        // No LAN fallback. Release is best effort; the bridge also owns a lease.
        const release = this.entry.capabilities.restore === true ?
            {device:this.entry.device, mode:"restore"} : this.holdCurrentColor();
        if (release) this.send("strip_release", release, now);
        this.pending = {}; this.colorPending = null; this.lastColor = null;
        if (this.socket) { try { this.socket.close(); } catch (_) {} this.socket = null; }
        this.status("unavailable", "Colors paused. Check the local bridge; automatic retry in 30 seconds. " + reason);
        if (this.alert === undefined && typeof this.host.notify === "function")
            this.alert = this.host.notify("Govee Bluetooth unavailable", "Colors paused. Check the local Bluetooth bridge. Automatic retry in 30 seconds.", 1);
    }
    receive(value) {
        const response = packet(value);
        if (!response || !Object.prototype.hasOwnProperty.call(this.pending, response.id)) return;
        const request = this.pending[response.id];
        if (request.op === "strip_release") { delete this.pending[response.id]; return; }
        const state = Array.isArray(response.devices) ? response.devices.find(x => x && x.device === this.entry.device) : null;
        if (response.ok !== true) { this.fail("Bridge rejected the request", Date.now()); return; }
        if (!state || typeof state.state !== "string") return;
        delete this.pending[response.id];
        if (this.colorPending === response.id) this.colorPending = null;
        const paused = state.state === "paused_off";
        const acquiring = ["connecting", "initializing", "reconnecting"].includes(state.state);
        // A retryable BLE error belongs to the bridge's bounded recovery window.
        // An externally powered-off device is a healthy paused lease, not failed
        // acquisition: automatic release/restore could turn it back on.
        if (state.state === "failed" || state.restore_error || (state.error && !acquiring && !paused)) {
            this.fail("Device reported a Bluetooth error", Date.now()); return;
        }
        if (paused || state.ready === true) {
            this.phase = paused ? "paused_off" : "streaming";
            this.startedAt = null;
            if (this.alert !== undefined && typeof this.host.denotify === "function") this.host.denotify(this.alert);
            this.alert = undefined;
        } else {
            this.phase = state.state;
            if (this.startedAt === null) this.startedAt = Date.now();
            // A restarted bridge has forgotten its lease and needs a color request.
            if (state.state === "idle") this.lastColor = null;
        }
        this.status(this.phase);
    }
    render(rgb, brightness, now) {
        if (this.closed) return;
        const color = rgbColor(rgb);
        if (!color) return;
        const level = Math.max(0, Math.min(100, Math.round(Number(brightness))));
        if (!Number.isFinite(level)) return;
        this.latest = {rgb:color, brightness:level};
        if (this.phase === "cooldown") {
            if (now < this.retryAt) return;
            this.phase = "idle"; this.startedAt = null; this.open();
            if (!this.socket) return;
        }
        for (const id of Object.keys(this.pending)) {
            if (now - this.pending[id].time >= REPLY_MS) { this.fail("Local bridge did not reply", now); return; }
        }
        if (this.startedAt !== null && now - this.startedAt >= ACQUIRE_MS) {
            this.fail("Bluetooth acquisition timed out", now); return;
        }
        const colorKey = color.join(",") + "/" + level;
        if (!this.colorPending && now - this.lastColorTime >= COLOR_MS && colorKey !== this.lastColor) {
            const body = {device:this.entry.device, rgb:color};
            if (this.entry.capabilities.brightness === true) body.brightness = level;
            if (this.entry.capabilities.power === true) body.on = true;
            // The bridge powers on only at acquisition, never for every RGB frame.
            const id = this.send("strip_colors", body, now);
            if (id === null) { this.fail("Local UDP write failed", now); return; }
            this.colorPending = id; this.lastColor = colorKey; this.lastColorTime = now;
            if (this.startedAt === null && this.phase !== "streaming" && this.phase !== "paused_off") this.startedAt = now;
        }
        if (now - this.lastHeartbeat >= HEARTBEAT_MS && this.lastColor !== null) {
            if (this.send("heartbeat", {devices:[this.entry.device]}, now) === null) this.fail("Local UDP write failed", now);
            this.lastHeartbeat = now;
        }
    }
    shutdown(setting, color) {
        if (this.closed) return;
        let body = null, warning = "";
        if (setting === "Hold current color") body = this.holdCurrentColor();
        else {
            const mode = setting === "Single color" ? "color" : setting === "Turn device off" ? "off" : setting === "Release control" ? "restore" : null;
            const capability = mode === "off" ? "power" : mode === "restore" ? "restore" : "rgb";
            if (mode !== null && this.entry.capabilities[capability] === true) {
                body = {device:this.entry.device, mode};
                if (mode === "color") body.rgb = hexColor(color);
            } else {
                body = this.holdCurrentColor();
                warning = "The saved shutdown action is unsupported by this profile. " +
                    (body ? "Holding the latest Canvas/Forced color instead." : "No color is available; the bridge lease will expire.");
            }
        }
        if (!body && !warning) warning = "No Canvas/Forced color is available; no shutdown color was invented. The bridge lease will expire.";
        if (warning) {
            this.status("unsupported shutdown action", warning);
            if (typeof this.host.notify === "function") this.host.notify("Govee Bluetooth shutdown", warning, 1);
        }
        if (body) {
            // An explicit shutdown must be sent even while the render path is paused.
            if (!this.socket) this.open();
            this.send("strip_release", body, Date.now());
        }
        this.closed = true;
        if (this.socket) { try { this.socket.close(); } catch (_) {} this.socket = null; }
    }
    holdCurrentColor() {
        return this.latest && this.entry.capabilities.rgb === true ?
            {device:this.entry.device, mode:"color", rgb:this.latest.rgb.slice()} : null;
    }
}

let client = null;
export function Validate() {
    return typeof controller !== "undefined" && supported(controller) && controller.id === "govee-ble:" + controller.device;
}
export function Initialize() {
    if (!Validate()) { device.log("Govee Bluetooth: unsupported or missing catalogue profile", {toFile:true}); return; }
    device.setName(controller.name); device.setSize([1, 1]);
    device.setControllableLeds([controller.name], [[0, 0]]);
    client = new BleClient(controller, device);
}
export function Render() {
    if (!client) return;
    const color = typeof lightingMode !== "undefined" && lightingMode === "Forced" ?
        hexColor(typeof forcedColor === "undefined" ? "#009bde" : forcedColor) : device.color(0, 0);
    client.render(color, typeof stripBrightness === "undefined" ? 100 : stripBrightness, Date.now());
}
export function Shutdown() {
    if (client) client.shutdown(typeof turnOff === "undefined" ?
        (client.entry.capabilities.restore === true ? "Release control" : "Hold current color") : turnOff,
        typeof shutDownColor === "undefined" ? "#8000ff" : shutDownColor);
    client = null;
}

export function DiscoveryService() {
    this.PollInterval = CATALOG_MS; this.lastPoll = -Infinity;
    this.sequence = 0; this.prefix = "catalog-" + Date.now() + "-";
    this.socket = null; this.pending = null; this.known = {};
    this.open = function() {
        if (this.socket) return;
        this.socket = udp.createSocket();
        this.socket.on("message", value => this.receive(value));
        this.socket.on("error", () => {
            service.log("Govee Bluetooth catalogue: local UDP unavailable");
            if (this.socket) { try { this.socket.close(); } catch (_) {} this.socket = null; }
        });
        this.socket.bind(0);
    };
    this.Refresh = function() {
        this.lastPoll = Date.now();
        try {
            this.open();
            const id = this.prefix + (++this.sequence);
            this.pending = id;
            this.socket.write(JSON.stringify({id, op:"catalog"}), ADDRESS, PORT);
        } catch (_) { service.log("Govee Bluetooth catalogue: local bridge unavailable"); }
    };
    this.receive = function(value) {
        const response = packet(value);
        if (!response || response.id !== this.pending || response.ok !== true || !Array.isArray(response.devices) || response.devices.length > 16) return;
        this.pending = null;
        const seen = {};
        for (const entry of response.devices) {
            if (!supported(entry)) continue;
            const id = "govee-ble:" + entry.device;
            if (seen[id]) continue;
            seen[id] = true;
            const item = {id, device:entry.device, profile:entry.profile, model:entry.model,
                name:entry.name, transport:"ble", leds:1,
                capabilities:{rgb:true, addressable:false, brightness:entry.capabilities.brightness === true,
                    power:entry.capabilities.power === true, restore:entry.capabilities.restore === true}};
            const existed = service.hasController(id);
            if (!existed) service.addController(item); else service.updateController(item);
            if (!existed || !this.known[id]) service.announceController(item);
            this.known[id] = item;
        }
        // An authoritative valid catalogue can revoke a profile; a missing reply cannot.
        for (const id of Object.keys(this.known)) if (!seen[id]) {
            service.removeController(this.known[id]); delete this.known[id];
        }
    };
    this.Initialize = function() { this.Refresh(); };
    this.Update = function() { if (Date.now() - this.lastPoll >= CATALOG_MS) this.Refresh(); };
    this.Shutdown = function() { if (this.socket) this.socket.close(); this.socket = null; };
}
