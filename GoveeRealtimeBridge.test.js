// Optional H6008 transport. The companion bridge owns BLE authentication;
// this addon sends only device IDs and RGB values over local UDP.
const BRIDGE_ADDRESS = '127.0.0.1';
const BRIDGE_PORT = 47684;
const REPLY_TIMEOUT = 2000;
const ACQUIRE_TIMEOUT = 15000;
const HEARTBEAT_INTERVAL = 500;
const COLOR_INTERVAL = 100;
const RETRY_INTERVAL = 30000;

export default class GoveeRealtimeBridge
{
    constructor(owner)
    {
        this.owner = owner;
        this.enabled = false;
        this.phase = 'idle';
        this.sequence = 0;
        this.requests = {};
        this.lastStateReply = 0;
        this.lastReply = 0;
        this.lastSend = 0;
        this.lastColor = null;
        this.lastColorTime = -Infinity;
        this.lastHeartbeat = -Infinity;
        this.retryAt = 0;
        this.startupComplete = false;
        this.startupStarted = undefined;
        this.startupPowerRequested = false;
    }

    eligible()
    {
        return this.owner.sku === 'H6008' && this.owner.type === 3 &&
            typeof this.owner.id === 'string' && this.owner.id.length > 0 &&
            !!this.owner.udpServer;
    }

    setEnabled(value)
    {
        this.enabled = value === true || value === 'true';
    }

    ownsTransport()
    {
        return ['pending', 'streaming', 'paused', 'releasing'].includes(this.phase);
    }

    send(op, body, now)
    {
        const id = ++this.sequence;
        this.requests[id] = {op, time: now};
        for (const key of Object.keys(this.requests))
            if (now - this.requests[key].time > ACQUIRE_TIMEOUT) delete this.requests[key];
        this.owner.udpServer.write(Object.assign({id, op}, body), BRIDGE_ADDRESS, BRIDGE_PORT);
        this.lastSend = now;
        return id;
    }

    finishRelease(now)
    {
        this.phase = 'cooldown';
        this.retryAt = now + RETRY_INTERVAL;
        this.lastColor = null;
        this.requests = {};
        // LAN Forced mode must send its color again after a transport change.
        this.owner.lastSingleColor = '';
    }

    release(now, restore = false, finalRgb, replacePending = false)
    {
        if (!this.ownsTransport() || (this.phase === 'releasing' && !replacePending)) return;
        this.phase = 'releasing';
        this.releaseDeadline = now + REPLY_TIMEOUT;
        const body = {devices: [this.owner.id], restore};
        if (finalRgb) body.final_rgb = finalRgb.slice();
        this.activeReleaseId = this.send('release', body, now);
    }

    fail(now, reason)
    {
        this.owner.log('H6008 BLE bridge: ' + reason + '; releasing before LAN fallback');
        this.release(now, false);
    }

    prepareStartup(now)
    {
        if (this.startupComplete) return true;
        if (this.startupStarted === undefined)
        {
            this.startupStarted = now;
            if (!this.owner.hasReceivedStatus || now - this.owner.lastStatus > 10000)
                this.owner.getStatus(now);
        }
        if (!this.owner.waitingForStatusUpdate && this.owner.hasReceivedStatus)
        {
            if (this.owner.onOff === 1)
            {
                this.startupComplete = true;
                return true;
            }
            if (!this.startupPowerRequested)
            {
                // Match the existing renderer's initial power-on, once only.
                this.startupPowerRequested = true;
                this.owner.turnOn();
                this.owner.getStatus(now);
            }
        }
        if (now - this.startupStarted >= REPLY_TIMEOUT)
        {
            this.owner.log('H6008 BLE bridge: LAN startup status timeout; keeping LAN transport');
            this.startupStarted = undefined;
            this.finishRelease(now); // No BLE request was sent, so no release is needed.
        }
        return false;
    }

    // true means the bridge is acquiring/streaming/releasing and LAN must wait.
    render(color, now)
    {
        const wanted = this.enabled && this.eligible();
        if (!wanted && this.ownsTransport()) this.release(now, false);

        if (this.phase === 'releasing')
        {
            if (now < this.releaseDeadline) return true;
            // No renewals have been sent during this interval; the 2s lease
            // has expired even when the release reply was lost.
            this.finishRelease(now);
        }
        if (!wanted) return false;
        if (this.phase === 'cooldown')
        {
            if (now < this.retryAt) return false;
            this.phase = 'idle';
        }
        if (!Array.isArray(color) || color.length !== 3 ||
            color.some(value => !Number.isInteger(value) || value < 0 || value > 255)) return false;

        if (this.phase === 'idle' && !this.prepareStartup(now))
            return this.phase !== 'cooldown';

        if (this.phase === 'idle')
        {
            this.phase = 'pending';
            this.started = now;
            this.lastReply = now;
            this.lastColorTime = -Infinity;
            this.lastHeartbeat = now;
            this.lastColor = null;
        }
        else if (now - this.lastReply >= REPLY_TIMEOUT ||
                 (this.phase === 'pending' && now - this.started >= ACQUIRE_TIMEOUT))
        {
            this.fail(now, this.phase === 'pending' ? 'acquisition timeout' : 'reply timeout');
            return true;
        }

        const serialized = JSON.stringify(color);
        if (now - this.lastColorTime >= COLOR_INTERVAL && serialized !== this.lastColor)
        {
            this.send('colors', {colors: [{device: this.owner.id, rgb: color.slice(), on: true}]}, now);
            this.lastColor = serialized;
            this.lastColorTime = now;
        }
        if (now - this.lastHeartbeat >= HEARTBEAT_INTERVAL)
        {
            this.send('heartbeat', {devices: [this.owner.id]}, now);
            this.lastHeartbeat = now;
        }
        return true;
    }

    handleMessage(message, now)
    {
        if (!message || typeof message.address !== 'string' ||
            !['127.0.0.1', '::ffff:127.0.0.1'].includes(message.address)) return false;
        if (message.port !== undefined && Number(message.port) !== BRIDGE_PORT) return false;
        let reply;
        try { reply = JSON.parse(message.data); } catch (_) { return false; }
        if (!reply || !Object.prototype.hasOwnProperty.call(this.requests, reply.id)) return false;
        const request = this.requests[reply.id];
        delete this.requests[reply.id];
        const state = Array.isArray(reply.devices) ? reply.devices.find(item => item &&
            typeof item.device === 'string' && item.device.toUpperCase() === this.owner.id.toUpperCase()) : null;
        if (!state || typeof reply.ok !== 'boolean') return true;
        if (reply.id < this.lastStateReply) return true;
        this.lastStateReply = reply.id;
        if (request.op === 'release')
        {
            if (reply.id === this.activeReleaseId && this.phase === 'releasing' && reply.ok && !state.ready &&
                ['idle', 'failed'].includes(state.state)) this.finishRelease(now);
            return true;
        }
        if (!['pending', 'streaming', 'paused'].includes(this.phase)) return true;
        if (!reply.ok || state.state === 'failed' || (state.error &&
            !['connecting', 'initializing', 'reconnecting'].includes(state.state)))
        {
            this.fail(now, state.error || 'bridge rejected request');
            return true;
        }
        if (state.ready === true)
        {
            this.lastReply = now;
            this.phase = 'streaming';
        }
        else if (state.state === 'paused_off')
        {
            // An off bulb must not be turned on by the legacy LAN fallback.
            this.lastReply = now;
            this.phase = 'paused';
        }
        else if (['connecting', 'initializing', 'reconnecting'].includes(state.state))
        {
            if (this.phase !== 'pending') this.started = now;
            this.phase = 'pending';
            this.lastReply = now;
        }
        return true;
    }

    shutdown(mode, color, now)
    {
        if (!this.ownsTransport()) return false;
        // Shutdown policy takes precedence over a fallback release already in flight.
        if (mode === 'Release control') this.release(now, true, undefined, true);
        else if (mode === 'Single color') this.release(now, false, color, true);
        else this.release(now, false, undefined, true);
        return true;
    }
}
