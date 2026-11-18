#!/usr/bin/env python3
"""Offline test of the guided setup logic against the simulated device (tools/fake_fleet.py).
Run:  python tools/test_guide.py      (nothing real is touched; files go to a temp folder)"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fake_fleet as ff                      # noqa: E402  (also puts the service folder on sys.path)
import config_tool as ct                      # noqa: E402
import guide_tool as g                        # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="guide-test-"))
(tmp / "profiles").mkdir()
shutil.copy(os.path.join(ff.SERVICE, "config-profiles", "site-default.example.json"), tmp / "profiles")
g.STATE_FILE, g.SITE_FILE = tmp / "state.json", tmp / "site.json"
ct.AUTH_FILE, ct.PROFILES_DIR, ct.BACKUPS_DIR = tmp / "auth.json", tmp / "profiles", tmp / "backups"
ct.BACKUPS_DIR.mkdir()
ct.http_get, ct.http_post = ff.fake_get, ff.fake_post
added = []
g.init(lambda ip, name, tag: added.append((ip, name, tag)), lambda: [{"ip": "10.101.2.74", "name": "CS3.IP/HI 01"}])

fails = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (("  " + str(detail)) if detail and not cond else ""))
    if not cond:
        fails.append(name)


def raises(fn, *a):
    try:
        fn(*a)
    except ValueError as e:
        return str(e)
    return None


# --- pure helpers
f = g.factory_addresses("40:A3:6B:A2:16:14")
check("factory media address", f["media"] == "10.162.22.20", f)
check("factory oob address", f["oob"] == "11.162.22.20")
check("bad MAC rejected", raises(g.factory_addresses, "40:A3:6B") is not None)

# --- site settings
check("audio layout > 16 channels rejected", raises(g.save_site, {"audio": {"streams": 4, "channels_per_stream": 8}}) is not None)
check("bad registry rejected", raises(g.save_site, {"nmos": {"registry": "nope"}}) is not None)
site = g.save_site({"site_name": "Test", "audio": {"streams": 2, "channels_per_stream": 8}})
check("site saved + configured", site["configured"] and site["audio"]["streams"] == 2)

# --- 1 connect
fd = ff.FakeDevice("40:a3:6b:a2:16:14", "10.162.22.20")
rec = g.connect("10.162.22.20")
mac = rec["mac"]
check("connect reads MAC + serial", mac == "40:a3:6b:a2:16:14" and rec["facts"]["serial"])
check("factory state detected", rec["facts"]["looksFactory"])
check("6 channels seen", len(rec["facts"]["channels"]) == 6)
check("next step is identity", rec["next"] == "identity", rec["next"])
check("unreachable device gives a friendly error", "cannot reach" in (raises(g.connect, "10.9.9.9") or ""))

# --- gating
check("services locked before identity", "finish the step" in (raises(g.plan, mac, "services", {}) or ""))

# --- 2 identity
bad = raises(g.plan, mac, "identity", {"name": "bad name!", "red_ip": "10.101.2.74", "red_gateway": "10.101.2.73"})
check("bad hostname rejected", bad and "name" in bad, bad)
bad = raises(g.plan, mac, "identity", {"name": "CS3.IP/HI 09", "red_ip": "10.101.2.74", "red_gateway": "10.101.2.73"})
check("IP already in dashboard rejected", bad and "already used" in bad, bad)
bad = raises(g.plan, mac, "identity", {"name": "CS3.IP/HI 09", "red_ip": "10.101.2.78", "red_gateway": "10.101.9.73"})
check("gateway outside subnet rejected", bad and "gateway" in bad, bad)
inp = {"name": "CS3.IP/HI 09", "tag": "C03FRMSDIGTW009", "red_ip": "10.101.2.78", "red_gateway": "10.101.2.77"}
p = g.plan(mac, "identity", inp)
check("identity plan has changes", p["changes"] >= 4, p["changes"])
check("blue derived from red", p["inputs"]["blue_ip"] == "10.102.2.78" and p["inputs"]["blue_gateway"] == "10.102.2.77", p["inputs"])
check("wrong serial refused", "serial" in (raises(g.apply, mac, "identity", inp, "999") or ""))
res = g.apply(mac, "identity", inp, rec["facts"]["serial"])
check("identity written, waiting for new address", res.get("written") and res["newIp"] == "10.101.2.78", res)
check("device moved off the factory address", "10.162.22.20" not in ff.REGISTRY and "10.101.2.78" in ff.REGISTRY)
check("not done until confirmed", g.devices_state()[mac]["steps"]["identity"]["status"] == "written")
check("services still locked while only written", "finish the step" in (raises(g.plan, mac, "services", {}) or ""))
conf = g.confirm_identity(mac)
check("identity confirmed on new address", conf.get("ok") and conf.get("red"), conf)
check("identity step done", g.devices_state()[mac]["steps"]["identity"]["status"] == "done")

# --- 3 services
p = g.plan(mac, "services", {})
groups = {r["group"] for r in p["rows"]}
check("services plan covers syslog, NMOS, PTP", {"Syslog", "NMOS registration", "PTP"} <= groups, groups)
res = g.apply(mac, "services", {}, rec["facts"]["serial"])
check("services applied + verified", res["ok"], [r for r in res["rows"] if r.get("verify") not in (None, "ok")][:3])
check("syslog server written", fd.ep["self/syslog"]["config"]["server"] == "10.0.0.50", fd.ep["self/syslog"]["config"])
res2 = g.apply(mac, "services", {}, rec["facts"]["serial"])
check("second run is a no-op", sum(1 for r in res2["rows"] if r["status"] == "change") == 0)

# --- 4 channels (2 audio streams x 8 ch)
p = g.plan(mac, "channels", {})
check("channels plan has changes", p["changes"] > 0, p["changes"])
res = g.apply(mac, "channels", {}, rec["facts"]["serial"])
check("channels applied + verified", res["ok"], [r for r in res["rows"] if r.get("verify") not in (None, "ok")][:3])
en = {f["name"]: f["network"]["enable"] for k, f in fd.ep.items() if k.startswith("flows/")}
check("ch1: video + 2 audio + data on, audio 3/4 off, both legs",
      all(en[f"rx ch1 flow {n} {leg}"] == "1" for n in (0, 1, 2, 5) for leg in ("pri", "sec"))
      and all(en[f"rx ch1 flow {n} {leg}"] == "0" for n in (3, 4) for leg in ("pri", "sec")), {k: v for k, v in en.items() if k.startswith("rx ch1")})
sdi1 = fd.ep["sdi_output/" + fd.sdi_ids[0]]["sdi_aud_chans_cfg"]
ids = {k.split("/", 1)[1]: v["name"] for k, v in fd.ep.items() if k.startswith("flows/")}
mapped = [(ids[sdi1[f"ch{i}"].rsplit(":", 2)[0]], sdi1[f"ch{i}"].rsplit(":", 2)[1]) if sdi1[f"ch{i}"] not in ("", ":0:0") else None for i in range(16)]
check("SDI 1 audio map: ch1-8 -> audio 1 ch0..7, ch9-16 -> audio 2", mapped[0] == ("rx ch1 flow 1 pri", "0") and mapped[7] == ("rx ch1 flow 1 pri", "7")
      and mapped[8] == ("rx ch1 flow 2 pri", "0") and mapped[15] == ("rx ch1 flow 2 pri", "7"), mapped)

last_out = fd.ep["sdi_output/" + fd.sdi_ids[5]]["sdi_aud_chans_cfg"]["ch0"]
check("6th SDI output (id b7) takes audio from CH06, not a non-existent CH08", ids[last_out.rsplit(":", 2)[0]] == "rx ch6 flow 1 pri", last_out)

# --- per-device overrides of the site layout
po = g.plan(mac, "channels", {"overrides": {"audio": {"streams": 1, "channels_per_stream": 4}}})
check("device override: 1 audio stream only", po["summary"]["channels"][0]["on"] == ["video", "audio 1", "data"], po["summary"]["channels"][0])
check("override too large is rejected", raises(g.plan, mac, "channels", {"overrides": {"audio": {"streams": 5}}}) is not None)
check("override > 16 audio channels is rejected", raises(g.plan, mac, "channels", {"overrides": {"audio": {"streams": 4, "channels_per_stream": 8}}}) is not None)
check("services override reaches the plan", any(r["new"] == "10.9.9.9" for r in g.plan(mac, "services", {"overrides": {"syslog": {"server": "10.9.9.9"}}})["rows"]))

# --- 5 verify + hand over
checks = g.run_checks(mac)
byid = {c["id"]: c for c in checks}
check("checks: PTP locked, NMOS connected, names ok", byid["ptp"]["ok"] and byid["nmos"]["ok"] and byid["name"]["ok"], byid)
check("checks: syslog + lldp ok", byid["syslog"]["ok"] and byid["lldp"]["ok"], byid)
ho = g.hand_over(mac, False)
failed = [c["id"] for c in ho["checks"] if not c["ok"]]
if failed:
    check("hand over blocks until failed checks are acknowledged", ho.get("needsAck"), failed)
    ho = g.hand_over(mac, True)
check("device added to monitoring with name + tag", added == [("10.101.2.78", "CS3.IP/HI 09", "C03FRMSDIGTW009")], added)
check("all steps done", g._public(g._get(mac))["next"] is None)

g.forget(mac)
check("forget removes the setup record", mac not in g.devices_state())
check("forget unknown device is an error", raises(g.forget, mac) is not None)

shutil.rmtree(tmp, ignore_errors=True)
print("\n%d check(s) failed" % len(fails) if fails else "\nall checks passed")
sys.exit(1 if fails else 0)
