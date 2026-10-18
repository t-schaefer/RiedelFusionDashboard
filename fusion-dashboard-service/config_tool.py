"""
Batch configuration of MuoN cards and Fusion gateways, built into the dashboard.

Moved here from the stand-alone MuoN config tool (RiedelMuoNConfig); the
logic is unchanged: nothing is written without plan -> preview -> apply, a
setting is only planned if the device's own GET response contains the field,
a full backup is saved before the first write, and every changed field is
read back and compared. What changed: it no longer runs its own web server -
server.py routes /config and /api/config/* here (see ConfigRoutes) - the
device list is the dashboard's, and the login password lives in auth.json
next to server.py (set it with `python server.py --set-password`).
"""

import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs

import config_catalog as cat

BASE_DIR = Path(__file__).resolve().parent
PROFILES_DIR = BASE_DIR / "config-profiles"
BACKUPS_DIR = BASE_DIR / "config-backups"
UI_FILE = BASE_DIR / "config_ui.html"
AUTH_FILE = BASE_DIR / "auth.json"

API = "/emsfp/node/v1/"
READ_CONCURRENCY = 8
APPLY_CONCURRENCY = 4
TIMEOUT = 6
READBACK_WAIT_SEC = 120
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

logger = logging.getLogger("fusion-dashboard")

SESSION_COOKIE = "fusion_config_session"
SESSION_TTL_SEC = 12 * 3600
SESSIONS = {}  # token -> expiry (epoch); in memory, a restart logs everyone out
FAILED_LOGINS = {}  # client ip -> (count, last attempt)
AUTH_LOCK = threading.Lock()

READS = {}  # ip -> last device read (see read_device)
READS_LOCK = threading.RLock()
APPLY_LOCK = threading.Lock()  # one apply at a time
MISSING = object()

_load_devices = lambda: []


def init(load_devices):
    """server.py hands over its device list loader (the dashboard's devices.json)."""
    global _load_devices
    _load_devices = load_devices
    PROFILES_DIR.mkdir(exist_ok=True)
    BACKUPS_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------- HTTP to devices

def http_get(ip, path):
    req = urllib.request.Request("http://" + ip + API + path, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
        body = res.read().decode("utf-8", errors="replace")
    try:
        return json.loads(body)
    except ValueError:
        return body


def http_post(ip, path, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request("http://" + ip + API + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            return res.status, res.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def derive_blue_ip(red_ip):
    parts = red_ip.split(".")
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        return None
    parts[1] = str(int(parts[1]) + 1)
    return ".".join(parts)


def reachable_address(ip):
    """Returns (address, 'red'|'blue') of whichever network answers."""
    try:
        http_get(ip, "")
        return ip, "red"
    except Exception as red_err:
        blue = derive_blue_ip(ip)
        if not blue:
            raise
        try:
            http_get(blue, "")
            return blue, "blue"
        except Exception:
            raise red_err


# ---------------------------------------------------------------- reading

def _children(ip, path):
    data = http_get(ip, path)
    return [str(x).rstrip("/") for x in data] if isinstance(data, list) else []


def read_device(ip):
    """Reads everything the catalog can touch. Returns the read record."""
    addr, net = reachable_address(ip)
    root = set(_children(addr, ""))
    endpoints, errors = {}, {}

    def grab(path):
        try:
            endpoints[path] = http_get(addr, path)
        except Exception as e:
            errors[path] = str(e)

    for path in ("self/information", "self/firmware", "self/license", "self/ipconfig", "self/interfaces",
                 "self/static_route", "self/syslog", "self/protocols", "self/system", "self/diag/nmos",
                 "self/phy"):
        grab(path)
    for path in ("refclk", "lldp", "sdi"):
        if path in root:
            grab(path)

    instances = {scope: [] for scope in cat.SCOPES}

    # channel (NMOS device) labels, for naming clean-switch instances
    device_labels = {}
    if "devices" in root:
        try:
            for d in http_get(addr, "devices/") or []:
                if isinstance(d, dict):
                    device_labels[d.get("id")] = d.get("label") or d.get("id")
        except Exception as e:
            errors["devices/"] = str(e)

    def collect(scope, collection, key_fn):
        if collection not in root:
            return
        try:
            ids = _children(addr, collection + "/")
        except Exception as e:
            errors[collection + "/"] = str(e)
            return
        for iid in ids:
            path = f"{collection}/{iid}"
            grab(path)
            data = endpoints.get(path)
            if isinstance(data, dict):
                instances[scope].append({"key": key_fn(iid, data), "path": path, "id": iid})

    collect(cat.SDI_OUTPUT, "sdi_output", lambda iid, d: d.get("label") or iid)
    collect(cat.CLEAN_SWITCH, "clean_switch", lambda iid, d: device_labels.get(iid, iid))
    collect(cat.FLOW, "flows", lambda iid, d: d.get("name") or d.get("label") or iid)
    collect(cat.PORT, "port", lambda iid, d: "Port " + iid)

    refclk = endpoints.get("refclk")
    if isinstance(refclk, dict):
        for n, uuid in enumerate(refclk.get("uuid") or []):
            path = f"refclk/{uuid}"
            grab(path)
            if isinstance(endpoints.get(path), dict):
                instances[cat.PTP].append({"key": f"PTP {n + 1}", "path": path, "id": uuid})

    for scope in instances:
        instances[scope].sort(key=lambda i: natural_key(i["key"]))

    info = endpoints.get("self/information") or {}
    ipconfig = endpoints.get("self/ipconfig") or {}
    return {
        "ip": ip,
        "address": addr,
        "network": net,
        "readAt": datetime.now().isoformat(timespec="seconds"),
        "summary": {
            "hostname": ipconfig.get("hostname"),
            "type": info.get("type"),
            "baseType": info.get("base_type"),
            "serial": info.get("serial_number"),
            "firmware": firmware_version(endpoints.get("self/firmware")),
        },
        "endpoints": endpoints,
        "instances": instances,
        "errors": errors,
    }


def firmware_version(fw):
    for slot in (fw or {}).get("info", []) if isinstance(fw, dict) else []:
        if slot.get("active") == "yes":
            return slot.get("semantic_version") or slot.get("version")
    return None


def natural_key(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", str(s))]


# ---------------------------------------------------------------- values

def get_in(data, path):
    cur = data
    for k in path:
        if isinstance(k, int):
            # flows/{id}.network is a list of legs on some flows (video
            # receivers) and a single flat object on the others; treat the
            # flat object as leg 0.
            if k == 0 and isinstance(cur, dict):
                continue
            if not isinstance(cur, list) or k >= len(cur):
                return MISSING
            cur = cur[k]
        else:
            if not isinstance(cur, dict) or k not in cur:
                return MISSING
            cur = cur[k]
    return cur


def set_in(obj, path, value):
    cur = obj
    for k in path[:-1]:
        cur = cur.setdefault(k, {})
    cur[path[-1]] = value


def coerce(setting, desired, current):
    """Brings a profile value into the wire type the device itself uses."""
    if desired is None:
        return None
    t = setting["type"]
    if t == cat.BOOL01 or isinstance(current, str) and current in ("0", "1") and t == "bool":
        truthy = desired in (True, 1, "1", "true", "True", "on", "yes")
        return "1" if truthy else "0"
    if isinstance(current, bool):
        return desired in (True, 1, "1", "true", "True", "on", "yes")
    if isinstance(current, int) and not isinstance(current, bool):
        try:
            return int(desired)
        except (TypeError, ValueError):
            raise ValueError(f"{setting['id']}: '{desired}' is not a number")
    if isinstance(current, float):
        return float(desired)
    return str(desired)


def same(a, b):
    return str(a).strip().lower() == str(b).strip().lower()


def audiomap_to_ids(value, flows_by_name):
    """'<flow name>:<ch>:<en>' -> '<flow uuid>:<ch>:<en>' (uuid form passes through)."""
    s = str(value or "")
    if s == "":
        return ":0:0"
    parts = s.rsplit(":", 2)
    if len(parts) != 3:
        raise ValueError(f"audio map '{s}' is not <flow>:<channel>:<enable>")
    ref, ch, en = parts
    if ref == "" or UUID_RE.match(ref):
        return s
    if ref not in flows_by_name:
        raise ValueError(f"audio map references unknown flow '{ref}'")
    return f"{flows_by_name[ref]}:{ch}:{en}"


def audiomap_to_names(value, flows_by_id):
    s = str(value or "")
    parts = s.rsplit(":", 2)
    if len(parts) == 3 and parts[0] in flows_by_id:
        return f"{flows_by_id[parts[0]]}:{parts[1]}:{parts[2]}"
    return s


def instances_for(read, setting):
    if setting["scope"] == cat.DEVICE:
        return [{"key": None, "path": setting["endpoint"]}]
    return read["instances"].get(setting["scope"], [])


def desired_value(setting, inst_key, profile, override):
    """Merge order (last wins): profile batch value < profile per-I/O value <
    device value < device per-I/O value."""
    sid, scope = setting["id"], setting["scope"]
    found = MISSING
    for layer in (profile or {}, override or {}):
        vals = layer.get("values") or {}
        if sid in vals and vals[sid] not in (None, ""):
            found = vals[sid]
        if inst_key is not None:
            io = ((layer.get("io") or {}).get(scope) or {}).get(inst_key) or {}
            if sid in io and io[sid] not in (None, ""):
                found = io[sid]
    return found


def current_values(read):
    """Flattened current values for the UI: {settingId: value} for device scope
    and {scope: {instanceKey: {settingId: value}}} for I/O scopes."""
    flows_by_id = {i["id"]: i["key"] for i in read["instances"].get(cat.FLOW, [])}
    values, io = {}, {}
    for s in cat.CATALOG:
        for inst in instances_for(read, s):
            data = read["endpoints"].get(inst["path"])
            v = get_in(data, s["field"]) if data is not None else MISSING
            if v is MISSING:
                continue
            if s["type"] == "audiomap":
                v = audiomap_to_names(v, flows_by_id)
            if inst["key"] is None:
                values[s["id"]] = v
            else:
                io.setdefault(s["scope"], {}).setdefault(inst["key"], {})[s["id"]] = v
    return {"values": values, "io": io}


# ---------------------------------------------------------------- planning

def plan_device(read, profile, override, groups=None):
    """groups: optional list of catalog groups to limit the plan to (the
    UI's "apply only this category")."""
    flows_by_name = {i["key"]: i["id"] for i in read["instances"].get(cat.FLOW, [])}
    rows = []
    for s in cat.CATALOG:
        if groups and s["group"] not in groups:
            continue
        for inst in instances_for(read, s):
            want = desired_value(s, inst["key"], profile, override)
            if want is MISSING:
                continue
            data = read["endpoints"].get(inst["path"])
            cur = get_in(data, s["field"]) if isinstance(data, (dict, list)) else MISSING
            row = {"setting": s["id"], "label": s["label"], "group": s["group"], "instance": inst["key"],
                   "path": inst["path"], "danger": s["danger"], "evidence": s["evidence"]}
            if cur is MISSING:
                rows.append({**row, "status": "unsupported", "current": None, "desired": want})
                continue
            try:
                if s["type"] == "audiomap":
                    wire = audiomap_to_ids(want, flows_by_name)
                else:
                    wire = coerce(s, want, cur)
            except ValueError as e:
                rows.append({**row, "status": "invalid", "current": cur, "desired": want, "error": str(e)})
                continue
            status = "unchanged" if same(cur, wire) else "change"
            rows.append({**row, "status": status, "current": cur, "desired": wire})
    return rows


def endpoint_rank(path):
    for n, prefix in enumerate(cat.ENDPOINT_ORDER):
        head = path.split("/")[0]
        if path == prefix or head == prefix or (prefix == "flow" and head == "flows"):
            return n
    return len(cat.ENDPOINT_ORDER)


def build_bodies(read, rows):
    """Groups 'change' rows into one POST body per endpoint path."""
    bodies = {}
    for r in rows:
        if r["status"] != "change":
            continue
        s = cat.BY_ID[r["setting"]]
        body = bodies.setdefault(r["path"], {})
        set_in(body, s["body"] or s["field"], r["desired"])
        endpoint = s["endpoint"] or ""
        for parent, extra in cat.BODY_EXTRAS.get(endpoint, {}).items():
            if tuple(s["field"][:len(parent)]) == parent:
                for k, v in extra.items():
                    set_in(body, list(parent) + [k], v)
        for parent in cat.FULL_PARENTS.get(endpoint, []):
            if s["field"][:len(parent)] != parent:
                continue
            data = read["endpoints"].get(r["path"])
            for other in cat.CATALOG:
                if other["endpoint"] == endpoint and other["field"][:len(parent)] == parent:
                    if get_in(body, other["field"]) is MISSING:
                        v = get_in(data, other["field"])
                        if v is not MISSING:
                            set_in(body, other["field"], v)
    return sorted(bodies.items(), key=lambda kv: endpoint_rank(kv[0]))


# ---------------------------------------------------------------- applying

def uptime_seconds(system):
    """'0 days, 00:01:07' -> 67"""
    m = re.match(r"\s*(\d+)\s+days?,\s*(\d+):(\d+):(\d+)", str((system or {}).get("uptime", "")))
    if not m:
        return None
    d, h, mi, s = (int(x) for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + s


def backup(read):
    BACKUPS_DIR.mkdir(exist_ok=True)
    name = f"{read['ip']}_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    (BACKUPS_DIR / name).write_text(json.dumps(read, indent=1), encoding="utf-8")
    return name


def apply_device(ip, profile, override, groups=None):
    """Fresh read -> plan -> backup -> POST per endpoint -> read back and verify."""
    started = time.time()
    read = read_device(ip)
    with READS_LOCK:
        READS[ip] = read
    rows = plan_device(read, profile, override, groups)
    bodies = build_bodies(read, rows)
    result = {"ip": ip, "network": read["network"], "posts": [], "rows": rows, "backup": None}
    if not bodies:
        return result
    result["backup"] = backup(read)
    addr = read["address"]
    for path, body in bodies:
        logger.warning("POST %s%s via %s: %s", ip, API + path, read["network"], json.dumps(body))
        try:
            status, text = http_post(addr, path, body)
            ok = 200 <= status < 300 or text.strip().lower() == "ok"
            result["posts"].append({"path": path, "body": body, "status": status, "response": text[:300], "ok": ok})
        except Exception as e:
            logger.error("POST %s%s failed: %s", ip, path, e)
            result["posts"].append({"path": path, "body": body, "status": None, "response": str(e), "ok": False})
    time.sleep(1.0)  # let the device settle before reading back
    # Some writes (mDNS, IGMP version) make the card stop answering and
    # reboot. Keep retrying the read-back for a while instead of giving up.
    deadline = time.time() + READBACK_WAIT_SEC
    fresh = {}

    def readback(path):
        while True:
            try:
                return http_get(addr, path)
            except Exception as e:
                if time.time() >= deadline:
                    return e
                result["unreachable"] = True
                time.sleep(5)

    for r in rows:
        if r["status"] != "change":
            continue
        s = cat.BY_ID[r["setting"]]
        if r["path"] not in fresh:
            fresh[r["path"]] = readback(r["path"])
        data = fresh[r["path"]]
        if isinstance(data, Exception):
            r["verify"] = "unreadable"
            r["readback"] = str(data)
            continue
        got = get_in(data, s["field"])
        r["readback"] = None if got is MISSING else got
        r["verify"] = "ok" if got is not MISSING and same(got, r["desired"]) else "mismatch"
    try:
        up_after = uptime_seconds(http_get(addr, "self/system"))
        if up_after is not None and up_after < time.time() - started:
            result["rebooted"] = True
            logger.warning("%s rebooted during apply (uptime now %ss)", ip, up_after)
    except Exception:
        pass
    ok = sum(1 for r in rows if r.get("verify") == "ok")
    changed = sum(1 for r in rows if r["status"] == "change")
    logger.warning("apply %s: %d/%d changed fields verified, backup %s", ip, ok, changed, result["backup"])
    return result


# ---------------------------------------------------------------- profiles

def safe_name(name):
    name = re.sub(r"[^A-Za-z0-9_.\- ]", "_", str(name or "")).strip()
    if not name:
        raise ValueError("name required")
    return name


def list_profiles():
    PROFILES_DIR.mkdir(exist_ok=True)
    # Seed the editable (git-ignored) site-default.json from the tracked
    # example once, so `git pull` never overwrites a site's own defaults.
    seed = PROFILES_DIR / "site-default.json"
    example = PROFILES_DIR / "site-default.example.json"
    if not seed.exists() and example.exists():
        seed.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    return sorted(p.stem for p in PROFILES_DIR.glob("*.json"))


def capture(read, batch_only):
    """Turns a device read into a profile ({values, io}). batch_only keeps
    only settings that make sense on many devices."""
    cur = current_values(read)
    if batch_only:
        values = {k: v for k, v in cur["values"].items() if cat.BY_ID[k]["batch"]}
        io = {}
        for scope, insts in cur["io"].items():
            # A per-I/O setting that has the same value on every instance
            # becomes a plain batch value, so it also applies to devices whose
            # I/Os are named differently (e.g. a MuoN vs. a Fusion).
            per_setting = {}
            for key, vals in insts.items():
                for sid, v in vals.items():
                    if cat.BY_ID[sid]["batch"]:
                        per_setting.setdefault(sid, {})[key] = v
            for sid, by_inst in per_setting.items():
                distinct = {json.dumps(v) for v in by_inst.values()}
                if len(distinct) == 1:
                    values[sid] = next(iter(by_inst.values()))
                else:
                    for key, v in by_inst.items():
                        io.setdefault(scope, {}).setdefault(key, {})[sid] = v
        cur = {"values": values, "io": io}
    return cur


# ---------------------------------------------------------------- login

def hash_password(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000).hex()


def set_password(password):
    salt = secrets.token_bytes(16)
    AUTH_FILE.write_text(json.dumps({"salt": salt.hex(), "hash": hash_password(password, salt)}), encoding="utf-8")


def password_configured():
    return AUTH_FILE.exists()


def check_password(password):
    try:
        auth = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
        return hmac.compare_digest(hash_password(password, bytes.fromhex(auth["salt"])), auth["hash"])
    except Exception:
        return False


def new_session():
    token = secrets.token_urlsafe(32)
    with AUTH_LOCK:
        SESSIONS[token] = time.time() + SESSION_TTL_SEC
    return token


def session_valid(token):
    with AUTH_LOCK:
        exp = SESSIONS.get(token)
        if not exp or exp < time.time():
            SESSIONS.pop(token, None)
            return False
        SESSIONS[token] = time.time() + SESSION_TTL_SEC  # sliding expiry
        return True


def login_delay(client):
    """Seconds this client must still wait after repeated wrong passwords."""
    count, last = FAILED_LOGINS.get(client, (0, 0))
    if count < 5:
        return 0
    return max(0, int(last + min(300, 15 * (count - 4)) - time.time()))


LOGIN_PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Device Config - Login</title>
<style>
 :root{--bg:#f4f5f7;--panel:#fff;--text:#1d2330;--muted:#6b7385;--line:#dde1e8;--accent:#2563eb;--bad:#b91c1c}
 @media (prefers-color-scheme: dark){:root{--bg:#13161c;--panel:#1b1f27;--text:#e4e7ee;--muted:#8d95a8;--line:#2c323e;--accent:#6d9bff;--bad:#f87171}}
 body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--text);font:14px system-ui,"Segoe UI",sans-serif}
 form{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:24px;width:min(340px,calc(100vw - 32px))}
 h1{font-size:17px;margin:0 0 4px} p{color:var(--muted);margin:0 0 16px;font-size:13px}
 input{width:100%;box-sizing:border-box;padding:8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:inherit;font:inherit}
 button{margin-top:12px;width:100%;padding:8px;border:0;border-radius:6px;background:var(--accent);color:#fff;font:inherit;cursor:pointer}
 .err{color:var(--bad);margin:10px 0 0;font-size:13px}
</style></head><body><form method="post" action="/config/login">
<h1>Device Config</h1><p>Writes configuration to broadcast devices. Password required.</p>
<input type="password" name="password" placeholder="Password" autofocus autocomplete="current-password">
<button type="submit">Log in</button>__ERROR__</form></body></html>"""

NO_PASSWORD_PAGE = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Device Config</title></head>
<body style="font:14px system-ui;padding:32px"><h1 style="font-size:17px">No password set</h1>
<p>Remote access stays locked until a password is set. On the server, double-click
<b>Set-ConfigPassword-FusionDashboard.bat</b> (or run <code>python fusion-dashboard-service\\server.py --set-password</code>).</p></body></html>"""


# ---------------------------------------------------------------- HTTP API

def read_many(ips):
    def one(ip):
        try:
            r = read_device(ip)
            with READS_LOCK:
                READS[ip] = r
            return ip, {"ok": True}
        except Exception as e:
            return ip, {"ok": False, "error": str(e)}

    with ThreadPoolExecutor(max_workers=READ_CONCURRENCY) as pool:
        return dict(pool.map(one, ips))


def public_read(r):
    return {k: r[k] for k in ("ip", "network", "readAt", "summary", "errors")} | {
        "instances": {s: [i["key"] for i in lst] for s, lst in r["instances"].items()},
        "current": current_values(r),
    }



class ConfigRoutes:
    """Mixed into the dashboard's request handler: serves the config page
    (/config), its login (/config/login, /config/logout) and /api/config/*.
    config_get / config_post return True when they handled the request."""

    def _cfg_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _cfg_html(self, html, status=200, headers=None):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _cfg_redirect(self, where, headers=None):
        self.send_response(303)
        self.send_header("Location", where)
        self.send_header("Content-Length", "0")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _cfg_is_local(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    def _cfg_token(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == SESSION_COOKIE:
                return v
        return ""

    def _cfg_authorized(self):
        """Password set -> everyone needs a session. No password yet ->
        only this machine itself may use the config page."""
        if not password_configured():
            return self._cfg_is_local()
        return session_valid(self._cfg_token())

    def _cfg_deny(self):
        path = self.path.split("?")[0]
        if not password_configured():
            if path.startswith("/api/"):
                self._cfg_json({"ok": False, "error": "no password set - remote access locked"}, 403)
            else:
                self._cfg_html(NO_PASSWORD_PAGE, 403)
        elif path.startswith("/api/"):
            self._cfg_json({"ok": False, "error": "login required", "login": True}, 401)
        else:
            self._cfg_redirect("/config/login")

    def _cfg_login_post(self):
        client = self.client_address[0]
        length = int(self.headers.get("Content-Length", 0))
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        wait = login_delay(client)
        if wait:
            self._cfg_html(LOGIN_PAGE.replace("__ERROR__", f'<p class="err">Too many attempts - wait {wait} s.</p>'), 429)
            return
        if password_configured() and check_password((form.get("password") or [""])[0]):
            FAILED_LOGINS.pop(client, None)
            logger.warning("config login from %s", client)
            cookie = f"{SESSION_COOKIE}={new_session()}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_TTL_SEC}"
            self._cfg_redirect("/config", {"Set-Cookie": cookie})
            return
        count, _ = FAILED_LOGINS.get(client, (0, 0))
        FAILED_LOGINS[client] = (count + 1, time.time())
        logger.warning("failed config login from %s (%d)", client, count + 1)
        self._cfg_html(LOGIN_PAGE.replace("__ERROR__", '<p class="err">Wrong password.</p>'), 401)

    def config_get(self):
        path = self.path.split("?")[0]
        if not (path in ("/config", "/config/") or path.startswith("/config/") or path.startswith("/api/config/")):
            return False
        if path == "/config/login":
            if not password_configured():
                self._cfg_redirect("/config")
            else:
                self._cfg_html(LOGIN_PAGE.replace("__ERROR__", ""))
            return True
        if path == "/config/logout":
            with AUTH_LOCK:
                SESSIONS.pop(self._cfg_token(), None)
            self._cfg_redirect("/config/login", {"Set-Cookie": f"{SESSION_COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"})
            return True
        if not self._cfg_authorized():
            self._cfg_deny()
            return True
        if path in ("/config", "/config/"):
            self._cfg_html(UI_FILE.read_text(encoding="utf-8"))
        elif path == "/api/config/catalog":
            self._cfg_json({"catalog": cat.CATALOG, "scopes": cat.SCOPES, "auth": password_configured()})
        elif path == "/api/config/devices":
            with READS_LOCK:
                reads = {ip: public_read(r) for ip, r in READS.items()}
            self._cfg_json({"devices": _load_devices(), "reads": reads})
        elif path == "/api/config/profiles":
            self._cfg_json({"profiles": list_profiles()})
        elif path == "/api/config/backups":
            BACKUPS_DIR.mkdir(exist_ok=True)
            self._cfg_json({"backups": sorted((p.name for p in BACKUPS_DIR.glob("*.json")), reverse=True)})
        else:
            self.send_error(404)
        return True

    def config_post(self):
        path = self.path.split("?")[0]
        if path == "/config/login":
            self._cfg_login_post()
            return True
        if not path.startswith("/api/config/"):
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
            self._cfg_route(path, data)
        except ValueError as e:
            self._cfg_json({"ok": False, "error": str(e)}, 400)
        except Exception as e:
            logger.exception("config request %s failed", path)
            self._cfg_json({"ok": False, "error": str(e)}, 500)
        return True

    def _cfg_route(self, p, data):
        if p == "/api/config/read":
            self._cfg_json({"ok": True, "results": read_many(list(data.get("ips") or []))})
        elif p == "/api/config/plan":
            profile, overrides = data.get("profile") or {}, data.get("overrides") or {}
            out = {}
            for ip in data.get("ips") or []:
                with READS_LOCK:
                    r = READS.get(ip)
                if r is None:
                    out[ip] = {"error": "not read yet - read the device first"}
                    continue
                rows = plan_device(r, profile, overrides.get(ip), data.get("groups"))
                out[ip] = {"rows": rows, "posts": [{"path": pth, "body": b} for pth, b in build_bodies(r, rows)]}
            self._cfg_json({"ok": True, "plan": out})
        elif p == "/api/config/apply":
            if data.get("confirm") != "APPLY":
                raise ValueError("apply needs confirm=APPLY")
            if not APPLY_LOCK.acquire(blocking=False):
                raise ValueError("another apply is still running")
            try:
                profile, overrides = data.get("profile") or {}, data.get("overrides") or {}
                ips = list(data.get("ips") or [])
                logger.warning("CONFIG APPLY to %d device(s): %s", len(ips), ips)

                def one(ip):
                    try:
                        return ip, apply_device(ip, profile, overrides.get(ip), data.get("groups"))
                    except Exception as e:
                        logger.error("config apply %s failed: %s", ip, e)
                        return ip, {"ip": ip, "error": str(e)}

                with ThreadPoolExecutor(max_workers=APPLY_CONCURRENCY) as pool:
                    results = dict(pool.map(one, ips))
            finally:
                APPLY_LOCK.release()
            self._cfg_json({"ok": True, "results": results})
        elif p == "/api/config/profiles/load":
            f = PROFILES_DIR / (safe_name(data.get("name")) + ".json")
            self._cfg_json({"ok": True, "profile": json.loads(f.read_text(encoding="utf-8"))})
        elif p == "/api/config/profiles/save":
            PROFILES_DIR.mkdir(exist_ok=True)
            f = PROFILES_DIR / (safe_name(data.get("name")) + ".json")
            f.write_text(json.dumps(data.get("profile") or {}, indent=2), encoding="utf-8")
            self._cfg_json({"ok": True})
        elif p == "/api/config/profiles/delete":
            (PROFILES_DIR / (safe_name(data.get("name")) + ".json")).unlink(missing_ok=True)
            self._cfg_json({"ok": True})
        elif p == "/api/config/capture":
            ip = data.get("ip")
            with READS_LOCK:
                r = READS.get(ip)
            if r is None:
                raise ValueError("read the device first")
            self._cfg_json({"ok": True, "profile": capture(r, bool(data.get("batchOnly", True)))})
        elif p == "/api/config/backups/load":
            name = Path(str(data.get("name") or "")).name
            r = json.loads((BACKUPS_DIR / name).read_text(encoding="utf-8"))
            self._cfg_json({"ok": True, "ip": r["ip"], "profile": capture(r, batch_only=False)})
        else:
            self.send_error(404)
