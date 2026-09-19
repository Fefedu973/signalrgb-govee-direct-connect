import udp from "@SignalRGB/udp";
import goveeProducts from "./govee-products.test.js";
import GoveeDevice from "./GoveeDevice.test.js";
import GoveeController from "./GoveeController.test.js";
import GoveeDeviceUI from "./GoveeDeviceUI.test.js";

export function Name() { return "Govee Direct Connect"; }
export function Version() { return "2.2.1-h6008-ble"; }
export function Type() { return "network"; }
export function Publisher() { return "RickOfficial"; }
export function Size() { return [1, 1]; }
export function DefaultPosition() {return [0, 70]; }
export function DefaultScale(){return 1.0;}
export function DefaultComponentBrand() { return "Govee";}
export function ControllableParameters()
{
	return [
		{"property":"lightingMode", "group":"lighting", "label":"Lighting Mode", "type":"combobox", "values":["Canvas", "Forced", "Test Pattern"], "default":"Canvas"},
		{"property":"forcedColor", "group":"lighting", "label":"Forced Color", "min":"0", "max":"360", "type":"color", "default":"#009bde"},
		{"property":"turnOff", "group":"lighting", "label":"On shutdown", "type":"combobox", "values":["Release control", "Single color", "Turn device off"], "default":"Turn device off"},
        {"property":"shutDownColor", "group":"lighting", "label":"Shutdown Color", "min":"0", "max":"360", "type":"color", "default":"#8000FF"},
        {"property":"H6008Realtime", "group":"settings", "label":"H6008 BLE realtime bridge", "type":"boolean", "default":false},
        {"property":"frameDelay", "group":"settings", "label":"Delay between frames", "type":"combobox", "values":["0", "10", "50", "100"], "default":"0"}
	];
}

export function SubdeviceController() { return false; }

let goveeUI;
let lastRender = 0;

export function Initialize()
{
    device.log('Creating Govee Device UI');
	goveeUI = new GoveeDeviceUI(device, controller);
    const realtime = typeof H6008Realtime === 'undefined' ? undefined : H6008Realtime;
    if (goveeUI.goveeDevice.sku === 'H6008')
    {
        device.log('Govee ' + Version() + ' BLE enabled=' + (realtime === true || realtime === 'true') +
            ', value=' + String(realtime) + ', valueType=' + typeof realtime +
            ', eligible=' + goveeUI.goveeDevice.realtimeBridge.eligible() +
            ', sku=' + goveeUI.goveeDevice.sku + ', protocol=' + goveeUI.goveeDevice.type, {toFile:true});
    }
}

export function Render()
{
    let now = Date.now();
    goveeUI.render(lightingMode, forcedColor, now, frameDelay, typeof H6008Realtime !== 'undefined' && H6008Realtime);
}

export function Shutdown(SystemSuspending)
{
    device.log('Shutting down');
    goveeUI.shutDown(turnOff, shutDownColor);
}

export function Validate()
{
    return true;
}

export function DiscoveryService()
{
    service.log("You're running version " + Version());
    this.IconUrl = getGoveeLogo();

    this.lastPollTime = -5000;
    this.PollInterval = 5000;

    this.lastPort = null;
    
    // Disabled so we don't use the built in broadcasting
    // this.UdpBroadcastPort = 4003;
    // this.UdpListenPort = 4002;

    this.discoveredDeviceData = {};
    this.GoveeDeviceControllers = {};
    this.lastDiscoveryTime = 0;
    this.DiscoveryInterval = 60000;

    this.Initialize = function() {
        this.lastPort = service.getSetting('ipCache', 'lastUniquePort');
        if (!this.lastPort) this.getUniquePort();

        this.lastPollTime = Date.now();
        this.devicesLoaded = false;

        this.startSocketServer();
	}

    this.startSocketServer = function()
    {
        // Start the udp server
        if (!this.udpServer)
        {
            this.udpServer = udp.createSocket();
            this.udpServer.on('message', this.handleSocketMessage.bind(this));
            this.udpServer.on('error', this.handleSocketError.bind(this));
            service.log('Trying to bind UDP port 4002');
            this.udpServer.bind(4002);
        }
    }

    this.forceDiscover = function(ip, leds, type, split)
    {
        let goveeLightData = { 
            ip: ip,
            leds: parseInt(leds),
            type: parseInt(type),
            split: split,
            uniquePort: this.getUniquePort()
        };

        this.GoveeDeviceControllers[ip] = this.createController(goveeLightData);

        this.saveCache();
        this.Update(true);
    }

    this.loadForcedDevices = function()
    {
        // Load the cached ips
        let ipCacheJSON = service.getSetting('ipCache', 'cache');
        let ipCache = {};
        if (ipCacheJSON) ipCache = JSON.parse(ipCacheJSON);

        // Get all cached ips
        let cachedIps = Object.keys(ipCache);
        
        for(let cachedIp of cachedIps)
        {
            // If Controller is not yet created
            if (!this.GoveeDeviceControllers.hasOwnProperty(cachedIp))
            {
                // Create the controller and add it
                this.GoveeDeviceControllers[cachedIp] = this.createController(ipCache[cachedIp]);
            }

            let goveeController = this.GoveeDeviceControllers[cachedIp];
            
            if (!service.hasController(goveeController.id))
            {
                service.addController(goveeController);
                // Announce the controller as a device
                service.announceController(goveeController);
            } else
            {
                service.updateController(goveeController);
            }
        }

        this.devicesLoaded = true;
    }

    this.Update = function(force)
    {
        let diff = Date.now() - discovery.lastPollTime;

        if(diff > discovery.PollInterval || force === true)
        {
			discovery.lastPollTime = Date.now();

            if (!this.devicesLoaded || force === true)
            {
                this.loadForcedDevices();
            }

            // DHCP can change an accepted device's address. Only a scan with a
            // matching saved device identifier may move an existing controller.
            if (Date.now() - this.lastDiscoveryTime >= this.DiscoveryInterval)
            {
                this.lastDiscoveryTime = Date.now();
                this.udpServer.write({msg: {cmd: 'scan', data: {account_topic: 'reserve'}}}, '239.255.255.250', 4001);
            }
		}
    }

    this.handleSocketError = function(err, message)
    {
        service.log(message);
    }

    this.handleSocketMessage = function(value)
    {
        if (!value) return;
        const ip = this.getIPv4(value.address);
        if (!ip) return;

        let response;
        try { response = JSON.parse(value.data); }
        catch (err) { return; }
        if (!response || !response.msg || !response.msg.data) return;

        if (response.msg.cmd === 'scan')
        {
            const data = response.msg.data;
            if (typeof data.device !== 'string' || data.ip !== ip) return;
            const deviceId = data.device.toUpperCase();
            const existing = Object.values(this.GoveeDeviceControllers).filter(controller =>
                typeof controller.device.id === 'string' && controller.device.id.toUpperCase() === deviceId);

            // Do not add unknown devices, guess by SKU, or overwrite another
            // controller if the old address has been assigned to someone else.
            if (existing.length === 1)
            {
                const controller = existing[0];
                if (controller.device.sku && controller.device.sku !== data.sku) return;
                if (controller.device.ip !== ip)
                {
                    if (!this.updateControllerSettings(controller, controller.device.leds,
                        controller.device.type, controller.device.split, ip)) return;
                    service.log(`Recovered ${data.sku} after IP change to ${ip}`);
                }
            }

            const target = this.GoveeDeviceControllers[ip];
            if (target && target.device.id && target.device.id.toUpperCase() !== deviceId) return;
        }

        if (this.GoveeDeviceControllers.hasOwnProperty(ip))
        {
            let goveeController = this.GoveeDeviceControllers[ip];
            goveeController.relaySocketMessage(value, this);
        } else
        {
            service.log(`Cannot find controller for ${ip}`);
        }
	};

    this.getIPv4 = function(address)
    {
        if (typeof address !== 'string') return null;
        const ipv4Pattern = /(\b25[0-5]|\b2[0-4][0-9]|\b[01]?[0-9][0-9]?)\.(\b25[0-5]|\b2[0-4][0-9]|\b[01]?[0-9][0-9]?)\.(\b25[0-5]|\b2[0-4][0-9]|\b[01]?[0-9][0-9]?)\.(\b25[0-5]|\b2[0-4][0-9]|\b[01]?[0-9][0-9]?)/;
        const match = address.match(ipv4Pattern);
        return match ? match[0] : null;
    }

    this.Delete = function(ip)
    {
        service.log('Deleting controller ' + ip);
        this.removeController(ip);
        this.saveCache();
        this.Update(true);
    }

    this.changeIp = function(oldIp, newIp)
    {
        if (this.GoveeDeviceControllers.hasOwnProperty(oldIp))
        {
            const controller = this.GoveeDeviceControllers[oldIp];
            return this.updateControllerSettings(controller, controller.device.leds,
                controller.device.type, controller.device.split, newIp);
        }
        return false;
    }

    this.updateControllerSettings = function(controller, leds, type, split, ip)
    {
        if (this.getIPv4(ip) !== ip) return false;
        if (!Number.isInteger(Number(leds)) || Number(leds) < 1 ||
            ![1,2,3,4,5].includes(Number(type)) || ![1,2,3,4].includes(Number(split))) return false;
        const oldIp = controller.device.ip;
        const occupied = this.GoveeDeviceControllers[ip];
        if (occupied && occupied !== controller)
        {
            service.log(`Cannot move ${oldIp} to ${ip}: another saved controller uses that address`);
            return false;
        }
        if (this.GoveeDeviceControllers[oldIp] !== controller) return false;

        // Remove by the OLD service identifier before changing either index.
        service.removeController(controller);
        delete this.GoveeDeviceControllers[oldIp];
        if (controller.udpSocket) controller.udpSocket.close();

        const data = controller.device.toCacheJSON();
        data.sku = controller.device.sku;
        data.bleVersionSoft = controller.device.firmware;
        // Keep the endpoint identifier so enablement, Canvas position and other
        // SignalRGB device settings remain attached across DHCP address changes.
        data.controllerId = controller.id;
        data.ip = ip;
        data.leds = parseInt(leds);
        data.type = parseInt(type);
        data.split = parseInt(split);
        // A new local relay port avoids racing the old renderer's socket close.
        data.uniquePort = this.getUniquePort();
        if (data.name === controller.device.generateName()) delete data.name;
        const device = new GoveeDevice(data);
        device.save();
        const replacement = this.createController(device.toCacheJSON());
        this.GoveeDeviceControllers[ip] = replacement;
        this.saveCache();
        service.addController(replacement);
        service.announceController(replacement);
        return true;
    }

    this.saveCache = function()
    {
        let ipCache = {};
        for(let ip of Object.keys(this.GoveeDeviceControllers))
        {
            let goveeController = this.GoveeDeviceControllers[ip];
            ipCache[goveeController.device.ip] = goveeController.toCacheJSON();
        }

        service.saveSetting('ipCache', 'cache', JSON.stringify(ipCache));
    }

    this.removeController = function(ip)
    {
        // The UI passes the stable controller ID, which can be a former IP.
        let goveeController = this.GoveeDeviceControllers[ip] ||
            Object.values(this.GoveeDeviceControllers).find(controller => controller.id === ip);
        if (!goveeController) return;
        service.removeController(goveeController);
        if (goveeController.udpSocket) goveeController.udpSocket.close();
        delete this.GoveeDeviceControllers[goveeController.device.ip];
    }

    this.getUniquePort = function()
    {
        
        if (!this.lastPort || this.lastPort < 46920)
        {
            this.lastPort = 46920;
        } else
        {
            this.lastPort++;
        }

        service.log('Assigning unique port ' + this.lastPort)
        // Save the new port:
        service.saveSetting('ipCache', 'lastUniquePort', this.lastPort);
        return this.lastPort;
    }

    this.createController = function(cacheData)
    {
        service.log('Creating controller: ' + cacheData.ip);
        
        let goveeDevice;

        if (cacheData.id)
        {
            goveeDevice = (new GoveeDevice).load(cacheData.id);

            // Add this for devices with the old settings
            if (!goveeDevice.uniquePort)
            {
                goveeDevice.uniquePort = this.getUniquePort();
                goveeDevice.save();
            }
        } else
        {
            goveeDevice = new GoveeDevice(cacheData);
        }

        // Create and store controller for network tab
        let goveeController = new GoveeController(goveeDevice);
        // A callback avoids putting a circular discovery object in controller data.
        goveeController.applySettings = (leds, type, split, ip) =>
            this.updateControllerSettings(goveeController, leds, type, split, ip);

        // Start the udp socket?
        goveeController.setupUDPSocket();
        return goveeController;
    }

    this.updatedController = function(goveeController)
    {
        service.log(`Controller ${goveeController.id} data updated`);
        this.saveCache();
        service.removeController(goveeController);
        service.addController(goveeController);
        service.log('Re-announcing the controller');
        service.announceController(goveeController);

        service.log('Restart our socket server');
        this.startSocketServer();

    }
}

function getGoveeLogo()
{
    return goveeProducts['default'].base64Image;
}


