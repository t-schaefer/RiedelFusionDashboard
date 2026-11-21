"""
Demo mode: the whole service against SIMULATED emsfp devices.

    python server.py --demo [--port 8190] [--host 127.0.0.1]

Nothing real is touched: the device calls inside the service are replaced
in-process and all data files of the demo live in demo-data/ (wiped at every
start). The demo shows three devices:

  * one in FACTORY state, to run through the guided setup (/guide):
        MAC 40:a3:6b:a2:16:14  ->  OOB 11.162.22.20, media ports 10.162.22.20
  * two that are FINISHED and in monitoring - a Fusion and a MuoN. They are not
    hard-coded: at start they are commissioned through the very same guided-setup
    code an engineer uses, so what you see is what the guide writes.

The simulated devices are derived from config_catalog.py. They show that the
tool's plumbing works, NOT that a real unit accepts the same writes.
"""
import copy
import math
import os
import re
import shutil
import sys
import time
import uuid
from pathlib import Path

import config_catalog as cat
import config_tool as ct
import guide_tool

HERE = Path(__file__).resolve().parent
DEMO_DIR = HERE / "demo-data"

REGISTRY = {}   # ip -> FakeDevice

FACTORY_MAC = "40:a3:6b:a2:16:14"
DEMO_SITE = {"site_name": "Demo Studio", "syslog": {"server": "10.20.0.50", "port": 514, "enable": True},
             "nmos": {"registry": "10.20.0.60:8080"}, "ptp": {"domain": 127, "dscp": 46},
             "system": {"igmp_version": 3, "class_2022_7": "d"},
             "network": {"red_vlan": 0, "blue_vlan": 0, "red_dhcp": False, "blue_dhcp": False, "blue_octet_offset": 1},
             "audio": {"streams": 2, "channels_per_stream": 8}}
DEMO_HINTS = {"mac": FACTORY_MAC, "identity": {"name": "Studio C Gateway", "tag": "STC-GW-01", "red_ip": "10.20.1.21",
                                                "red_prefix": "24", "red_gateway": "10.20.1.1"}}
GROUPS = [{"re": "^Studio A", "label": "Studio A"}, {"re": "^Studio B", "label": "Studio B"}, {"re": "^Studio C", "label": "Studio C"}]
FINISHED = [
    {"mac": "40:a3:6b:b0:11:21", "factory_ip": "10.176.17.33", "base_type": "FusioN6", "channels": 6, "temp": 61,
     "identity": {"name": "Studio A Gateway", "tag": "STA-GW-01", "red_ip": "10.20.1.11", "red_prefix": 24, "red_gateway": "10.20.1.1"}},
    {"mac": "40:a3:6b:b0:12:35", "factory_ip": "10.176.18.53", "base_type": "MuoN B", "channels": 2, "temp": 67, "info_warning": True,
     "identity": {"name": "Studio B Decoder", "tag": "STB-DEC-01", "red_ip": "10.20.1.12", "red_prefix": 24, "red_gateway": "10.20.1.1"}},
]


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
    if not isinstance(path[-1], int):
        cur[path[-1]] = value


def _has(d, path):
    cur = d
    for k in path:
        if isinstance(k, int):
            continue
        if not isinstance(cur, dict) or k not in cur:
            return False
        cur = cur[k]
    return True


def _merge(dst, src):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        else:
            dst[k] = v


def _uuid(seed):
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))


class FakeDevice:
    """One simulated emsfp node. Fusion: 6 channels, 6 SDI outputs, 6 SFP ports, OOB. MuoN: 2 channels, no ports/OOB."""

    def __init__(self, mac, factory_ip, base_type="FusioN6", channels=6, temp=58, info_warning=False):
        self.mac, self.base_type, self.channels, self.temp, self.info_warning = mac, base_type, channels, temp, info_warning
        self.muon = base_type.startswith("MuoN")
        b = [int(x, 16) for x in mac.split(":")][3:]
        self.oob = None if self.muon else "11.%d.%d.%d" % tuple(b)
        self.ips = {factory_ip}
        self.ep = e = {}
        e["self/information"] = {"type": "18 - ST2110 UHD Decapsulator" if self.muon else "22 - ST2110 UHD Transceiver",
                                 "base_type": base_type, "serial_number": "1250722%05d" % (b[0] * 256 + b[1] + b[2])}
        e["self/firmware"] = {"info": [{"active": "yes", "semantic_version": "4.8.4"}, {"active": "no", "semantic_version": "4.8.0"}]}
        e["self/license"] = {"feature": {"clean_switch": "enabled", "uhd_support": "enabled"} if self.muon else
                             {"clean_switch": "enabled", "uhd_support": "enabled", "frame_sync": "enabled", "black_burst": "enabled"}}
        e["self/ipconfig"] = {"hostname": "emsfp-%02x-%02x-%02x" % tuple(b), "local_mac": mac}
        ifs = {
            "e1": {"static_ip": factory_ip + "/24", "static_gateway": "0.0.0.0", "current_ip": factory_ip + "/24",
                   "mac_address": mac, "dhcp": True, "vlan": 0},
            "e2": {"static_ip": "0.0.0.0/0", "static_gateway": "0.0.0.0", "current_ip": "0.0.0.0/0",
                   "mac_address": mac[:-2] + "%02x" % (int(mac[-2:], 16) + 1), "dhcp": True, "vlan": 0}}
        if not self.muon:
            ifs["oob"] = {"static_ip": "11.%d.%d.%d/8" % tuple(b), "static_gateway": "0.0.0.0", "dhcp": True,
                          "mac_address": mac[:-2] + "%02x" % (int(mac[-2:], 16) + 2)}
        e["self/interfaces"] = ifs
        e["self/system"] = {"uptime": "3 days, 04:12:09"}
        e["self/diag/nmos"] = {"status": "disconnected"}
        e["lldp"] = {"configuration": {"rate": "45", "untag": True, "enable_rx": "1"},
                     "neighbor": [{"interface": 1, "chassis": "c4:ca:2b:ff:eb:7a", "port": "Ethernet3/%d/3" % (b[2] % 30 + 1), "ttl": "120"},
                                  {"interface": 2, "chassis": "c4:ca:2b:ff:eb:62", "port": "Ethernet3/%d/3" % (b[2] % 30 + 1), "ttl": "120"}] +
                     ([] if self.muon else [{"interface": 3, "chassis": "", "port": "", "ttl": "0"}])}
        for s in cat.CATALOG:                       # every device-scope setting the catalog knows
            if s["scope"] == cat.DEVICE and s["endpoint"] and s["endpoint"] != "self/interfaces":
                d = e.setdefault(s["endpoint"], {})
                if not _has(d, s["field"]):
                    _set(d, s["field"], _default(s))
        e["self/syslog"]["config"].update({"server": "192.168.0.0", "port": 514, "enable": False})
        if not self.muon:
            e["sdi"] = {"configuration": {"operating_bit_rate": "auto"}, "running_operating_bit_rate": "integer"}
            self.port_ids = [str(n) for n in range(1, 7)]
            for n in self.port_ids:
                up = n in ("1", "3", "5", "6")
                e["port/" + n] = {"link": "up" if up else "down", "speed": "25Gbps", "host_pinout": "2T" if n in ("1", "2") else "RT",
                                  "detected_sfp_part_number": "1913047" if up else "N/A", "sfp_type": "auto"}
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
            for n, ess in enumerate(["video", "audio", "audio", "audio", "audio", "data"]):
                flows = []
                for leg, nm in (("primary", "pri"), ("secondary", "sec")):
                    fid = _uuid(f"{mac}-{ch}-{n}-{nm}")
                    flows.append({"flow": fid, "type": leg, "essence": ess})
                    f = {"name": f"rx ch{ch} flow {n} {nm}", "network": {}}
                    for s in cat.CATALOG:
                        if s["scope"] == cat.FLOW:
                            _set(f, s["field"], _default(s))
                    e["flows/" + fid] = f
                engines.append({"essence": ess, "flows": flows})
            tele.append({"channel": ch, "type": "decapsulator", "engines": engines})
        self.tele = tele
        self.counters = {}
        for ip in self.ips | ({self.oob} if self.oob else set()):
            REGISTRY[ip] = self
        self.up_since = time.time() - 3 * 86400 - 4 * 3600
        self.provisioned = False

    # -- behaviour
    def _readdress(self):
        i = self.ep["self/interfaces"]
        e1, e2 = i["e1"]["static_ip"].split("/")[0], i["e2"]["static_ip"].split("/")[0]
        for ip in list(self.ips):
            REGISTRY.pop(ip, None)
        self.ips = {e1, e2}
        for ip in self.ips:
            REGISTRY[ip] = self
        i["e1"]["current_ip"], i["e2"]["current_ip"] = i["e1"]["static_ip"], i["e2"]["static_ip"]
        self.provisioned = True
        self.ep["self/diag/nmos"]["status"] = "connected"

    def _node(self):
        t = time.time()
        wobble = math.sin(t / 37.0 + int(self.mac[-2:], 16)) * 1.2
        rx = (8.0e8 + wobble * 4e6) if self.provisioned else 0      # bytes per second (the dashboard multiplies by 8)
        return {"health": {"core_temp": round(self.temp + wobble, 1), "fan_speed": 5000 + int(wobble * 40)} if not self.muon
                else {"core_temp": round(self.temp + wobble, 1)},
                "refclk": {"status": "locked" if self.provisioned else "unlocked", "offset_from_master": int(wobble * 6) if self.provisioned else 0},
                "interfaces": {"e1": {"link_status": "up", "rx_bw": rx, "tx_bw": 5.0e4}, "e2": {"link_status": "up", "rx_bw": rx * 0.98, "tx_bw": 5.0e4}}}

    def _telemetry_devices(self):
        out = copy.deepcopy(self.tele)
        t = time.time()
        for chan in out:
            for eng in chan["engines"]:
                for fl in eng["flows"]:
                    on = str(self.ep["flows/" + fl["flow"]]["network"].get("enable")) in ("1", "True", "true")
                    rate = {"video": 216000, "audio": 8000, "data": 120}.get(eng["essence"], 0) if on else 0
                    jitter = int(math.sin(t / 5.0 + len(fl["flow"])) * 3) if rate else 0
                    cnt = self.counters.get(fl["flow"], 0) + (rate * 2 if on else 0)
                    self.counters[fl["flow"]] = cnt
                    fl.update({"pkt_rate": rate + jitter, "pkt_cnt": cnt, "sequence_error": 0})
        return {"devices": out}

    def get(self, path):
        p = path.strip("/")
        e = self.ep
        if p == "":
            return ["self/", "flows/", "sdi_output/", "refclk/", "lldp/", "telemetry/", "devices/"] + ([] if self.muon else ["port/", "sdi/"])
        if p == "flows":
            return [k.split("/", 1)[1] + "/" for k in e if k.startswith("flows/")]
        if p == "sdi_output":
            return [i + "/" for i in self.sdi_ids]
        if p == "port":
            if self.muon:
                raise FakeError("HTTP Error 404: Not Found")
            return [n + "/" for n in self.port_ids]
        if p == "devices":
            return []
        if p == "telemetry/node":
            return self._node()
        if p == "telemetry/devices":
            return self._telemetry_devices()
        if p == "telemetry/warnings":
            return ["1/"] if self.info_warning and self.provisioned else []
        if p == "telemetry/warnings/1":
            return {"warning": [{"code": 1001, "uuid": [self.sdi_ids[min(1, len(self.sdi_ids) - 1)] + "/"]}]}
        if p == "self/system":
            up = int(time.time() - self.up_since)
            return dict(e["self/system"], uptime="%d days, %02d:%02d:%02d" % (up // 86400, up % 86400 // 3600, up % 3600 // 60, up % 60))
        if p.startswith("self/diag/flow/"):
            return {"commit": "0", "reset": "0", "reset_counter": "0"}
        if p in e:
            return copy.deepcopy(e[p])
        raise FakeError("HTTP Error 404: Not Found")

    def post(self, path, body):
        p = path.strip("/")
        if p.startswith("self/diag/flow/"):
            fid = p.rsplit("/", 1)[1]
            if str((body.get("config") or body).get("reset_counter", "0")) == "1":
                self.counters[fid] = 0
            return 200, "ok"
        if p not in self.ep:
            raise FakeError("HTTP Error 404: Not Found")
        before = self.ep["self/interfaces"]["e1"]["static_ip"]
        _merge(self.ep[p], body)
        if p == "self/interfaces" and self.ep["self/interfaces"]["e1"]["static_ip"] != before:
            self._readdress()
        return 200, "ok"


def _device(ip):
    host = ip.split(":")[0]
    if host not in REGISTRY:
        raise FakeError("<urlopen error timed out>")
    return REGISTRY[host]


def fake_get(ip, path):
    return _device(ip).get(path)


def fake_post(ip, path, payload):
    return _device(ip).post(path, payload)


def install(server=None):
    """Route the service's device calls to the fake devices."""
    ct.http_get, ct.http_post = fake_get, fake_post
    guide_tool.FORCE_LOCAL = True
    guide_tool.host_interfaces = lambda: [{"name": "en0", "ip": "192.168.1.23", "prefix": 24}]   # never show the real host's interfaces
    guide_tool._tcp_open = lambda ip, *a, **k: ip in REGISTRY      # the address scan sees only simulated devices
    if server is not None:
        strip = lambda path: re.sub(r"^/emsfp/node/v1/", "", path)
        server.http_get_json = lambda ip, path, timeout: fake_get(ip, strip(path))
        server.http_post = lambda ip, path, payload, timeout=4: fake_post(ip, strip(path), payload)


# ---------------------------------------------------------------- demo start

def arg(name, default):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def prepare(server):
    """Switch the service to demo mode: simulated network, its own empty data folder."""
    if DEMO_DIR.exists():
        shutil.rmtree(DEMO_DIR, ignore_errors=True)
    (DEMO_DIR / "profiles").mkdir(parents=True)
    shutil.copy(HERE / "config-profiles" / "site-default.example.json", DEMO_DIR / "profiles")
    server.DEMO, server.DEMO_HINTS, server.GROUP_RULES = True, DEMO_HINTS, GROUPS
    server.HOST, server.PORT = arg("--host", "127.0.0.1"), int(arg("--port", "8190"))
    server.DEVICES_FILE, server.DEVICES_EXAMPLE_FILE, server.SEEDED_FILE = DEMO_DIR / "devices.json", DEMO_DIR / "none.json", DEMO_DIR / "seeded.json"
    server.DEVICES_FILE.write_text("[]", encoding="utf-8")
    guide_tool.STATE_FILE, guide_tool.SITE_FILE = DEMO_DIR / "guide-state.json", DEMO_DIR / "guide-site.json"
    ct.PROFILES_DIR, ct.BACKUPS_DIR = DEMO_DIR / "profiles", DEMO_DIR / "backups"
    install(server)
    guide_tool.save_site(DEMO_SITE)
    FakeDevice(FACTORY_MAC, "10.162.22.20")


def seed(log):
    """Commission the two finished devices through the real guided-setup code."""
    for d in FINISHED:
        fd = FakeDevice(d["mac"], d["factory_ip"], d["base_type"], d["channels"], d["temp"], d.get("info_warning", False))
        log("demo: setting up %s ..." % d["identity"]["name"])
        rec = guide_tool.connect(d["factory_ip"])
        mac, serial = rec["mac"], rec["facts"]["serial"]
        guide_tool.apply(mac, "identity", d["identity"], serial)
        r = guide_tool.confirm_identity(mac)
        if not r.get("ok"):
            raise RuntimeError("demo device %s did not come up: %s" % (d["identity"]["name"], r))
        for step in ("services", "channels"):
            res = guide_tool.apply(mac, step, {}, serial)
            if not res["ok"]:
                raise RuntimeError("demo step %s failed for %s" % (step, d["identity"]["name"]))
        out = guide_tool.hand_over(mac, True)
        if not out.get("ok"):
            raise RuntimeError("demo hand-over failed: %s" % out)
        fd.up_since = time.time() - (5 if d["base_type"] == "FusioN6" else 2) * 86400
    log("demo: ready - open 'New device' to set up the third device (MAC %s)" % FACTORY_MAC)
