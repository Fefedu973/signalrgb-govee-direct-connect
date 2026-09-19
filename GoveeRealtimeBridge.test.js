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
    constructor(owner, host)
    {
        this.owner = owner;
        this.host = host;
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
        this.counters = {colorRequests: 0, readyReplies: 0, failures: 0, retries: 0};
        this.lastError = '';
        this.alertId = undefined;
        this.lastStatusTime = -Infinity;
        this.lastStatusPhase = undefined;
    }

    supported()
    {
        return this.owner.sku === 'H6008' && this.owner.type === 3;
    }

    eligible()
    {
        return this.supported() &&
            typeof this.owner.id === 'string' && this.owner.id.length > 0 &&
            !!this.owner.udpServer;
    }

    setEnabled(value)
    {
        const enabled = value === true || value === 'true';
        if (enabled !== this.enabled)
        {
            if (!enabled) this.clearFailure();
            else if (this.phase === 'cooldown') this.phase = 'idle';
        }
        this.enabled = enabled;
    }

    clearFailure()
    {
        this.lastError = '';
        if (this.alertId !== undefined && this.host && typeof this.host.denotify === 'function')
            this.host.denotify(this.alertId);
        this.alertId = undefined;
    }

    reportFailure(reason)
    {
        this.counters.failures++;
        this.lastError = String(reason).slice(0, 160);
        const message = 'H6008 BLE bridge: ' + this.lastError + '; holding color, LAN color fallback disabled';
        if (this.host) this.host.log(message, {toFile: true});
        else this.owner.log(message);
        if (this.alertId === undefined && this.host && typeof this.host.notify === 'function')
            this.alertId = this.host.notify('H6008 BLE transport unavailable',
                'Colors are paused to avoid LAN fading. Check the local BLE bridge; automatic retry every 30 seconds. Disable H6008 BLE realtime bridge to use LAN colors.', 1);
    }

    publishStatus(now)
    {
        if (!this.host || !this.supported()) return;
        const phase = this.enabled ? this.phase : (this.phase === 'releasing' ? 'releasing' : 'LAN');
        const changed = phase !== this.lastStatusPhase;
        if (!changed && now - this.lastStatusTime < 1000) return;
        const counts = 'requests=' + this.counters.colorRequests + ', readyReplies=' + this.counters.readyReplies +
            ', failures=' + this.counters.failures + ', retries=' + this.counters.retries;
        if (changed) this.host.log('H6008 transport=' + phase + ', ' + counts, {toFile: true});
        if (typeof this.host.addMessage === 'function')
            this.host.addMessage('h6008-ble-transport', 'H6008 transport: ' + phase + ' | ' + counts,
                (this.lastError ? this.lastError + '. ' : '') +
                'Counts are local bridge requests/replies, not measured BLE or optical FPS. LAN colors remain blocked while BLE realtime is enabled.');
        this.lastStatusTime = now;
        this.lastStatusPhase = phase;
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
        try { this.owner.udpServer.write(Object.assign({id, op}, body), BRIDGE_ADDRESS, BRIDGE_PORT); }
        catch (_) { delete this.requests[id]; return null; }
        this.lastSend = now;
        if (op === 'colors') this.counters.colorRequests++;
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
        this.reportFailure(reason);
        if (this.ownsTransport()) this.release(now, false);
        else this.finishRelease(now);
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
            this.reportFailure('LAN startup status timeout');
            this.startupStarted = undefined;
            this.finishRelease(now); // No BLE request was sent, so no release is needed.
        }
        return false;
    }

    // While opted in, true also covers outages: never silently resume fading LAN colors.
    render(color, now)
    {
        const handled = this.renderFrame(color, now);
        this.publishStatus(now);
        return handled;
    }

    renderFrame(color, now)
    {
        const wanted = this.enabled && this.supported();
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
            if (now < this.retryAt) return true;
            this.counters.retries++;
            this.phase = 'idle';
        }
        if (!this.eligible())
        {
            this.fail(now, 'device ID or local UDP socket unavailable');
            return true;
        }
        if (!Array.isArray(color) || color.length !== 3 ||
            color.some(value => !Number.isInteger(value) || value < 0 || value > 255))
        {
            this.fail(now, 'invalid RGB frame');
            return true;
        }

        if (this.phase === 'idle' && !this.prepareStartup(now))
            return true;

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
            if (this.send('colors', {colors: [{device: this.owner.id, rgb: color.slice(), on: true}]}, now) === null)
            {
                this.fail(now, 'local UDP send failed');
                return true;
            }
            this.lastColor = serialized;
            this.lastColorTime = now;
        }
        if (now - this.lastHeartbeat >= HEARTBEAT_INTERVAL)
        {
            if (this.send('heartbeat', {devices: [this.owner.id]}, now) === null)
            {
                this.fail(now, 'local UDP send failed');
                return true;
            }
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
            this.clearFailure();
            this.counters.readyReplies++;
            this.lastReply = now;
            this.phase = 'streaming';
        }
        else if (state.state === 'paused_off')
        {
            this.clearFailure();
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
        // Shutdown policy takes precedence over an outage release already in flight.
        if (mode === 'Release control') this.release(now, true, undefined, true);
        else if (mode === 'Single color') this.release(now, false, color, true);
        else this.release(now, false, undefined, true);
        return true;
    }
}
