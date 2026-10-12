#!/usr/bin/env python3
"""
Fusion Fleet Dashboard - background monitoring service.

Runs unattended (e.g. as a Windows Scheduled Task under the SYSTEM account,
see install-task-windows.ps1) and polls a fleet of Nevion/Macnica Fusion
ST2110 devices, exposing the current health/config snapshot over a small
built-in web server so anyone on the network can view it in a browser
without needing to be logged into this machine.

Standard library only - nothing to pip install.

Polling design (why it's built this way - matters at 100+ devices):
- Health comes from one endpoint per device (/telemetry/node + a warnings
  lookup) instead of the many small GETs the config page uses.
- A ThreadPoolExecutor caps how many devices are polled at once (CONCURRENCY)
  so a large fleet never floods the network with simultaneous requests.
- Each poll sweep runs to completion in its own thread before the next one
  is scheduled, so sweeps never overlap even if some devices are slow.
- Every device request has a hard timeout.
- Config data (hostname, firmware, license, ports, NMOS) is refreshed on a
  much slower interval than health, since it rarely changes.
"""

import gzip
import json
import logging
import logging.handlers
import re
import threading
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

BASE_DIR = Path(__file__).resolve().parent
DEVICES_FILE = BASE_DIR / "devices.json"
DEVICES_EXAMPLE_FILE = BASE_DIR / "devices.example.json"
SEEDED_FILE = BASE_DIR / "devices.seeded.json"
DASHBOARD_FILE = BASE_DIR / "dashboard.html"
LOG_FILE = BASE_DIR / "service.log"

HOST = "0.0.0.0"
PORT = 8090
POLL_INTERVAL_SEC = 30
CONFIG_REFRESH_INTERVAL_SEC = 600
CONCURRENCY = 8
REQUEST_TIMEOUT_SEC = 4
CONFIG_TIMEOUT_SEC = 5

logger = logging.getLogger("fusion-dashboard")
logger.setLevel(logging.INFO)
_file_handler = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3)
_file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_file_handler)
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_console_handler)

STATE_LOCK = threading.RLock()
STATE = {}  # ip -> device record
INFLIGHT = set()  # ips currently being health-polled
POLLING_ENABLED = threading.Event()
POLLING_ENABLED.set()  # polling starts enabled


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_devices():
    if not DEVICES_FILE.exists() and DEVICES_EXAMPLE_FILE.exists():
        # First run on a fresh checkout: seed the local (git-ignored) device
        # list from the tracked example, so `git pull` never overwrites a
        # site's own device list once it exists.
        DEVICES_FILE.write_text(DEVICES_EXAMPLE_FILE.read_text(encoding="utf-8"), encoding="utf-8")
        logger.info("devices.json not found - seeded it from devices.example.json")
    with open(DEVICES_FILE, "r", encoding="utf-8") as f:
        devices = json.load(f)
    return merge_new_seed_devices(devices)


def merge_new_seed_devices(devices):
    """Append devices that were newly added to devices.example.json by an update.

    devices.json is local and never touched by git, so devices added to the
    tracked example later (e.g. a new batch of cards) would otherwise never
    show up on an existing install. SEEDED_FILE remembers which example IPs
    were already offered, so a device deleted from devices.json by hand is not
    resurrected on every restart - only IPs the example gained since last time
    are added.
    """
    if not DEVICES_EXAMPLE_FILE.exists():
        return devices
    try:
        seed = json.loads(DEVICES_EXAMPLE_FILE.read_text(encoding="utf-8"))
        offered = set(json.loads(SEEDED_FILE.read_text(encoding="utf-8"))) if SEEDED_FILE.exists() else set()
    except (OSError, ValueError):
        return devices
    known = {d["ip"] for d in devices}
    added = [d for d in seed if d["ip"] not in offered and d["ip"] not in known]
    if added:
        devices = devices + [{"ip": d["ip"], "name": d.get("name", ""), "tag": d.get("tag", "")} for d in added]
        save_devices(devices)
        logger.info("added %d new device(s) from devices.example.json", len(added))
    if {d["ip"] for d in seed} - offered:
        SEEDED_FILE.write_text(json.dumps(sorted(d["ip"] for d in seed)), encoding="utf-8")
    return devices


def save_devices(devices):
    with open(DEVICES_FILE, "w", encoding="utf-8") as f:
        json.dump(devices, f, indent=2)


def init_state():
    with STATE_LOCK:
        for d in load_devices():
            STATE[d["ip"]] = {
                "ip": d["ip"],
                "name": d.get("name", ""),
                "tag": d.get("tag", ""),
                "status": "unknown",
                "lastError": None,
                "lastPollAt": None,
                "health": None,
                "activeNetwork": None,
                "config": None,
                "configFetchedAt": None,
                "configError": None,
            }


def http_get_json(ip, path, timeout):
    url = "http://" + ip + path
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        ctype = res.headers.get("Content-Type", "")
        body = res.read()
        if "json" in ctype:
            return json.loads(body)
        return body.decode("utf-8", errors="replace")


def http_post(ip, path, payload, timeout):
    url = "http://" + ip + path
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.status, res.read().decode("utf-8", errors="replace")


def derive_blue_ip(red_ip):
    """
    SMPTE 2022-7 style redundant addressing seen across this fleet: the
    Blue (backup) network address is the same as Red (primary), with the
    second octet incremented by one - e.g. 10.101.4.158 (red) <->
    10.102.4.158 (blue), confirmed against self/interfaces e1/e2 on several
    devices. Returns None if `red_ip` doesn't look like a plain IPv4 address.
    """
    parts = red_ip.split(".")
    if len(parts) != 4:
        return None
    try:
        octets = [int(p) for p in parts]
    except ValueError:
        return None
    octets[1] += 1
    return ".".join(str(o) for o in octets)


def call_with_failover(red_ip, fn):
    """
    Calls fn(ip) against the Red (primary) address first; if that raises,
    retries once against the derived Blue (backup) address. Returns
    (result, used_ip, used_network) where used_network is "red" or "blue".
    Re-raises the original Red-path exception if there's no usable Blue
    address or the Blue attempt also fails.
    """
    try:
        return fn(red_ip), red_ip, "red"
    except Exception as red_err:
        blue_ip = derive_blue_ip(red_ip)
        if not blue_ip:
            raise
        try:
            return fn(blue_ip), blue_ip, "blue"
        except Exception:
            raise red_err


def reboot_device(ip):
    """
    Sends the Fusion device's documented-by-field-name reboot trigger:
    POST /emsfp/node/v1/self/system with {"reboot": "1"}. self/system's GET
    response exposes reboot/config_reset/counters_reset as "0"/"1" flags, so
    this mirrors that convention - but it has NOT been fired against a live
    unit during development (that would mean actually rebooting a production
    device to test it). Verify once on a device you know is safe to restart,
    and if the response looks wrong, check service.log for the raw body.
    Falls back to the Blue address if Red doesn't respond.
    """
    (status, body), used_ip, used_net = call_with_failover(
        ip, lambda addr: http_post(addr, "/emsfp/node/v1/self/system", {"reboot": "1"}, REQUEST_TIMEOUT_SEC)
    )
    return status, body, used_net


def configure_syslog(ip, server, port, enable, monitoring=None):
    """
    Mirrors the shape GET /self/syslog itself returns
    ({"config": {"server", "port", "enable"}, "monitoring": {"common": {...},
    "encap": {...}, "decap": {...}}}) - POST-ing the same sub-objects back is
    the pattern the rest of this API uses elsewhere (e.g. self/diag/nmos).
    `monitoring`, if given, is passed straight through as the "monitoring"
    sub-object, e.g. {"common": {"ptp_event": True}, "decap": {"output_flywheel": True}}.
    Not verified against a live unit (that would mean actually repointing a
    production device's syslog target to test it) - verify on one device
    before applying fleet-wide. Falls back to the Blue address if Red
    doesn't respond.
    """
    payload = {"config": {"server": server, "port": port, "enable": enable}}
    if monitoring:
        payload["monitoring"] = monitoring
    (status, body), used_ip, used_net = call_with_failover(
        ip, lambda addr: http_post(addr, "/emsfp/node/v1/self/syslog", payload, REQUEST_TIMEOUT_SEC)
    )
    return status, body, used_net


def set_device_syslog_enable(ip, enable):
    """
    Per-device on/off toggle: reads that device's own currently-configured
    server/port (so this never changes them) and re-posts with just the
    enable flag flipped, leaving monitoring flags alone. Uses whichever of
    Red/Blue actually answers, and posts the change back over that same path.
    """
    current, used_ip, used_net = call_with_failover(
        ip, lambda addr: http_get_json(addr, "/emsfp/node/v1/self/syslog", REQUEST_TIMEOUT_SEC)
    )
    current_cfg = (current or {}).get("config", {})
    server = current_cfg.get("server")
    port = current_cfg.get("port")
    status, body = http_post(used_ip, "/emsfp/node/v1/self/syslog", {"config": {"server": server, "port": port, "enable": enable}}, REQUEST_TIMEOUT_SEC)
    return status, body, used_net


def get_color_bars(ip):
    """
    On-demand only (not part of the background poll): lists this device's
    SDI outputs and whether each currently has its color-bar test pattern
    on, mirroring the old fusionFunctions.js getColorBarInfo()/setColorBarInfo().
    """
    id_list, used_ip, used_net = call_with_failover(
        ip, lambda addr: http_get_json(addr, "/emsfp/node/v1/sdi_output/", REQUEST_TIMEOUT_SEC)
    )
    outputs = []
    for entry in id_list or []:
        out_id = str(entry).rstrip("/")
        detail = http_get_json(used_ip, "/emsfp/node/v1/sdi_output/" + out_id, REQUEST_TIMEOUT_SEC)
        outputs.append({"id": out_id, "label": detail.get("label"), "color_bar": detail.get("color_bar")})
    return outputs, used_net


def set_color_bar(ip, sdi_output_id, value):
    """
    POST /sdi_output/{id} {"color_bar": true|false} - this puts a live test
    pattern on that SDI output in place of the real picture, so it needs the
    same care as a reboot: confirm before enabling on a device that's on air.
    """
    (status, body), used_ip, used_net = call_with_failover(
        ip, lambda addr: http_post(addr, "/emsfp/node/v1/sdi_output/" + sdi_output_id, {"color_bar": bool(value)}, REQUEST_TIMEOUT_SEC)
    )
    return status, body, used_net


# Short essence letters used in stream names: V01, A01..A04, D01 (D = data /
# ancillary).
ESSENCE_LETTER = {"video": "V", "audio": "A", "ancillary": "D"}


def get_stream_rates(ip):
    """
    On-demand only: live per-flow packet rate/count/sequence-error telemetry
    for every channel on this device (/telemetry/devices), flattened into a
    simple list for the UI instead of the deeply nested shape the device
    returns it in. Each row is named like "CH01 - V01" / "CH01 - A03": the
    channel, the essence letter, and the n-th engine of that essence on the
    channel in the order the device lists them (pri and sec legs of the same
    engine share the name).
    """
    data, used_ip, used_net = call_with_failover(
        ip, lambda addr: http_get_json(addr, "/emsfp/node/v1/telemetry/devices", REQUEST_TIMEOUT_SEC)
    )
    rows = []
    for chan in (data or {}).get("devices", []):
        ch = chan.get("channel")
        ch_label = "CH%02d" % ch if isinstance(ch, int) else "CH--"
        seen = {}  # essence -> how many engines of it already named on this channel
        for engine in chan.get("engines", []):
            essence = engine.get("essence") or "stream"
            seen[essence] = seen.get(essence, 0) + 1
            letter = ESSENCE_LETTER.get(essence) or (essence[:1].upper() or "?")
            name = "%s - %s%02d" % (ch_label, letter, seen[essence])
            for flow in engine.get("flows", []):
                rows.append({
                    "channel": ch,
                    "chType": chan.get("type"),
                    "name": name,
                    "essence": engine.get("essence"),
                    "leg": flow.get("type"),
                    "flow": flow.get("flow"),
                    "pkt_rate": flow.get("pkt_rate"),
                    "pkt_cnt": flow.get("pkt_cnt"),
                    "sequence_error": flow.get("sequence_error"),
                })
    return rows, used_net


# IDs coming from the browser get spliced into a device URL path, so only
# accept plain UUID-ish tokens (no slashes / dots / query characters).
_ID_RE = re.compile(r"^[0-9A-Za-z-]{8,64}$")


def is_valid_id(value):
    return bool(value) and bool(_ID_RE.match(value))


def _flow_counters(diag):
    """pkt_cnt / sequence_error per RTP leg from a /self/diag/flow/{id} body."""
    legs = []
    for leg in (diag or {}).get("rtp_stream_info", []) or []:
        st = leg.get("status", {}) or {}
        legs.append({"pkt_cnt": st.get("pkt_cnt"), "sequence_error": st.get("sequence_error")})
    return legs


def _sum_counter(legs_per_flow, key):
    total = 0
    for legs in legs_per_flow:
        for leg in legs:
            try:
                total += int(leg.get(key) or 0)
            except (TypeError, ValueError):
                pass
    return total


def reset_flows(ip, flow_ids, mode):
    """
    Resets one or several flows via /self/diag/flow/{flow_id}, whose GET
    exposes config: {commit, reset, reset_counter}:
      mode "counters" -> reset_counter: clears that flow's packet /
                         sequence-error counters only (non-disruptive).
      mode "flow"     -> reset: resets the flow itself (may glitch that
                         stream while it re-locks).
    The POST body ({"config": {<flag>: "1", "commit": "1"}}) is inferred
    from the field names - the existing tools never wrote these - so it has
    NOT been confirmed on a live unit. To make that visible instead of
    silent, this reads every flow's counters before and after and returns
    both, plus the config flags as the device reports them afterwards (a flag
    left at "1" means the device didn't treat it as a momentary trigger).
    Whether Red or Blue answers is worked out once (first read) and reused
    for the rest, instead of paying a Red timeout per flow.
    Returns {"network": ..., "flows": [{flow, ok, httpStatus|error, before,
    after, configAfter}, ...]} in the order given. Raises only if the device
    can't be reached at all.
    """
    if mode == "counters":
        flag = "reset_counter"
    elif mode == "flow":
        flag = "reset"
    else:
        raise ValueError("mode must be 'counters' or 'flow'")
    if not flow_ids:
        raise ValueError("no flows given")

    def path_of(fid):
        return "/emsfp/node/v1/self/diag/flow/" + fid

    def safe_get(addr, fid):
        try:
            return http_get_json(addr, path_of(fid), REQUEST_TIMEOUT_SEC)
        except Exception:
            return None

    first, used_ip, used_net = call_with_failover(
        ip, lambda addr: http_get_json(addr, path_of(flow_ids[0]), REQUEST_TIMEOUT_SEC)
    )
    before = {flow_ids[0]: first}
    for fid in flow_ids[1:]:
        before[fid] = safe_get(used_ip, fid)

    results = {}
    for fid in flow_ids:
        try:
            status, body = http_post(used_ip, path_of(fid), {"config": {flag: "1", "commit": "1"}}, REQUEST_TIMEOUT_SEC)
            results[fid] = {"ok": True, "httpStatus": status, "body": body[:300]}
        except Exception as e:
            results[fid] = {"ok": False, "error": str(e)}

    time.sleep(0.5)
    out = []
    for fid in flow_ids:
        after = safe_get(used_ip, fid)
        entry = dict(results[fid])
        entry.update({
            "flow": fid,
            "before": _flow_counters(before[fid]),
            "after": _flow_counters(after),
            "configAfter": (after or {}).get("config"),
        })
        out.append(entry)
    return {"network": used_net, "flows": out}


def reset_flow(ip, flow_id, mode):
    """Single-flow convenience wrapper around reset_flows()."""
    res = reset_flows(ip, [flow_id], mode)
    f = res["flows"][0]
    if not f["ok"]:
        raise RuntimeError(f["error"])
    return {
        "httpStatus": f["httpStatus"],
        "body": f["body"],
        "network": res["network"],
        "before": f["before"],
        "after": f["after"],
        "configAfter": f["configAfter"],
    }


def reset_channel(ip, channel, mode):
    """
    Resets every flow of one logical channel on a Fusion together - all
    engines (V01, A01..A04, D01) and both legs (pri / sec) - by looking the
    channel's flows up fresh from /telemetry/devices rather than trusting a
    list from the browser, then handing them to reset_flows(). Returns the
    per-flow results plus summed sequence-error / packet counters before and
    after, so the UI can show one summary line.
    """
    rows, _ = get_stream_rates(ip)
    flow_ids = []
    for r in rows:
        fid = r.get("flow")
        if r.get("channel") == channel and is_valid_id(fid) and fid not in flow_ids:
            flow_ids.append(fid)
    if not flow_ids:
        raise LookupError("no flows found for channel %s on %s" % (channel, ip))
    res = reset_flows(ip, flow_ids, mode)
    flows = res["flows"]
    return {
        "network": res["network"],
        "total": len(flows),
        "succeeded": sum(1 for f in flows if f["ok"]),
        "failed": [{"flow": f["flow"], "error": f.get("error")} for f in flows if not f["ok"]],
        "seqErrBefore": _sum_counter([f["before"] for f in flows], "sequence_error"),
        "seqErrAfter": _sum_counter([f["after"] for f in flows], "sequence_error"),
        "pktBefore": _sum_counter([f["before"] for f in flows], "pkt_cnt"),
        "pktAfter": _sum_counter([f["after"] for f in flows], "pkt_cnt"),
        "stuckFlags": sorted({
            k for f in flows for k, v in (f.get("configAfter") or {}).items()
            if k != "commit" and str(v) == "1"
        }),
    }


def _flow_link0(flow):
    """First link of a /flows/{id} body (GET returns 'network' as a list of up to 4 legs, or a bare object)."""
    net = (flow or {}).get("network")
    if isinstance(net, list):
        return net[0] if net else {}
    return net or {}


def enable_channel_flows(ip, channel):
    """
    Switches ON every flow of one logical channel - all engines (V, A, D) and
    both legs (pri / sec), 12 flows on a Fusion channel - by POSTing
    {"network": {"enable": "1"}} to /flows/{id}. This is the same
    'network' write the old flow editor uses, with the device's own field
    name ('enable', "1"/"0", as GET returns it). Checked on a live Fusion
    (CH06 video primary: enable 0 -> 1, nothing else changed).
    The device silently ignores the write while the flow has no destination
    address (dst_ip_addr 0.0.0.0 - not routed yet; a 200 comes back but
    enable stays 0), so every flow is read back afterwards and those that
    didn't stick are reported with the reason instead of claiming success.
    Flows that are already on are left alone. Channel flows are looked up
    fresh from /telemetry/devices, not taken from the browser.
    """
    rows, _ = get_stream_rates(ip)
    flows = []
    for r in rows:
        fid = r.get("flow")
        if r.get("channel") == channel and is_valid_id(fid) and all(f["flow"] != fid for f in flows):
            flows.append({"flow": fid, "name": r.get("name"), "leg": r.get("leg")})
    if not flows:
        raise LookupError("no flows found for channel %s on %s" % (channel, ip))

    def path_of(fid):
        return "/emsfp/node/v1/flows/" + fid

    first, used_ip, used_net = call_with_failover(
        ip, lambda addr: http_get_json(addr, path_of(flows[0]["flow"]), REQUEST_TIMEOUT_SEC)
    )
    before = {flows[0]["flow"]: first}
    for f in flows[1:]:
        try:
            before[f["flow"]] = http_get_json(used_ip, path_of(f["flow"]), REQUEST_TIMEOUT_SEC)
        except Exception:
            before[f["flow"]] = None

    posted = []
    failed = []
    for f in flows:
        link = _flow_link0(before[f["flow"]])
        if before[f["flow"]] is None:
            failed.append({"flow": f["flow"], "name": f["name"], "leg": f["leg"], "error": "could not read flow"})
        elif str(link.get("enable")) != "1":
            try:
                http_post(used_ip, path_of(f["flow"]), {"network": {"enable": "1"}}, REQUEST_TIMEOUT_SEC)
                posted.append(f)
            except Exception as e:
                failed.append({"flow": f["flow"], "name": f["name"], "leg": f["leg"], "error": str(e)})

    time.sleep(0.5)
    enabled = 0
    stuck = []
    for f in posted:
        try:
            link = _flow_link0(http_get_json(used_ip, path_of(f["flow"]), REQUEST_TIMEOUT_SEC))
        except Exception as e:
            failed.append({"flow": f["flow"], "name": f["name"], "leg": f["leg"], "error": "read-back failed: " + str(e)})
            continue
        if str(link.get("enable")) == "1":
            enabled += 1
        else:
            no_dst = link.get("dst_ip_addr") in (None, "", "0.0.0.0")
            stuck.append({
                "flow": f["flow"], "name": f["name"], "leg": f["leg"],
                "reason": "no destination address set (not routed yet)" if no_dst else "device did not accept enable",
            })
    return {
        "network": used_net,
        "total": len(flows),
        "alreadyOn": len(flows) - len(posted) - len(failed),
        "enabled": enabled,
        "stuck": stuck,
        "failed": failed,
    }


def reset_device_counters(ip):
    """
    All flows of one Fusion at once: POST /self/system {"counters_reset": "1"},
    mirroring the "reboot" flag next to it in the same GET. Inferred from the
    field name, not confirmed on a live unit (see reset_flow).
    """
    (status, body), used_ip, used_net = call_with_failover(
        ip, lambda addr: http_post(addr, "/emsfp/node/v1/self/system", {"counters_reset": "1"}, REQUEST_TIMEOUT_SEC)
    )
    return status, body, used_net


# Warning codes the device reports under /telemetry/warnings, with how much
# they should count. Only 1001 has ever been seen. A fleet-wide read-only
# check (21 warnings on 15 devices) found it always refers to an SDI
# *output* and always coincides with that output's line_offset.status == 1
# (91 of 91 warned-vs-unwarned pairs on the same device) - a timing /
# line-offset status flag. In 20 of the 21 cases the channel's video
# receiver was enabled and ~189k pkt/s were arriving, so it is NOT a "stream
# down" indicator and must not turn the device yellow. The vendor doesn't
# document the code, so anything not listed here stays a real warning.
WARNING_SEVERITY = {1001: "info"}


def classify_warnings(warnings):
    """Tags each warning with severity 'info' | 'warning' (unknown codes = 'warning')."""
    for w in warnings:
        w["severity"] = WARNING_SEVERITY.get(w.get("code"), "warning")
    return warnings


def poll_device_health(ip):
    with STATE_LOCK:
        if ip in INFLIGHT:
            return
        INFLIGHT.add(ip)
    try:
        node, used_ip, used_net = call_with_failover(
            ip, lambda addr: http_get_json(addr, "/emsfp/node/v1/telemetry/node", REQUEST_TIMEOUT_SEC)
        )
        warnings = []
        try:
            warn_ids = http_get_json(used_ip, "/emsfp/node/v1/telemetry/warnings/", REQUEST_TIMEOUT_SEC) or []
            for wid in warn_ids:
                clean = str(wid).rstrip("/")
                w = http_get_json(used_ip, "/emsfp/node/v1/telemetry/warnings/" + clean, REQUEST_TIMEOUT_SEC)
                if isinstance(w, dict) and w.get("warning"):
                    warnings.extend(w["warning"])
        except Exception:
            pass  # warnings are secondary info; a failure here shouldn't mark the device offline
        with STATE_LOCK:
            rec = STATE.get(ip)
            if rec is None:
                return
            classify_warnings(warnings)
            rec["health"] = {"node": node, "warnings": warnings}
            rec["status"] = "warning" if any(w["severity"] == "warning" for w in warnings) else "online"
            rec["lastError"] = None
            rec["activeNetwork"] = used_net
    except Exception as e:
        with STATE_LOCK:
            rec = STATE.get(ip)
            if rec is not None:
                rec["status"] = "offline"
                rec["lastError"] = str(e)
                rec["activeNetwork"] = None
        logger.warning("health poll failed for %s (red and blue both unreachable): %s", ip, e)
    finally:
        with STATE_LOCK:
            if ip in STATE:
                STATE[ip]["lastPollAt"] = now_iso()
            INFLIGHT.discard(ip)


def fetch_device_config(ip):
    def safe(path):
        try:
            return http_get_json(used_ip, path, CONFIG_TIMEOUT_SEC)
        except Exception:
            return None

    # Determine which of Red/Blue is reachable once (via the first call),
    # then reuse that address for the rest of this device's config calls -
    # retrying the failover for every single sub-call would multiply the
    # timeout cost whenever Red is down.
    used_ip = ip
    try:
        ipconfig, used_ip, used_net = call_with_failover(
            ip, lambda addr: http_get_json(addr, "/emsfp/node/v1/self/ipconfig", CONFIG_TIMEOUT_SEC)
        )
        information = safe("/emsfp/node/v1/self/information")
        firmware = safe("/emsfp/node/v1/self/firmware")
        license_ = safe("/emsfp/node/v1/self/license")
        interfaces = safe("/emsfp/node/v1/self/interfaces")
        nmos = safe("/emsfp/node/v1/self/diag/nmos")
        syslog = safe("/emsfp/node/v1/self/syslog")

        ports = []
        try:
            port_list = http_get_json(used_ip, "/emsfp/node/v1/port", CONFIG_TIMEOUT_SEC) or []
            for p in port_list:
                num = str(p).rstrip("/")
                detail = safe("/emsfp/node/v1/port/" + num)
                ports.append({"num": num, "detail": detail})
        except Exception:
            pass  # port breakdown is optional detail

        with STATE_LOCK:
            rec = STATE.get(ip)
            if rec is None:
                return
            rec["config"] = {
                "ipconfig": ipconfig,
                "information": information,
                "firmware": firmware,
                "license": license_,
                "interfaces": interfaces,
                "nmos": nmos,
                "syslog": syslog,
                "ports": ports,
            }
            rec["configError"] = None
    except Exception as e:
        with STATE_LOCK:
            rec = STATE.get(ip)
            if rec is not None:
                rec["configError"] = str(e)
        logger.warning("config fetch failed for %s: %s", ip, e)
    finally:
        with STATE_LOCK:
            if ip in STATE:
                STATE[ip]["configFetchedAt"] = now_iso()


def health_poll_loop():
    while True:
        POLLING_ENABLED.wait()
        start = time.time()
        with STATE_LOCK:
            ips = list(STATE.keys())
        logger.info("health sweep starting for %d device(s)", len(ips))
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            list(pool.map(poll_device_health, ips))
        elapsed = time.time() - start
        logger.info("health sweep done in %.1fs", elapsed)
        time.sleep(max(1.0, POLL_INTERVAL_SEC - elapsed))


def config_refresh_loop():
    while True:
        POLLING_ENABLED.wait()
        with STATE_LOCK:
            ips = list(STATE.keys())
        logger.info("config refresh starting for %d device(s)", len(ips))
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            list(pool.map(fetch_device_config, ips))
        logger.info("config refresh done")
        time.sleep(CONFIG_REFRESH_INTERVAL_SEC)


def snapshot():
    with STATE_LOCK:
        devices = [dict(rec) for rec in STATE.values()]
    devices.sort(key=lambda d: tuple(int(x) for x in d["ip"].split(".")))
    summary = {"online": 0, "warning": 0, "offline": 0, "unknown": 0}
    for d in devices:
        summary[d["status"]] = summary.get(d["status"], 0) + 1
    summary["total"] = len(devices)
    summary["info"] = sum(
        1 for d in devices
        if d["status"] != "offline"
        and any(w.get("severity") == "info" for w in ((d.get("health") or {}).get("warnings") or []))
    )
    return {
        "generatedAt": now_iso(),
        "pollIntervalSec": POLL_INTERVAL_SEC,
        "pollingEnabled": POLLING_ENABLED.is_set(),
        "devices": devices,
        "summary": summary,
    }


# /api/state is ~1 MB of JSON and every open browser tab asks for it every 4s.
# Build it at most once per STATE_CACHE_SEC and share the (also gzipped) bytes
# between all viewers, so the cost no longer grows with the number of viewers.
STATE_CACHE_SEC = 1.0
_state_cache = {"at": 0.0, "raw": b"", "gz": b""}
_state_cache_lock = threading.Lock()


def state_payload():
    with _state_cache_lock:
        now = time.monotonic()
        if now - _state_cache["at"] > STATE_CACHE_SEC or not _state_cache["raw"]:
            raw = json.dumps(snapshot()).encode("utf-8")
            _state_cache.update(at=now, raw=raw, gz=gzip.compress(raw, 5))
        return _state_cache["raw"], _state_cache["gz"]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        if args and str(args[0]).startswith("GET /api/state"):
            return      # polled every 4s by every open tab - would flood service.log
        logger.info("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            try:
                body = DASHBOARD_FILE.read_bytes()
            except FileNotFoundError:
                self.send_error(500, "dashboard.html missing next to server.py")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")   # always pick up a freshly updated page
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/state":
            raw, gz = state_payload()
            use_gz = "gzip" in self.headers.get("Accept-Encoding", "")
            body = gz if use_gz else raw
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            if use_gz:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/api/devices/colorbars"):
            ip = (parse_qs(urlparse(self.path).query).get("ip") or [""])[0]
            if not ip or ip not in STATE:
                self._send_json({"ok": False, "error": "unknown device"}, 400)
                return
            try:
                outputs, used_net = get_color_bars(ip)
                self._send_json({"ok": True, "network": used_net, "outputs": outputs})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 502)
        elif self.path.startswith("/api/devices/streams"):
            ip = (parse_qs(urlparse(self.path).query).get("ip") or [""])[0]
            if not ip or ip not in STATE:
                self._send_json({"ok": False, "error": "unknown device"}, 400)
                return
            try:
                rows, used_net = get_stream_rates(ip)
                self._send_json({"ok": True, "network": used_net, "streams": rows})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 502)
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except Exception:
            self._send_json({"error": "invalid JSON body"}, 400)
            return

        if self.path == "/api/devices":
            ip = (data.get("ip") or "").strip()
            if not ip:
                self._send_json({"error": "ip is required"}, 400)
                return
            name = (data.get("name") or "").strip()
            tag = (data.get("tag") or "").strip()
            with STATE_LOCK:
                if ip not in STATE:
                    STATE[ip] = {
                        "ip": ip, "name": name, "tag": tag, "status": "unknown",
                        "lastError": None, "lastPollAt": None, "health": None,
                        "activeNetwork": None,
                        "config": None, "configFetchedAt": None, "configError": None,
                    }
                devices = load_devices()
                if not any(d["ip"] == ip for d in devices):
                    devices.append({"ip": ip, "name": name, "tag": tag})
                    save_devices(devices)
            threading.Thread(target=fetch_device_config, args=(ip,), daemon=True).start()
            threading.Thread(target=poll_device_health, args=(ip,), daemon=True).start()
            self._send_json({"ok": True})
        elif self.path == "/api/devices/delete":
            ip = (data.get("ip") or "").strip()
            with STATE_LOCK:
                STATE.pop(ip, None)
                devices = [d for d in load_devices() if d["ip"] != ip]
                save_devices(devices)
            self._send_json({"ok": True})
        elif self.path == "/api/devices/reboot":
            ip = (data.get("ip") or "").strip()
            if not ip or ip not in STATE:
                self._send_json({"ok": False, "error": "unknown device"}, 400)
                return
            logger.warning("reboot requested for %s via API", ip)
            try:
                status, body, used_net = reboot_device(ip)
                logger.warning("reboot response for %s (via %s): HTTP %s %s", ip, used_net, status, body[:300])
                self._send_json({"ok": True, "httpStatus": status, "body": body[:500], "network": used_net})
            except Exception as e:
                logger.error("reboot failed for %s: %s", ip, e)
                self._send_json({"ok": False, "error": str(e)}, 502)
        elif self.path == "/api/devices/colorbar":
            ip = (data.get("ip") or "").strip()
            sdi_output_id = (data.get("sdiOutputId") or "").strip()
            value = bool(data.get("value"))
            if not ip or ip not in STATE or not is_valid_id(sdi_output_id):
                self._send_json({"ok": False, "error": "valid ip and sdiOutputId are required"}, 400)
                return
            logger.warning("color_bar=%s requested for %s / %s via API", value, ip, sdi_output_id)
            try:
                status, body, used_net = set_color_bar(ip, sdi_output_id, value)
                self._send_json({"ok": True, "httpStatus": status, "body": body[:500], "network": used_net})
            except Exception as e:
                logger.error("color_bar change failed for %s / %s: %s", ip, sdi_output_id, e)
                self._send_json({"ok": False, "error": str(e)}, 502)
        elif self.path == "/api/devices/flow-reset":
            ip = (data.get("ip") or "").strip()
            flow_id = (data.get("flowId") or "").strip()
            mode = (data.get("mode") or "").strip()
            if not ip or ip not in STATE or not is_valid_id(flow_id) or mode not in ("counters", "flow"):
                self._send_json({"ok": False, "error": "valid ip, flowId and mode ('counters' or 'flow') are required"}, 400)
                return
            logger.warning("flow reset (%s) requested for %s / %s via API", mode, ip, flow_id)
            try:
                result = reset_flow(ip, flow_id, mode)
                logger.warning(
                    "flow reset (%s) for %s / %s: HTTP %s, before=%s after=%s configAfter=%s",
                    mode, ip, flow_id, result["httpStatus"], result["before"], result["after"], result["configAfter"],
                )
                self._send_json(dict(result, ok=True))
            except Exception as e:
                logger.error("flow reset failed for %s / %s: %s", ip, flow_id, e)
                self._send_json({"ok": False, "error": str(e)}, 502)
        elif self.path == "/api/devices/channel-reset":
            ip = (data.get("ip") or "").strip()
            channel = data.get("channel")
            mode = (data.get("mode") or "").strip()
            if not ip or ip not in STATE or not isinstance(channel, int) or isinstance(channel, bool) or mode not in ("counters", "flow"):
                self._send_json({"ok": False, "error": "valid ip, integer channel and mode ('counters' or 'flow') are required"}, 400)
                return
            logger.warning("channel reset (%s) requested for %s / CH%02d via API", mode, ip, channel)
            try:
                result = reset_channel(ip, channel, mode)
                logger.warning("channel reset (%s) for %s / CH%02d: %s", mode, ip, channel, result)
                self._send_json(dict(result, ok=True))
            except LookupError as e:
                self._send_json({"ok": False, "error": str(e)}, 404)
            except Exception as e:
                logger.error("channel reset failed for %s / CH%02d: %s", ip, channel, e)
                self._send_json({"ok": False, "error": str(e)}, 502)
        elif self.path == "/api/devices/channel-enable":
            ip = (data.get("ip") or "").strip()
            channel = data.get("channel")
            if not ip or ip not in STATE or not isinstance(channel, int) or isinstance(channel, bool):
                self._send_json({"ok": False, "error": "valid ip and integer channel are required"}, 400)
                return
            logger.warning("enable flows requested for %s / CH%02d via API", ip, channel)
            try:
                result = enable_channel_flows(ip, channel)
                logger.warning("enable flows for %s / CH%02d: %s", ip, channel, result)
                self._send_json(dict(result, ok=True))
            except LookupError as e:
                self._send_json({"ok": False, "error": str(e)}, 404)
            except Exception as e:
                logger.error("enable flows failed for %s / CH%02d: %s", ip, channel, e)
                self._send_json({"ok": False, "error": str(e)}, 502)
        elif self.path == "/api/devices/counters-reset":
            ip = (data.get("ip") or "").strip()
            if not ip or ip not in STATE:
                self._send_json({"ok": False, "error": "unknown device"}, 400)
                return
            logger.warning("counters reset (all flows) requested for %s via API", ip)
            try:
                status, body, used_net = reset_device_counters(ip)
                logger.warning("counters reset for %s (via %s): HTTP %s %s", ip, used_net, status, body[:300])
                self._send_json({"ok": True, "httpStatus": status, "body": body[:500], "network": used_net})
            except Exception as e:
                logger.error("counters reset failed for %s: %s", ip, e)
                self._send_json({"ok": False, "error": str(e)}, 502)
        elif self.path == "/api/devices/syslog-enable":
            ip = (data.get("ip") or "").strip()
            enable = bool(data.get("enable", True))
            if not ip or ip not in STATE:
                self._send_json({"ok": False, "error": "unknown device"}, 400)
                return
            logger.info("syslog enable=%s requested for %s via API", enable, ip)
            try:
                status, body, used_net = set_device_syslog_enable(ip, enable)
                # Reflect the change immediately so the checkbox doesn't
                # appear to "snap back" before the next slow config refresh
                # (every CONFIG_REFRESH_INTERVAL_SEC) picks it up for real.
                with STATE_LOCK:
                    rec = STATE.get(ip)
                    if rec and rec.get("config") and rec["config"].get("syslog"):
                        rec["config"]["syslog"].setdefault("config", {})["enable"] = enable
                self._send_json({"ok": True, "httpStatus": status, "body": body[:500], "network": used_net})
            except Exception as e:
                logger.error("syslog enable change failed for %s: %s", ip, e)
                self._send_json({"ok": False, "error": str(e)}, 502)
        elif self.path == "/api/syslog":
            server_addr = (data.get("server") or "").strip()
            port = data.get("port")
            enable = bool(data.get("enable", True))
            monitoring = data.get("monitoring") if isinstance(data.get("monitoring"), dict) else None
            if not server_addr or not isinstance(port, int):
                self._send_json({"ok": False, "error": "server (string) and port (integer) are required"}, 400)
                return
            with STATE_LOCK:
                ips = list(STATE.keys())
            logger.warning(
                "syslog config requested: server=%s port=%s enable=%s monitoring=%s targets=%d",
                server_addr, port, enable, monitoring, len(ips),
            )

            def apply_one(ip):
                try:
                    status, body, used_net = configure_syslog(ip, server_addr, port, enable, monitoring)
                    with STATE_LOCK:
                        rec = STATE.get(ip)
                        if rec and rec.get("config") and rec["config"].get("syslog"):
                            rec["config"]["syslog"].setdefault("config", {})["enable"] = enable
                    return ip, True, f"HTTP {status} (via {used_net})"
                except Exception as e:
                    return ip, False, str(e)

            results = {}
            with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
                for ip, ok, info in pool.map(apply_one, ips):
                    results[ip] = {"ok": ok, "info": info}
            ok_count = sum(1 for r in results.values() if r["ok"])
            logger.warning("syslog config done: %d/%d succeeded", ok_count, len(ips))
            self._send_json({"ok": True, "succeeded": ok_count, "total": len(ips), "results": results})
        elif self.path == "/api/polling":
            enabled = bool(data.get("enabled", True))
            if enabled:
                POLLING_ENABLED.set()
                logger.info("polling resumed via API")
            else:
                POLLING_ENABLED.clear()
                logger.info("polling paused via API")
            self._send_json({"ok": True, "pollingEnabled": POLLING_ENABLED.is_set()})
        else:
            self.send_error(404)


def main():
    init_state()
    threading.Thread(target=config_refresh_loop, daemon=True).start()
    threading.Thread(target=health_poll_loop, daemon=True).start()
    ThreadingHTTPServer.request_queue_size = 128   # default 5 drops connections when many browsers connect at once
    ThreadingHTTPServer.daemon_threads = True
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    logger.info("PLS Fusion Dashboard service listening on http://%s:%d/", HOST, PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
