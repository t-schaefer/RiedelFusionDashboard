"""
Guided setup: a step-by-step front end to commission a new Fusion from factory
state and hand it over to monitoring (served at /guide).

It adds no new wire logic: reading, planning, writing and read-back are the
config tool's (config_tool.py). This module decides WHICH settings belong to
which step, builds their values from the site settings plus what the engineer
types, explains the result in plain words, and remembers per device how far
the setup got (guide-state.json).

Steps:   connect -> identity (name + network) -> services (time, NMOS, syslog)
         -> channels (flows + SDI audio) -> verify (checks + hand over).
Roadmap: firmware step (docs/ROADMAP.md).
"""

import ipaddress
import json
import logging
import platform
import re
import shutil
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import config_catalog as cat
import config_tool as ct

BASE_DIR = Path(__file__).resolve().parent
SITE_FILE = BASE_DIR / "guide-site.json"
STATE_FILE = BASE_DIR / "guide-state.json"
UI_FILE = BASE_DIR / "guide.html"

logger = logging.getLogger("fusion-dashboard")
LOCK = threading.RLock()

FORCE_LOCAL = False        # demo: pretend the server is the engineer's own laptop
_add_device = lambda ip, name, tag: None
_device_list = lambda: []


def init(add_device, device_list):
    """server.py hands over how to add a device to the dashboard and its device list."""
    global _add_device, _device_list
    _add_device, _device_list = add_device, device_list


STEPS = [
    {"id": "connect", "title": "Connect", "writes": False,
     "why": "Find the device and make sure this is the unit you hold in your hands."},
    {"id": "identity", "title": "Name & network", "writes": True,
     "why": "Give the device its name and its permanent Red / Blue addresses."},
    {"id": "services", "title": "Time & services", "writes": True,
     "why": "PTP, NMOS registration, syslog and LLDP, taken from the site settings."},
    {"id": "channels", "title": "Channels & audio", "writes": True,
     "why": "Switch on the streams of every channel and map the audio to the SDI outputs."},
    {"id": "verify", "title": "Check & hand over", "writes": False,
     "why": "Read everything back, check the device is healthy, then add it to monitoring."},
]
STEP_IDS = [s["id"] for s in STEPS]

# ---------------------------------------------------------------- site settings

DEFAULT_SITE = {
    "configured": False,
    "site_name": "",
    "network": {"red_vlan": 0, "blue_vlan": 0, "red_dhcp": False, "blue_dhcp": True, "blue_octet_offset": 1},
    "syslog": {"server": "10.0.0.50", "port": 514, "enable": True},
    "nmos": {"registry": "10.0.0.60:8080"},
    "ptp": {"domain": 127, "dscp": 46},
    "system": {"igmp_version": 3, "class_2022_7": "d"},
    "audio": {"streams": 4, "channels_per_stream": 4},
}


def _merge(base, extra):
    out = json.loads(json.dumps(base))
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_site():
    try:
        return _merge(DEFAULT_SITE, json.loads(SITE_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return json.loads(json.dumps(DEFAULT_SITE))


def _int(value, name, lo, hi):
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a whole number")
    if not lo <= n <= hi:
        raise ValueError(f"{name} must be between {lo} and {hi}")
    return n


def _hostport(value, name):
    m = re.fullmatch(r"(\d{1,3}(?:\.\d{1,3}){3}):(\d{1,5})", str(value or "").strip())
    if not m:
        raise ValueError(f"{name} must look like 10.12.81.11:8080")
    ipaddress.ip_address(m.group(1))
    _int(m.group(2), name + " port", 1, 65535)
    return m.group(0)


def validate_site(data):
    """Returns a clean site dict from what the form sent, or raises ValueError."""
    site = _merge(DEFAULT_SITE, data)
    net = site["network"]
    net["red_vlan"] = _int(net["red_vlan"], "Red VLAN", 0, 4094)
    net["blue_vlan"] = _int(net["blue_vlan"], "Blue VLAN", 0, 4094)
    net["red_dhcp"], net["blue_dhcp"] = bool(net["red_dhcp"]), bool(net["blue_dhcp"])
    net["blue_octet_offset"] = _int(net["blue_octet_offset"], "Blue network offset", 0, 50)
    sl = site["syslog"]
    ipaddress.ip_address(str(sl["server"]).strip())
    sl["server"] = str(sl["server"]).strip()
    sl["port"] = _int(sl["port"], "Syslog port", 1, 65535)
    sl["enable"] = bool(sl["enable"])
    site["nmos"]["registry"] = _hostport(site["nmos"]["registry"], "NMOS registry")
    site["ptp"]["domain"] = _int(site["ptp"]["domain"], "PTP domain", 0, 127)
    site["ptp"]["dscp"] = _int(site["ptp"]["dscp"], "PTP DSCP", 0, 63)
    site["system"]["igmp_version"] = _int(site["system"]["igmp_version"], "IGMP version", 2, 3)
    if str(site["system"]["class_2022_7"]).lower() not in ("a", "b", "c", "d"):
        raise ValueError("ST 2022-7 class must be a, b, c or d")
    site["system"]["class_2022_7"] = str(site["system"]["class_2022_7"]).lower()
    au = site["audio"]
    au["streams"] = _int(au["streams"], "Audio streams per channel", 1, 4)
    au["channels_per_stream"] = _int(au["channels_per_stream"], "Audio channels per stream", 1, 16)
    if au["streams"] * au["channels_per_stream"] > 16:
        raise ValueError("Audio streams x channels per stream is %d - an SDI output carries at most 16 embedded channels"
                         % (au["streams"] * au["channels_per_stream"]))
    site["site_name"] = str(site.get("site_name") or "").strip()[:60]
    site["configured"] = True
    return site


def save_site(data):
    site = validate_site(data)
    with LOCK:
        SITE_FILE.write_text(json.dumps(site, indent=2), encoding="utf-8")
    return site


# ---------------------------------------------------------------- per-device progress

def _load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"devices": {}}


def _save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=1), encoding="utf-8")


def _now():
    return datetime.now().isoformat(timespec="seconds")


def devices_state():
    with LOCK:
        return _load_state()["devices"]


def _update(mac, **fields):
    with LOCK:
        state = _load_state()
        rec = state["devices"].setdefault(mac, {"mac": mac, "created": _now(), "steps": {}, "inputs": {}})
        rec.update(fields)
        rec["updated"] = _now()
        _save_state(state)
        return rec


def _mark(mac, step, status, note=""):
    with LOCK:
        state = _load_state()
        rec = state["devices"][mac]
        rec["steps"][step] = {"status": status, "at": _now(), "note": note}
        rec["updated"] = _now()
        _save_state(state)
        return rec


def forget(mac):
    """Removes a device's setup record (it does not touch the device or the dashboard)."""
    with LOCK:
        state = _load_state()
        if state["devices"].pop(mac, None) is None:
            raise ValueError("unknown device")
        _save_state(state)


def _get(mac):
    rec = devices_state().get(mac)
    if not rec:
        raise ValueError("unknown device - start with the Connect step")
    return rec


# ---------------------------------------------------------------- step 1: connect

def parse_mac(text):
    digits = re.sub(r"[^0-9a-fA-F]", "", str(text or ""))
    if len(digits) != 12:
        raise ValueError("a MAC address has 12 hex digits, e.g. 40:A3:6B:A2:16:14")
    return bytes.fromhex(digits)


def factory_addresses(mac_text):
    """Factory addresses derived from the MAC of the first media port (slot 3):
    the last three bytes, each in decimal."""
    b = parse_mac(mac_text)
    a, c, d = b[3], b[4], b[5]
    return {
        "mac": ":".join("%02x" % x for x in b),
        "media": f"10.{a}.{c}.{d}",
        "oob": f"11.{a}.{c}.{d}",
        "oob_prefix": 8,
        "laptop_oob": "11.0.0.1",
        "laptop_media": f"10.{a}.{c}.1" if d != 1 else f"10.{a}.{c}.2",
        "notes": [
            "OOB port: 11.x.y.z/8 - matches the OOB address on devices already in the fleet (one device checked).",
            "Media ports (Red/Blue): 10.x.y.z - the rule Riedel documents for the MuoN A10 (first-port MAC); not yet confirmed on a Fusion.",
        ],
    }


# ---------------------------------------------------------------- this computer's network (read only)
def _run(cmd, timeout=6):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)      # Windows: no console window flashing up from the service
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=flags).stdout or ""


def host_interfaces():
    """IPv4 addresses of the computer the SERVER runs on: [{name, ip, prefix}]. Read only."""
    out, system = [], platform.system()
    try:
        if system == "Windows":
            raw = _run(["powershell", "-NoProfile", "-Command",
                        "Get-NetIPAddress -AddressFamily IPv4 | Select-Object InterfaceAlias,IPAddress,PrefixLength | ConvertTo-Json"])
            rows = json.loads(raw) if raw.strip() else []
            for r in rows if isinstance(rows, list) else [rows]:
                out.append({"name": r["InterfaceAlias"], "ip": r["IPAddress"], "prefix": int(r["PrefixLength"])})
        elif shutil.which("ip"):
            for line in _run(["ip", "-4", "-o", "addr"]).splitlines():
                m = re.match(r"\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)", line)
                if m:
                    out.append({"name": m.group(1), "ip": m.group(2), "prefix": int(m.group(3))})
        else:
            name = None
            for line in _run(["ifconfig"]).splitlines():
                h = re.match(r"^(\S+?):\s", line)
                if h:
                    name = h.group(1)
                m = re.search(r"\binet (\d+\.\d+\.\d+\.\d+) netmask (0x[0-9a-fA-F]+|\d+\.\d+\.\d+\.\d+)", line)
                if m and name:
                    mask = m.group(2)
                    prefix = bin(int(mask, 16)).count("1") if mask.startswith("0x") else ipaddress.ip_network("0.0.0.0/" + mask).prefixlen
                    out.append({"name": name, "ip": m.group(1), "prefix": prefix})
    except Exception as e:
        logger.warning("could not list network interfaces: %s", e)
    return [i for i in out if not i["ip"].startswith(("127.", "169.254."))]


def host_network(target_ip, prefix=None, is_local=True):
    """Can the computer running the server reach target_ip directly (same subnet)? Plus the commands to add a
    temporary address - shown to the engineer to copy, never executed here (changing a computer's network is its owner's job)."""
    ipaddress.ip_address(target_ip)
    ifs = host_interfaces() if is_local else []
    covered = None
    for i in ifs:
        net = ipaddress.ip_network("%s/%d" % (i["ip"], i["prefix"]), strict=False)
        if ipaddress.ip_address(target_ip) in net:
            covered = i
            break
    return {"system": platform.system(), "local": is_local, "interfaces": ifs, "covered": covered, "target": target_ip}


# ---------------------------------------------------------------- find a device whose address is unknown (GET only)
def _tcp_open(ip, port=80, timeout=0.6):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def scan(cidr):
    """Looks for emsfp devices in one subnet (at most a /23): a quick TCP check on port 80, then ONE read of
    self/information on hosts that answer. Never writes."""
    try:
        net = ipaddress.ip_network(str(cidr or "").strip(), strict=False)
    except ValueError:
        raise ValueError("enter a network like 10.20.1.0/24")
    if net.version != 4 or net.num_addresses > 512:
        raise ValueError("scan at most a /23 (512 addresses) at a time")

    def probe(host):
        ip = str(host)
        if not _tcp_open(ip):
            return None
        try:
            info = ct.http_get(ip, "self/information")
            if not isinstance(info, dict) or not info.get("serial_number"):
                return None
            try:
                hostname = (ct.http_get(ip, "self/ipconfig") or {}).get("hostname")
            except Exception:
                hostname = None
            return {"ip": ip, "type": info.get("type"), "serial": info.get("serial_number"), "hostname": hostname}
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=64) as ex:
        found = [r for r in ex.map(probe, net.hosts() if net.prefixlen < 31 else list(net)) if r]
    known = {d.get("ip") for d in _device_list()}
    for r in found:
        r["inDashboard"] = r["ip"] in known
    return {"network": str(net), "found": found, "scanned": net.num_addresses}


def _channels(ip_addr):
    try:
        data = ct.http_get(ip_addr, "telemetry/devices")
        return [{"channel": c.get("channel"), "type": c.get("type")} for c in (data or {}).get("devices", [])]
    except Exception:
        return []


def connect(ip):
    """Reads the device (GET only) and starts / refreshes its setup record."""
    ip = str(ip or "").strip()
    if not ip:
        raise ValueError("enter the device address")
    try:
        ipaddress.IPv4Address(ip)
    except ValueError:
        raise ValueError("'%s' is not an IPv4 address - use the form 10.20.1.11" % ip)
    try:
        read = ct.read_device(ip)
    except Exception as e:
        raise ValueError("cannot reach %s (%s). Check the cable, that your computer is on the same network, and the address." % (ip, e))
    with ct.READS_LOCK:
        ct.READS[ip] = read
    eps = read["endpoints"]
    ifs = eps.get("self/interfaces") or {}
    info = eps.get("self/information") or {}
    mac = ((ifs.get("e1") or {}).get("mac_address") or "").lower()
    if not mac:
        raise ValueError("the device answered but reported no MAC address - is this an emsfp device?")
    chans = _channels(read["address"])
    facts = {
        "mac": mac, "ip": ip, "address": read["address"], "network": read["network"],
        "type": info.get("type"), "baseType": info.get("base_type"), "serial": info.get("serial_number"),
        "firmware": read["summary"]["firmware"], "hostname": read["summary"]["hostname"],
        "e1": (ifs.get("e1") or {}).get("current_ip"), "e2": (ifs.get("e2") or {}).get("current_ip"),
        "channels": chans,
        "license": eps.get("self/license"),
    }
    host = str(facts["hostname"] or "")
    facts["looksFactory"] = host.startswith("emsfp-") or host == ""
    rec = _update(mac, ip=ip, facts=facts)
    if rec["steps"].get("connect", {}).get("status") != "done":
        _mark(mac, "connect", "done", "connected via %s" % read["network"])
    return _public(_get(mac))


def _public(rec):
    rec = json.loads(json.dumps(rec))
    rec["next"] = next_step(rec)
    return rec


def next_step(rec):
    for sid in STEP_IDS:
        if (rec["steps"].get(sid) or {}).get("status") not in ("done",):
            return sid
    return None


def _require_ready(rec, step):
    """A step can only run once every step before it is done (written steps count
    as done only after their check passed)."""
    for sid in STEP_IDS[:STEP_IDS.index(step)]:
        if (rec["steps"].get(sid) or {}).get("status") != "done":
            title = next(s["title"] for s in STEPS if s["id"] == sid)
            raise ValueError("finish the step '%s' first" % title)
    if step in ("channels",) and not load_site().get("configured"):
        raise ValueError("fill in the Site settings first (audio layout)")


# ---------------------------------------------------------------- building values per step

HOST_RE = re.compile(r"^[A-Za-z0-9 ._/\-]{1,63}$")


def _shift_second_octet(addr, offset):
    parts = str(addr).split(".")
    if len(parts) == 4 and offset:
        parts[1] = str(int(parts[1]) + offset)
    return ".".join(parts)


def identity_defaults(rec):
    """Suggestions for the identity form."""
    site = load_site()
    facts = rec.get("facts") or {}
    return {
        "name": "", "tag": "", "red_ip": "", "red_prefix": 30, "red_gateway": "",
        "blue_ip": "", "blue_gateway": "", "blue_prefix": 30,
        "red_vlan": site["network"]["red_vlan"], "blue_vlan": site["network"]["blue_vlan"],
        "current": {"hostname": facts.get("hostname"), "e1": facts.get("e1"), "e2": facts.get("e2")},
    }


def _identity_inputs(inp, rec):
    site = load_site()
    name = str(inp.get("name") or "").strip()
    if not HOST_RE.match(name):
        raise ValueError("name: use letters, digits and . _ / - (max 63 characters)")
    out = {"name": name, "tag": str(inp.get("tag") or "").strip()[:40]}
    off = site["network"]["blue_octet_offset"]
    for net in ("red", "blue"):
        ip = str(inp.get(net + "_ip") or "").strip()
        gw = str(inp.get(net + "_gateway") or "").strip()
        if net == "blue":
            ip = ip or (_shift_second_octet(out["red_ip"], off) if off else "")
            gw = gw or (_shift_second_octet(out["red_gateway"], off) if off else "")
        try:
            prefix = int(inp.get(net + "_prefix") or (out["red_prefix"] if net == "blue" else 30))
            iface = ipaddress.ip_interface(f"{ip}/{prefix}")
            gateway = ipaddress.ip_address(gw)
        except ValueError:
            raise ValueError(f"{net.title()}: enter a valid IP address, prefix and gateway")
        if gateway not in iface.network:
            raise ValueError(f"{net.title()}: the gateway {gw} is not inside {iface.network}")
        if gateway == iface.ip:
            raise ValueError(f"{net.title()}: the gateway must differ from the device address")
        out[net + "_ip"], out[net + "_prefix"], out[net + "_gateway"] = str(iface.ip), prefix, str(gateway)
    if out["red_ip"] == out["blue_ip"]:
        raise ValueError("Red and Blue need different addresses")
    for dev in _device_list():
        if dev.get("ip") in (out["red_ip"], out["blue_ip"]) and dev.get("ip") != rec.get("ip"):
            raise ValueError("%s is already used by '%s' in the dashboard" % (dev["ip"], dev.get("name") or "another device"))
    out["red_vlan"] = _int(inp.get("red_vlan", site["network"]["red_vlan"]), "Red VLAN", 0, 4094)
    out["blue_vlan"] = _int(inp.get("blue_vlan", site["network"]["blue_vlan"]), "Blue VLAN", 0, 4094)
    return out


def identity_profile(inp, rec):
    site = load_site()
    i = _identity_inputs(inp, rec)
    values = {
        "hostname": i["name"],
        "if.e1.static_ip": f"{i['red_ip']}/{i['red_prefix']}", "if.e1.static_gateway": i["red_gateway"],
        "if.e1.dhcp": site["network"]["red_dhcp"], "if.e1.vlan": i["red_vlan"],
        "if.e2.static_ip": f"{i['blue_ip']}/{i['blue_prefix']}", "if.e2.static_gateway": i["blue_gateway"],
        "if.e2.dhcp": site["network"]["blue_dhcp"], "if.e2.vlan": i["blue_vlan"],
    }
    return {"values": values, "io": {}}, i


SERVICE_IDS = [
    "syslog.server", "syslog.port", "syslog.enable",
    "nmos.registry_mode", "nmos.registry_address",
    "sys.igmp_version", "sys.2022_7_class",
    "lldp.rate", "lldp.untag", "lldp.enable_rx",
    "ptp.domain", "ptp.dscp",
]


def effective_site(overrides):
    """The site settings with this device's own overrides applied (and validated).
    Overrides use the same shape as the site settings, e.g. {"audio": {"streams": 2}}."""
    if not overrides:
        return load_site()
    site = validate_site(_merge(load_site(), overrides))
    return site


def _overrides(inputs):
    o = (inputs or {}).get("overrides")
    return o if isinstance(o, dict) else {}


def services_profile(site=None):
    """Site values for the services step; everything else the site-default profile
    already defines (syslog monitoring flags, PTP timing, NMOS details) is added
    on top so a new device ends up like the rest of the fleet."""
    site = site or load_site()
    base = {}
    for name in ("site-default", "site-default.example"):
        f = ct.PROFILES_DIR / (name + ".json")
        if f.exists():
            base = json.loads(f.read_text(encoding="utf-8"))
            break
    wanted_groups = {"Syslog", "Syslog monitoring", "NMOS registration", "PTP"}
    values = {}
    for sid, v in (base.get("values") or {}).items():
        s = cat.BY_ID.get(sid)
        if s and s["group"] in wanted_groups:
            values[sid] = v
    for sid in ("sys.igmp_version", "sys.2022_7_class", "lldp.rate", "lldp.untag", "lldp.enable_rx"):
        if sid in (base.get("values") or {}):
            values[sid] = base["values"][sid]
    values.update({
        "syslog.server": site["syslog"]["server"], "syslog.port": site["syslog"]["port"],
        "syslog.enable": site["syslog"]["enable"],
        "nmos.registry_mode": "manual", "nmos.registry_address": site["nmos"]["registry"],
        "sys.igmp_version": site["system"]["igmp_version"], "sys.2022_7_class": site["system"]["class_2022_7"],
        "ptp.domain": str(site["ptp"]["domain"]), "ptp.dscp": str(site["ptp"]["dscp"]),
    })
    return {"values": values, "io": {}}


# channels ----------------------------------------------------------------

SDI_OUT_RE = re.compile(r"^b([0-7])d2da17-")
SDI_NUMBER = {0: 1, 1: 2, 2: 3, 3: 4, 5: 6, 7: 8}   # output id prefix -> SDI n (b4 / b6 are inputs)


def _telemetry(addr):
    data = ct.http_get(addr, "telemetry/devices")
    return (data or {}).get("devices", [])


def channels_profile(read, site=None):
    """Flow enable states and SDI audio map for the site's audio layout.
    Per channel: video, the first N audio streams and the data stream ON (both
    legs, ST 2022-7), any further audio streams OFF."""
    site = site or load_site()
    streams, per = site["audio"]["streams"], site["audio"]["channels_per_stream"]
    tele = _telemetry(read["address"])
    by_uuid = {}
    for inst in read["instances"].get(cat.FLOW, []):
        by_uuid[str(inst["id"])] = inst
    flow_io, audio_flow_name = {}, {}   # audio_flow_name[(channel, stream)] = name of the primary leg
    summary = {"channels": [], "sdi": [], "audio": f"{streams} audio stream(s) x {per} channel(s)"}
    for chan in tele:
        ch, seen = chan.get("channel"), {}
        on_names, off_names = [], []
        for eng in chan.get("engines", []):
            ess = eng.get("essence") or "?"
            seen[ess] = seen.get(ess, 0) + 1
            on = ess == "video" or (ess == "audio" and seen[ess] <= streams) or (ess == "data" and seen[ess] == 1)
            (on_names if on else off_names).append(ess if ess != "audio" else f"audio {seen[ess]}")
            for fl in eng.get("flows", []):
                inst = by_uuid.get(str(fl.get("flow"))) or next(
                    (i for u, i in by_uuid.items() if str(fl.get("flow")).startswith(u) or u.startswith(str(fl.get("flow")))), None)
                if not inst:
                    continue
                flow_io[inst["key"]] = {"flow.enable": "1" if on else "0"}
                if ess == "audio" and on and fl.get("type") == "primary":
                    audio_flow_name[(ch, seen[ess])] = inst["key"]
        summary["channels"].append({"channel": ch, "direction": "receive" if chan.get("type") == "decapsulator" else "transmit",
                                    "on": on_names, "off": off_names})
    # The n-th SDI output (by id) belongs to the n-th receive channel: a Fusion
    # has six of each, the output ids run b0..b3,b5,b7 and the connector labels
    # SDI 1-4, 6, 8. An output with no matching channel is left untouched
    # rather than being cleared.
    rx_channels = sorted(c.get("channel") for c in tele if c.get("type") == "decapsulator")
    outs = sorted((o for o in read["instances"].get(cat.SDI_OUTPUT, []) if SDI_OUT_RE.match(str(o["id"]))),
                  key=lambda o: str(o["id"]))
    sdi_io = {}
    for idx, out in enumerate(outs):
        m = SDI_OUT_RE.match(str(out["id"]))
        n = SDI_NUMBER.get(int(m.group(1)), idx + 1)
        ch = rx_channels[idx] if idx < len(rx_channels) else None
        if ch is None or not all((ch, st) in audio_flow_name for st in range(1, streams + 1)):
            summary["sdi"].append({"output": f"SDI {n}", "from": None, "map": [], "unused": "", "skipped": True})
            continue
        vals = {}
        for i in range(16):
            st, c = divmod(i, per)
            name = audio_flow_name.get((ch, st + 1)) if i < streams * per else None
            vals[f"sdi.audio.ch{i}"] = f"{name}:{c}:1" if name else ""
        sdi_io[out["key"]] = vals
        summary["sdi"].append({"output": f"SDI {n}", "from": f"CH{ch:02d}", "map": [
            f"embedded {st * per + 1}-{st * per + per} from audio stream {st + 1} (ch 1-{per})" for st in range(streams)],
            "unused": f"embedded {streams * per + 1}-16 unused" if streams * per < 16 else ""})
    return {"values": {}, "io": {cat.FLOW: flow_io, cat.SDI_OUTPUT: sdi_io}}, summary


# ---------------------------------------------------------------- plan / apply

def _fresh_read(rec):
    ip = rec["ip"]
    read = ct.read_device(ip)
    with ct.READS_LOCK:
        ct.READS[ip] = read
    return read


def _words(rows):
    out = []
    for r in rows:
        s = cat.BY_ID[r["setting"]]
        where = f" ({r['instance']})" if r.get("instance") else ""
        out.append({
            "label": r["label"] + where, "group": r["group"], "status": r["status"],
            "current": r.get("current"), "new": r.get("desired"), "danger": r["danger"],
            "unverified": r["evidence"] in ("api",), "error": r.get("error"),
            "verify": r.get("verify"), "readback": r.get("readback"),
        })
    return out


def _check_device(rec, serial):
    facts = rec.get("facts") or {}
    if serial and facts.get("serial") and str(serial) != str(facts["serial"]):
        raise ValueError("serial number mismatch - this is not the device you connected to")


def build_profile(rec, step, inputs):
    if step == "identity":
        return identity_profile(inputs, rec)
    if step == "services":
        return services_profile(effective_site(_overrides(inputs))), {"overrides": _overrides(inputs)}
    if step == "channels":
        return None, {"overrides": _overrides(inputs)}
    raise ValueError("this step writes nothing")


def plan(mac, step, inputs):
    rec = _get(mac)
    if step not in ("identity", "services", "channels"):
        raise ValueError("this step has no preview")
    _require_ready(rec, step)
    read = _fresh_read(rec)
    profile, clean = build_profile(rec, step, inputs or {})
    summary = None
    if step == "channels":
        profile, summary = channels_profile(read, effective_site(clean.get("overrides")))
    rows = ct.plan_device(read, profile, None)
    changes = sum(1 for r in rows if r["status"] == "change")
    return {"rows": _words(rows), "changes": changes, "summary": summary,
            "unsupported": sum(1 for r in rows if r["status"] == "unsupported"),
            "invalid": [r for r in _words(rows) if r["status"] == "invalid"],
            "inputs": clean}


def apply(mac, step, inputs, serial):
    rec = _get(mac)
    if step not in ("identity", "services", "channels"):
        raise ValueError("this step writes nothing")
    _require_ready(rec, step)
    _check_device(rec, serial)
    read = _fresh_read(rec)
    profile, clean = build_profile(rec, step, inputs or {})
    if step == "channels":
        profile, _summary = channels_profile(read, effective_site(clean.get("overrides")))
    if step in ("services", "channels"):
        _update(mac, inputs=dict(rec.get("inputs", {}), **{step: clean}))
    if step == "identity":
        return _apply_identity(mac, rec, read, profile, clean)
    with ct.APPLY_LOCK:
        result = ct.apply_device(rec["ip"], profile, None)
    rows = result["rows"]
    bad = [r for r in rows if r["status"] == "change" and r.get("verify") != "ok"]
    failed_posts = [p for p in result["posts"] if not p["ok"]]
    if not bad and not failed_posts:
        _mark(mac, step, "done", "%d changed, all verified" % sum(1 for r in rows if r["status"] == "change"))
    else:
        _mark(mac, step, "problem", "%d field(s) not confirmed" % (len(bad) + len(failed_posts)))
    return {"rows": _words(rows), "ok": not bad and not failed_posts,
            "rebooted": bool(result.get("rebooted")), "backup": result.get("backup"),
            "posts": [{"path": p["path"], "ok": p["ok"], "response": p["response"]} for p in result["posts"]]}


def _apply_identity(mac, rec, read, profile, clean):
    """Changing the management address moves the device: write, then wait for it
    on its new address instead of reading back from the old one."""
    rows = ct.plan_device(read, profile, None)
    bodies = ct.build_bodies(read, rows)
    if not bodies:
        _update(mac, inputs=dict(rec.get("inputs", {}), identity=clean), ip=clean["red_ip"])
        _mark(mac, "identity", "done", "already correct")
        return {"rows": _words(rows), "ok": True, "alreadyCorrect": True}
    backup = ct.backup(read)
    posts = []
    with ct.APPLY_LOCK:
        for path, body in bodies:
            logger.warning("GUIDE POST %s%s via %s: %s", rec["ip"], ct.API + path, read["network"], json.dumps(body))
            try:
                status, text = ct.http_post(read["address"], path, body)
                ok = 200 <= status < 300 or text.strip().lower() == "ok"
                posts.append({"path": path, "ok": ok, "response": text[:300]})
            except Exception as e:      # the device may drop the connection while it re-addresses itself
                posts.append({"path": path, "ok": None, "response": str(e)})
    new_ip = clean["red_ip"]
    _update(mac, inputs=dict(rec.get("inputs", {}), identity=clean), pendingIp=new_ip)
    _mark(mac, "identity", "written", "waiting for the device on %s" % new_ip)
    return {"rows": _words(rows), "ok": True, "written": True, "newIp": new_ip, "backup": backup, "posts": posts}


def confirm_identity(mac):
    """After the address change: find the device on its new address and check
    that the values really are what was asked for."""
    rec = _get(mac)
    clean = (rec.get("inputs") or {}).get("identity")
    if not clean:
        raise ValueError("nothing to confirm - write the network settings first")
    last = None
    for addr in (clean["red_ip"], clean["blue_ip"]):
        try:
            read = ct.read_device(addr)
        except Exception as e:
            last = e
            continue
        ifs = read["endpoints"].get("self/interfaces") or {}
        host = (read["endpoints"].get("self/ipconfig") or {}).get("hostname")
        mac_now = ((ifs.get("e1") or {}).get("mac_address") or "").lower()
        if mac_now != mac:
            raise ValueError("a different device answers on %s (MAC %s)" % (addr, mac_now))
        problems = []
        if host != clean["name"]:
            problems.append("hostname is '%s'" % host)
        if (ifs.get("e1") or {}).get("static_ip") != f"{clean['red_ip']}/{clean['red_prefix']}":
            problems.append("Red address is %s" % (ifs.get("e1") or {}).get("static_ip"))
        if (ifs.get("e2") or {}).get("static_ip") != f"{clean['blue_ip']}/{clean['blue_prefix']}":
            problems.append("Blue address is %s" % (ifs.get("e2") or {}).get("static_ip"))
        if problems:
            _mark(mac, "identity", "problem", "; ".join(problems))
            return {"ok": False, "problems": problems, "answeredOn": addr}
        with ct.READS_LOCK:
            ct.READS[clean["red_ip"]] = read
        _update(mac, ip=clean["red_ip"], name=clean["name"], tag=clean["tag"], pendingIp=None)
        _mark(mac, "identity", "done", "confirmed on %s (%s)" % (addr, "Red" if addr == clean["red_ip"] else "Blue"))
        return {"ok": True, "answeredOn": addr, "red": addr == clean["red_ip"]}
    return {"ok": False, "waiting": True, "error": str(last)}


# ---------------------------------------------------------------- step 5: checks + hand over

def _check(cid, title, ok, detail, hint=""):
    return {"id": cid, "title": title, "ok": bool(ok), "detail": detail, "hint": "" if ok else hint}


def run_checks(mac):
    rec = _get(mac)
    ip = rec["ip"]
    site = load_site()
    clean = (rec.get("inputs") or {}).get("identity") or {}
    checks = []
    addrs = {}
    blue = clean.get("blue_ip") or _shift_second_octet(ip, site["network"]["blue_octet_offset"])
    for net, a in (("Red", ip), ("Blue", blue)):
        try:
            ct.http_get(a, "")
            addrs[net] = a
        except Exception:
            pass
    checks.append(_check("reach", "Reachable on Red and Blue", len(addrs) == 2,
                         "answers on: " + (", ".join(f"{k} {v}" for k, v in addrs.items()) or "nothing"),
                         "Check the cable and the switch port of the network that does not answer."))
    addr = addrs.get("Red") or addrs.get("Blue")
    if not addr:
        return checks
    node = {}
    try:
        node = ct.http_get(addr, "telemetry/node") or {}
    except Exception:
        pass
    ifs = node.get("interfaces") or {}
    for key, label in (("e1", "Red"), ("e2", "Blue")):
        st = (ifs.get(key) or {}).get("link_status")
        checks.append(_check("link_" + key, f"{label} link is up", st == "up", f"link status: {st or 'unknown'}",
                             "Check the fibre / DAC and the switch port."))
    ptp = (node.get("refclk") or {}).get("status")
    checks.append(_check("ptp", "PTP is locked", ptp == "locked", f"PTP status: {ptp or 'unknown'}",
                         "Check the PTP domain (site settings) and that the grandmaster is reachable on this VLAN."))
    try:
        nmos = ct.http_get(addr, "self/diag/nmos") or {}
    except Exception:
        nmos = {}
    checks.append(_check("nmos", "Registered in NMOS", nmos.get("status") == "connected",
                         f"NMOS status: {nmos.get('status') or 'unknown'}",
                         "Check the registry address (site settings) and the route to it."))
    try:
        lldp = ct.http_get(addr, "lldp") or {}
        seen = [n for n in lldp.get("neighbor", []) if n.get("chassis") and n.get("interface") in (1, 2)]
    except Exception:
        seen = []
    checks.append(_check("lldp", "Switch neighbours seen (LLDP)", len(seen) >= 1,
                         "; ".join(f"if {n['interface']}: {n['chassis']} {n.get('port', '')}" for n in seen) or "none",
                         "No LLDP neighbour: the switch port may be down or LLDP is off on the switch."))
    temp = ((node.get("health") or {}).get("core_temp"))
    checks.append(_check("temp", "Temperature is normal", isinstance(temp, (int, float)) and temp < 70,
                         f"core temperature: {temp if temp is not None else 'unknown'} C",
                         "70 C or more: check airflow and fans."))
    try:
        warn = []
        for g in ct.http_get(addr, "telemetry/warnings/") or []:
            data = ct.http_get(addr, "telemetry/warnings/" + str(g).rstrip("/")) or {}
            warn += [w for w in data.get("warning", []) if w.get("code") != 1001]
    except Exception:
        warn = []
    checks.append(_check("warn", "No active warnings", not warn, f"{len(warn)} warning(s)" if warn else "none",
                         "Open the device in the dashboard to see the warning details."))
    try:
        ipc = ct.http_get(addr, "self/ipconfig") or {}
        sysl = ct.http_get(addr, "self/syslog") or {}
    except Exception:
        ipc, sysl = {}, {}
    if clean:
        checks.append(_check("name", "Hostname is as planned", ipc.get("hostname") == clean.get("name"),
                             f"hostname: {ipc.get('hostname')}", "Repeat the 'Name & network' step."))
    cfg = (sysl.get("config") or {})
    checks.append(_check("syslog", "Syslog points to the site server",
                         str(cfg.get("server")) == site["syslog"]["server"] and str(cfg.get("enable")) in ("True", "true", "1"),
                         f"syslog: {cfg.get('server')} ({'on' if str(cfg.get('enable')) in ('True', 'true', '1') else 'off'})",
                         "Repeat the 'Time & services' step."))
    return checks


def hand_over(mac, checks_ok_ack=False):
    rec = _get(mac)
    _require_ready(rec, "verify")
    checks = run_checks(mac)
    failed = [c for c in checks if not c["ok"]]
    if failed and not checks_ok_ack:
        return {"ok": False, "checks": checks, "needsAck": True}
    name = rec.get("name") or ((rec.get("inputs") or {}).get("identity") or {}).get("name") or ""
    tag = rec.get("tag") or ((rec.get("inputs") or {}).get("identity") or {}).get("tag") or ""
    _add_device(rec["ip"], name, tag)
    _mark(mac, "verify", "done", "%d of %d checks passed; added to the dashboard" % (len(checks) - len(failed), len(checks)))
    return {"ok": True, "checks": checks, "added": True}


# ---------------------------------------------------------------- HTTP routes

class GuideRoutes:
    """Mixed into the dashboard handler next to ConfigRoutes (same login):
    /guide and /api/guide/*. guide_get / guide_post return True when handled."""

    def guide_get(self):
        path = self.path.split("?")[0]
        if not (path == "/guide" or path.startswith("/api/guide/")):
            return False
        if not self._cfg_authorized():
            self._cfg_deny()
            return True
        if path == "/guide":
            self._cfg_html(UI_FILE.read_text(encoding="utf-8"))
        elif path == "/api/guide/site":
            self._cfg_json({"site": load_site(), "steps": STEPS})
        elif path == "/api/guide/devices":
            self._cfg_json({"devices": [_public(r) for r in devices_state().values()]})
        else:
            self.send_error(404)
        return True

    def guide_post(self):
        path = self.path.split("?")[0]
        if not path.startswith("/api/guide/"):
            return False
        if not self._cfg_authorized():
            self._cfg_deny()
            return True
        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._cfg_json({"ok": False, "error": "invalid JSON"}, 400)
            return True
        try:
            self._cfg_json(self._guide_route(path, data))
        except ValueError as e:
            self._cfg_json({"ok": False, "error": str(e)}, 400)
        except Exception as e:
            logger.exception("guide request %s failed", path)
            self._cfg_json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, 500)
        return True

    def _guide_route(self, p, d):
        if p == "/api/guide/site":
            return {"ok": True, "site": save_site(d.get("site") or {})}
        if p == "/api/guide/factory":
            return {"ok": True, **factory_addresses(d.get("mac"))}
        if p == "/api/guide/scan":
            return {"ok": True, **scan(d.get("network"))}
        if p == "/api/guide/netcheck":
            local = FORCE_LOCAL or self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")
            return {"ok": True, **host_network(str(d.get("ip") or ""), d.get("prefix"), local)}
        if p == "/api/guide/connect":
            return {"ok": True, "device": connect(d.get("ip"))}
        if p == "/api/guide/device":
            rec = _get(str(d.get("mac") or "").lower())
            return {"ok": True, "device": _public(rec), "identityDefaults": identity_defaults(rec)}
        if p == "/api/guide/plan":
            return {"ok": True, **plan(str(d.get("mac") or "").lower(), d.get("step"), d.get("inputs"))}
        if p == "/api/guide/apply":
            if d.get("confirm") is not True:
                raise ValueError("confirmation missing")
            return {"ok": True, **apply(str(d.get("mac") or "").lower(), d.get("step"), d.get("inputs"), d.get("serial"))}
        if p == "/api/guide/confirm-identity":
            return {"ok": True, **confirm_identity(str(d.get("mac") or "").lower())}
        if p == "/api/guide/forget":
            forget(str(d.get("mac") or "").lower())
            return {"ok": True}
        if p == "/api/guide/checks":
            return {"ok": True, "checks": run_checks(str(d.get("mac") or "").lower())}
        if p == "/api/guide/hand-over":
            return {"ok": True, **hand_over(str(d.get("mac") or "").lower(), bool(d.get("acknowledge")))}
        raise ValueError("unknown request")
