#!/usr/bin/env python3
"""
Runs the dashboard service against SIMULATED emsfp devices, for trying the
guided setup (/guide) without hardware.

    python tools/fake_fleet.py [--port 8190]

The device HTTP calls inside the service are replaced in-process, so no
network is touched and nothing real can be written. The fake models the
endpoints the catalog knows (shapes are derived from config_catalog.py - they
show that the tool's plumbing works, NOT that a real unit accepts the same
writes). It starts with one factory-state Fusion:

    MAC 40:a3:6b:a2:16:14   ->  media ports 10.162.22.20, OOB 11.162.22.20

Writing a new address to it moves it, like the real unit does: the old
address stops answering and the new Red/Blue addresses start to.
State lives in memory only; the service files (devices.json, guide-state.json,
...) are written next to server.py as usual - delete them after a test.
"""
import copy
import json
import os
import re
import sys
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICE = os.path.join(os.path.dirname(HERE), "fusion-dashboard-service")
sys.path.insert(0, SERVICE)

import config_catalog as cat   # noqa: E402
import config_tool as ct       # noqa: E402

REGISTRY = {}   # ip -> FakeDevice


class FakeError(Exception):
    pass


def _default(s):
    t = s["type"]
    if t == "bool01":
        return "0"
    if t == "bool":
        return False
    if t == "int":
        return 0
    if t == "ip":
        return "0.0.0.0"
    if t == "enum":
        opts = s.get("options") or []
        first = opts[0] if opts else ""
        return first[0] if isinstance(first, (list, tuple)) else (first.get("value") if isinstance(first, dict) else first)
    return ""


def _set(obj, path, value):
    cur = obj
    for k in path[:-1]:
        if isinstance(k, int):
            continue
        cur = cur.setdefault(k, {})
    last = path[-1]
    if not isinstance(last, int):
        cur[last] = value


def _merge(dst, src):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        else:
            dst[k] = v


def _uuid(seed):
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))


class FakeDevice:
    def __init__(self, mac, factory_ip, base_type="FusioN6", channels=6):
        self.mac = mac
        b = [int(x, 16) for x in mac.split(":")][3:]
        self.ips = {factory_ip}
        self.oob = "11.%d.%d.%d" % tuple(b)       # the OOB port answers on its own address, always
        self.ep = {}
        self.channels = channels
        self.link = {"e1": "up", "e2": "up"}
        e = self.ep
        e["self/information"] = {"type": "22 - ST2110 UHD Transceiver", "base_type": base_type,
                                 "serial_number": "1250722%05d" % (b[0] * 256 + b[1] + b[2])}
        e["self/firmware"] = {"info": [{"active": "yes", "semantic_version": "4.8.4"}, {"active": "no", "semantic_version": "4.8.0"}]}
        e["self/license"] = {"clean_switch": "yes", "uhd_support": "yes"}
        e["self/ipconfig"] = {"hostname": "emsfp-%02x-%02x-%02x" % tuple(b)}
        e["self/interfaces"] = {
            "e1": {"static_ip": factory_ip + "/24", "static_gateway": "0.0.0.0", "current_ip": factory_ip + "/24",
                   "mac_address": mac, "dhcp": True, "vlan": 0},
            "e2": {"static_ip": "0.0.0.0/0", "static_gateway": "0.0.0.0", "current_ip": "0.0.0.0/0",
                   "mac_address": mac[:-2] + "%02x" % (int(mac[-2:], 16) + 1), "dhcp": True, "vlan": 0},
            "oob": {"static_ip": "11.%d.%d.%d/8" % tuple(b), "static_gateway": "0.0.0.0", "dhcp": True,
                    "mac_address": mac[:-2] + "%02x" % (int(mac[-2:], 16) + 2)},
        }
        e["self/system"] = {"uptime": "0 days, 00:10:00"}
        e["telemetry/node"] = {"health": {"core_temp": 58}, "refclk": {"status": "unlocked"},
                               "interfaces": {"e1": {"link_status": "up"}, "e2": {"link_status": "up"}}}
        e["self/diag/nmos"] = {"status": "disconnected"}
        e["lldp"] = {"configuration": {"rate": "45", "untag": True, "enable_rx": "1"},
                     "neighbor": [{"interface": 1, "chassis": "c4:ca:2b:ff:eb:7a", "port": "Ethernet3/17/3", "ttl": "120"},
                                  {"interface": 2, "chassis": "c4:ca:2b:ff:eb:62", "port": "Ethernet3/17/3", "ttl": "120"},
                                  {"interface": 3, "chassis": "", "port": "", "ttl": "0"}]}
        for s in cat.CATALOG:                       # every device-scope setting the catalog knows
            if s["scope"] == cat.DEVICE and s["endpoint"] and s["endpoint"] != "self/interfaces":
                d = e.setdefault(s["endpoint"], {})
                if not _has(d, s["field"]):
                    _set(d, s["field"], _default(s))
        e["self/syslog"]["config"].update({"server": "192.168.0.0", "port": 514, "enable": False})
        self.sdi_ids = ["b%dd2da17-360a-4396-80a9-f620c4bb3885" % n for n in (0, 1, 2, 3, 5, 7)][:channels]
        for sid in self.sdi_ids:
            d = {"label": ""}
            for s in cat.CATALOG:
                if s["scope"] == cat.SDI_OUTPUT:
                    _set(d, s["field"], _default(s))
            e["sdi_output/" + sid] = d
        e["refclk"] = {"uuid": [_uuid(mac + "ptp")]}
        e["refclk/" + e["refclk"]["uuid"][0]] = {"domain_num": 0, "vlan_id": 0, "dscp": 0}
        tele = []
        for ch in range(1, channels + 1):
            engines = []
            order = ["video", "audio", "audio", "audio", "audio", "data"]
            for n, ess in enumerate(order):
                flows = []
                for leg, nm in (("primary", "pri"), ("secondary", "sec")):
                    fid = _uuid(f"{mac}-{ch}-{n}-{nm}")
                    flows.append({"flow": fid, "type": leg})
                    f = {"name": f"rx ch{ch} flow {n} {nm}", "network": {}}
                    for s in cat.CATALOG:
                        if s["scope"] == cat.FLOW:
                            _set(f, s["field"], _default(s))
                    e["flows/" + fid] = f
                engines.append({"essence": ess, "flows": flows})
            tele.append({"channel": ch, "type": "decapsulator", "engines": engines})
        e["telemetry/devices"] = {"devices": tele}
        e["telemetry/warnings/"] = []
        REGISTRY[factory_ip] = REGISTRY[self.oob] = self

    # -- network behaviour
    def _readdress(self):
        e1 = self.ep["self/interfaces"]["e1"]["static_ip"].split("/")[0]
        e2 = self.ep["self/interfaces"]["e2"]["static_ip"].split("/")[0]
        for ip in list(self.ips):
            REGISTRY.pop(ip, None)
        self.ips = {e1, e2}
        for ip in self.ips:
            REGISTRY[ip] = self
        i = self.ep["self/interfaces"]
        i["e1"]["current_ip"], i["e2"]["current_ip"] = i["e1"]["static_ip"], i["e2"]["static_ip"]
        self.ep["telemetry/node"]["refclk"]["status"] = "locked"
        self.ep["self/diag/nmos"]["status"] = "connected"

    def get(self, path):
        p = path.strip("/")
        if p == "":
            return ["self/", "flows/", "sdi_output/", "refclk/", "lldp/", "telemetry/", "devices/"]
        if p == "flows":
            return [k.split("/", 1)[1] + "/" for k in self.ep if k.startswith("flows/")]
        if p == "sdi_output":
            return [i + "/" for i in self.sdi_ids]
        if p == "devices":
            return []
        if p == "telemetry/warnings":
            return self.ep["telemetry/warnings/"]
        if p.startswith("telemetry/warnings/"):
            return {"warning": []}
        if p in self.ep:
            return copy.deepcopy(self.ep[p])
        raise FakeError("HTTP Error 404: Not Found")

    def post(self, path, body):
        p = path.strip("/")
        if p not in self.ep:
            raise FakeError("HTTP Error 404: Not Found")
        before = self.ep["self/interfaces"]["e1"]["static_ip"]
        _merge(self.ep[p], body)
        if p == "self/interfaces" and self.ep["self/interfaces"]["e1"]["static_ip"] != before:
            self._readdress()
        return 200, "ok"


def _has(d, path):
    cur = d
    for k in path:
        if isinstance(k, int):
            continue
        if not isinstance(cur, dict) or k not in cur:
            return False
        cur = cur[k]
    return True


def _device(ip):
    host = ip.split(":")[0]
    if host not in REGISTRY:
        raise FakeError("<urlopen error timed out>")
    return REGISTRY[host]


def fake_get(ip, path):
    return _device(ip).get(path)


def fake_post(ip, path, payload):
    return _device(ip).post(path, payload)


def install():
    """Route the service's device calls to the fake devices."""
    import server
    ct.http_get, ct.http_post = fake_get, fake_post

    def get_json(ip, path, timeout):
        return fake_get(ip, re.sub(r"^/emsfp/node/v1/", "", path))
    server.http_get_json = get_json
    server.http_post = lambda ip, path, payload, timeout=4: fake_post(ip, re.sub(r"^/emsfp/node/v1/", "", path), payload)
    return server


def main():
    port = 8190
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    import shutil
    import tempfile
    from pathlib import Path
    import guide_tool
    tmp = Path(tempfile.mkdtemp(prefix="fake-fleet-"))      # all service files of this run live here
    (tmp / "profiles").mkdir()
    shutil.copy(os.path.join(SERVICE, "config-profiles", "site-default.example.json"), tmp / "profiles")
    FakeDevice("40:a3:6b:a2:16:14", "10.162.22.20")
    server = install()
    server.PORT, server.HOST = port, "127.0.0.1"
    server.DEVICES_FILE = tmp / "devices.json"
    server.DEVICES_EXAMPLE_FILE = tmp / "none.json"
    server.SEEDED_FILE = tmp / "seeded.json"
    server.DEVICES_FILE.write_text("[]", encoding="utf-8")
    guide_tool.STATE_FILE, guide_tool.SITE_FILE = tmp / "guide-state.json", tmp / "guide-site.json"
    ct.AUTH_FILE, ct.PROFILES_DIR, ct.BACKUPS_DIR = tmp / "auth.json", tmp / "profiles", tmp / "backups"
    print("Service files for this run: %s" % tmp)
    print("Fake fleet: factory Fusion MAC 40:a3:6b:a2:16:14 at 10.162.22.20 (media) / 11.162.22.20 (OOB)")
    print("Open http://127.0.0.1:%d/guide" % port)
    server.main()


if __name__ == "__main__":
    main()
