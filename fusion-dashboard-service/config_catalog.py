"""
Settings catalog for emsfp-platform devices (Nevion/Macnica MuoN cards and
Fusion gateways - both expose the same /emsfp/node/v1 REST API).

Every writable setting the tool knows about is one entry here. The device
itself decides what applies: a setting is only planned for a device if the
field is actually present in that device's own GET response ("capability by
presence"), so e.g. Fusion-only fields (fan_speed syslog flag, /port, /sdi
bit rate) are simply reported as "not supported" on a MuoN card.

Entry fields:
  id        unique id (also the key used in profiles)
  group     UI grouping
  label     UI label
  scope     "device" | "sdi_output" | "clean_switch" | "flow" | "ptp" | "port"
            - anything but "device" exists once per I/O instance and can be
              overridden per instance (per-I/O settings)
  endpoint  path relative to /emsfp/node/v1/ for scope "device"; for I/O
            scopes the instance path comes from the device read
  field     path into the GET response (list of keys / list indices)
  body      optional different path for the POST body (flows: the GET
            returns network as a list of legs, the device's own web client
            POSTs it back as a flat object for leg 0)
  type      "str" | "int" | "bool" | "enum" | "ip" | "audiomap"
  options   for enum
  batch     True  = sensible to set the same value on many devices at once
            False = per-device / per-I/O only (addresses, hostnames, ...)
  danger    shown with a warning; changes that can take a device or a
            signal off the air
  evidence  "verified"   = written and read back on the MuoN test card
            "same-shape" = not probed itself, but same endpoint and write
                           shape as a verified setting
            "js"  = write shape taken from the vendor's own web client JS
                    (fusionFunctions.js / flowEditorFunctions.js)
            "api" = derived from field names in the GET response only;
                    verify on a test card before fleet-wide use
"""

DEVICE, SDI_OUTPUT, CLEAN_SWITCH, FLOW, PTP, PORT = "device", "sdi_output", "clean_switch", "flow", "ptp", "port"

SCOPES = {
    SDI_OUTPUT: "SDI outputs",
    CLEAN_SWITCH: "Clean switch (per channel)",
    FLOW: "Flows (receive/transmit legs)",
    PTP: "PTP clock ports",
    PORT: "SFP ports (Fusion)",
}

CATALOG = []


def _add(id, group, label, scope, field, type="str", endpoint=None, options=None, batch=True,
         danger=False, evidence="api", body=None, help=None):
    CATALOG.append({
        "id": id, "group": group, "label": label, "scope": scope, "endpoint": endpoint,
        "field": field, "body": body, "type": type, "options": options, "batch": batch,
        "danger": danger, "evidence": evidence, "help": help,
    })


BOOL01 = "bool01"  # "0"/"1" strings on the wire, shown as a checkbox

# ---------------------------------------------------------------- identity / network
_add("hostname", "Identity & network", "Hostname", DEVICE, ["hostname"], endpoint="self/ipconfig",
     batch=False, evidence="js", help="Shown in NMOS as the node label prefix.")
for eth in ("e1", "e2"):
    name = "Red (e1)" if eth == "e1" else "Blue (e2)"
    _add(f"if.{eth}.static_ip", "Identity & network", f"{name} static IP/prefix", DEVICE, [eth, "static_ip"],
         endpoint="self/interfaces", batch=False, danger=True, evidence="js",
         help="e.g. 10.101.12.162/30 - changing this moves the management address.")
    _add(f"if.{eth}.static_gateway", "Identity & network", f"{name} gateway", DEVICE, [eth, "static_gateway"], "ip",
         endpoint="self/interfaces", batch=False, danger=True, evidence="js")
    _add(f"if.{eth}.dhcp", "Identity & network", f"{name} DHCP", DEVICE, [eth, "dhcp"], "bool",
         endpoint="self/interfaces", danger=True, evidence="js")
    _add(f"if.{eth}.vlan", "Identity & network", f"{name} VLAN", DEVICE, [eth, "vlan"], "int",
         endpoint="self/interfaces", danger=True, evidence="js")
for i in range(1, 6):
    _add(f"route{i}.destination", "Static routes", f"Route {i} destination", DEVICE, [f"route_{i}", "destination"],
         endpoint="self/static_route", help="CIDR, 0.0.0.0/0 + gateway 0.0.0.0 = unused")
    _add(f"route{i}.gateway", "Static routes", f"Route {i} gateway", DEVICE, [f"route_{i}", "gateway"], "ip",
         endpoint="self/static_route")

# ---------------------------------------------------------------- syslog
_add("syslog.server", "Syslog", "Server", DEVICE, ["config", "server"], "ip", endpoint="self/syslog")
_add("syslog.port", "Syslog", "Port", DEVICE, ["config", "port"], "int", endpoint="self/syslog")
_add("syslog.enable", "Syslog", "Enabled", DEVICE, ["config", "enable"], "bool", endpoint="self/syslog")
for section, flags in (
    ("common", ["ptp_event", "temp_event", "northbound_api_event", "rtp_timestamp_audio_event", "fan_speed"]),
    ("encap", ["sdi_event", "no_signal"]),
    ("decap", ["output_flywheel", "memory_pkt_error", "dash7_fifo_error", "flow_impairment", "frame_repeat", "frame_skipped"]),
):
    for flag in flags:
        _add(f"syslog.mon.{section}.{flag}", "Syslog monitoring", f"{section}: {flag}", DEVICE,
             ["monitoring", section, flag], "bool", endpoint="self/syslog")

# ---------------------------------------------------------------- NMOS
_add("nmos.registry_mode", "NMOS registration", "Registry mode", DEVICE, ["registry_mode"], "enum",
     endpoint="self/diag/nmos", options=["manual", "auto"], evidence="js")
_add("nmos.registry_address", "NMOS registration", "Registry address (ip:port)", DEVICE, ["registry_address"],
     endpoint="self/diag/nmos", evidence="js")
_add("nmos.registry_address_2", "NMOS registration", "Registry address 2 (ip:port)", DEVICE, ["registry_address_2"],
     endpoint="self/diag/nmos")
_add("nmos.domain_name", "NMOS registration", "DNS domain name", DEVICE, ["domain_name"], endpoint="self/diag/nmos",
     evidence="js")
_add("nmos.mdns_mode", "NMOS registration", "mDNS discovery", DEVICE, ["mdns_mode"], "bool", endpoint="self/diag/nmos")
for i, suffix in enumerate(["", "_2", "_3", "_4"]):
    _add(f"nmos.dns{i + 1}", "NMOS registration", f"Manual DNS server {i + 1}", DEVICE,
         [f"manual_dns_server_address{suffix}"], "ip", endpoint="self/diag/nmos", evidence="js" if i == 0 else "api")
_add("nmos.control_network", "NMOS registration", "Control network (Fusion)", DEVICE, ["control_network"], "enum",
     endpoint="self/diag/nmos", options=["media", "oob"], danger=True)

# ---------------------------------------------------------------- system / protocols
_add("sys.igmp_version", "System", "IGMP version", DEVICE, ["igmp", "version"], "enum", endpoint="self/system",
     options=[2, 3])
_add("sys.2022_7_class", "System", "ST 2022-7 class", DEVICE, ["smpte_network", "2022-7", "class"], "enum",
     endpoint="self/system", options=["a", "b", "c", "d"])
_add("sys.led_mode", "System", "Status LED mode", DEVICE, ["led", 0, "mode"], "enum", endpoint="self/system",
     options=["auto", "on", "off", "blink"])
_add("sys.device_management", "System", "Media-network device management (Fusion)", DEVICE,
     ["access_control", "media", "device_management"], "bool", endpoint="self/system", danger=True)
_add("proto.mdns", "System", "mDNS responder", DEVICE, ["mdns_enable"], BOOL01, endpoint="self/protocols")
_add("proto.sap", "System", "SAP announcements (Fusion)", DEVICE, ["sap_announcement_enable"], BOOL01,
     endpoint="self/protocols")
_add("lldp.rate", "System", "LLDP rate (s)", DEVICE, ["configuration", "rate"], "int", endpoint="lldp")
_add("lldp.untag", "System", "LLDP untagged", DEVICE, ["configuration", "untag"], "bool", endpoint="lldp")
_add("lldp.enable_rx", "System", "LLDP receive", DEVICE, ["configuration", "enable_rx"], BOOL01, endpoint="lldp")
for eth in ("e1", "e2"):
    _add(f"phy.{eth}.fec", "System", f"{eth} FEC scheme", DEVICE, [eth, "fec", "scheme"], "enum", endpoint="self/phy",
         options=["rs", "none"], danger=True, help="Sent together with commit=true; the link retrains.")
_add("sdi.bitrate", "System", "SDI operating bit rate (Fusion)", DEVICE, ["configuration", "operating_bit_rate"],
     "enum", endpoint="sdi", options=["auto", "1", "2", "3", "4"], evidence="js")

# ---------------------------------------------------------------- PTP
_add("ptp.mode", "PTP", "Mode", DEVICE, ["mode"], endpoint="refclk")
_add("ptp.two_step", "PTP", "Two-step sync", DEVICE, ["sync_mode_two_step"], "bool", endpoint="refclk")
_add("ptp.delay_req", "PTP", "Delay request interval (log2)", DEVICE, ["delay_req"], "int", endpoint="refclk")
_add("ptp.announce_timeout", "PTP", "Announce receipt timeout", DEVICE, ["announceReceiptTimeout"], "int",
     endpoint="refclk")
_add("ptp.ttl", "PTP", "TTL", DEVICE, ["ttl"], "int", endpoint="refclk")
_add("ptp.domain", "PTP", "Domain", PTP, ["domain_num"], "int", evidence="js")
_add("ptp.vlan", "PTP", "VLAN", PTP, ["vlan_id"], "int")
_add("ptp.dscp", "PTP", "DSCP", PTP, ["dscp"], "int")

# ---------------------------------------------------------------- SDI outputs
_add("sdi.loss_of_input", "SDI output", "On loss of input", SDI_OUTPUT, ["input_signal_output_mode", "loss_of_input"],
     "enum", options=["freeze", "black", "disable"], evidence="js")
_add("sdi.vpid_source", "SDI output", "VPID source", SDI_OUTPUT, ["vpid", "source"], "enum",
     options=["regenerated", "passthrough", "override"])
_add("sdi.vpid_override", "SDI output", "VPID override value", SDI_OUTPUT, ["vpid", "override_value"])
_add("sdi.offset_mode", "SDI output", "Line offset mode", SDI_OUTPUT, ["line_offset", "offset_mode"])
_add("sdi.usec_offset", "SDI output", "Offset (µs)", SDI_OUTPUT, ["line_offset", "usec_offset"], "int")
_add("sdi.v_offset", "SDI output", "Vertical offset (lines)", SDI_OUTPUT, ["line_offset", "v_offset"], "int")
_add("sdi.h_offset", "SDI output", "Horizontal offset (px)", SDI_OUTPUT, ["line_offset", "h_offset"], "int")
_add("sdi.audio_delay", "SDI output", "Audio delay", SDI_OUTPUT, ["line_offset", "audio_delay"], "int")
_add("sdi.frame_buffer", "SDI output", "Frame buffer", SDI_OUTPUT, ["line_offset", "frame_buffer"], "int")
_add("sdi.color_bar", "SDI output", "Colour bars", SDI_OUTPUT, ["color_bar"], "bool", danger=True, evidence="js",
     help="Replaces the live picture with a test pattern.")
for ch in range(16):
    _add(f"sdi.audio.ch{ch}", "SDI audio map", f"Embedded ch {ch + 1}", SDI_OUTPUT, ["sdi_aud_chans_cfg", f"ch{ch}"],
         "audiomap", batch=False, help="<audio flow name>:<flow channel>:<enable 0/1>, :0:0 = unused")

# ---------------------------------------------------------------- clean switch
_add("cs.mode", "Clean switch", "Mode", CLEAN_SWITCH, ["clean_switch", "mode"], "enum",
     options=["disabled", "enabled"])
_add("cs.type", "Clean switch", "Type", CLEAN_SWITCH, ["clean_switch", "type"], "enum", options=["bbm", "mbb"],
     help="bbm = break-before-make, mbb = make-before-break")
_add("cs.igmp_setup_delay", "Clean switch", "IGMP setup delay (ms)", CLEAN_SWITCH,
     ["clean_switch", "igmp_setup_delay"], "int")
_add("cs.timeout_option", "Clean switch", "On timeout", CLEAN_SWITCH, ["clean_switch", "timeout_option"], "enum",
     options=["switch", "abort"])

# ---------------------------------------------------------------- flows
_add("flow.label", "Flow", "Label", FLOW, ["label"], batch=False)
for key, label, typ, batch in (
    ("src_ip_addr", "Source IP", "ip", False),
    ("src_udp_port", "Source UDP port", "int", False),
    ("dst_ip_addr", "Destination (multicast) IP", "ip", False),
    ("dst_udp_port", "Destination UDP port", "int", False),
    ("igmp_src_ip", "IGMPv3 source (SSM)", "ip", False),
    ("enable", "Leg enabled", BOOL01, False),
    ("rtp_pt", "RTP payload type", "int", True),
    ("vlan_tag", "VLAN tag", "int", True),
    ("ssrc", "SSRC", "int", False),
    ("pkt_filter_src_ip", "Filter on source IP", BOOL01, True),
    ("pkt_filter_src_udp", "Filter on source port", BOOL01, True),
    ("pkt_filter_src_mac", "Filter on source MAC", BOOL01, True),
    ("pkt_filter_dst_ip", "Filter on destination IP", BOOL01, True),
    ("pkt_filter_dst_udp", "Filter on destination port", BOOL01, True),
    ("pkt_filter_dst_mac", "Filter on destination MAC", BOOL01, True),
    ("pkt_filter_vlan", "Filter on VLAN", BOOL01, True),
    ("pkt_filter_ssrc", "Filter on SSRC", BOOL01, True),
):
    _add(f"flow.{key}", "Flow", label, FLOW, ["network", 0, key], typ, body=["network", key], batch=batch,
         danger=key in ("dst_ip_addr", "dst_udp_port", "enable"),
         evidence="js" if key in ("src_ip_addr", "src_udp_port", "dst_ip_addr", "dst_udp_port") else "api")

# ---------------------------------------------------------------- SFP ports (Fusion)
_add("port.host_pinout", "SFP port", "Host pinout", PORT, ["host_pinout"], "enum", options=["2T", "2R", "1T1R"],
     danger=True, evidence="js")
_add("port.sfp_type", "SFP port", "SFP type", PORT, ["sfp_type"], "enum", options=["auto", "msa", "n-msa"],
     danger=True)

BY_ID = {s["id"]: s for s in CATALOG}

# Results of tools/probe-writes.py on the MuoN test card 10.101.12.162
# (2026-09-25): write shape accepted and read back correctly.
VERIFIED_ON_MUON = [
    "syslog.port", "syslog.mon.common.temp_event", "ptp.announce_timeout", "ptp.dscp", "sdi.audio_delay",
    "cs.igmp_setup_delay", "flow.rtp_pt", "flow.pkt_filter_src_ip",
    # second run, same day
    "lldp.rate", "nmos.registry_address_2", "sdi.v_offset",
]
for _id in VERIFIED_ON_MUON:
    BY_ID[_id]["evidence"] = "verified"
# Same endpoint and write shape as a verified sibling -> same confidence.
for _s in CATALOG:
    if _s["evidence"] == "api" and (
        _s["endpoint"] in ("self/syslog", "lldp", "self/diag/nmos")
        or _s["id"].startswith(("sdi.", "cs.", "flow.pkt_filter_", "ptp."))
    ) and _s["type"] != "audiomap" and _s["id"] != "sdi.vpid_source":
        _s["evidence"] = "same-shape"

# During the same probe run, writing mdns_enable and/or the IGMP version was
# followed by the card dropping off the network and rebooting, with the new
# values kept. Treat both as disruptive.
for _id in ("proto.mdns", "sys.igmp_version"):
    BY_ID[_id]["danger"] = True
    BY_ID[_id]["help"] = "Changing this was followed by a card reboot on the MuoN test card."

BY_ID["sdi.vpid_source"]["options"] = ["regenerated", "override"]
BY_ID["sdi.vpid_source"]["help"] = "'passthrough' is rejected with HTTP 400 on the MuoN."

# Parents that are always POSTed complete (all catalog-known leaves, current
# values merged with the change), because the vendor client sends them as a
# unit or because a partial update would be ambiguous.
FULL_PARENTS = {
    "self/interfaces": [["e1"], ["e2"]],
    "self/syslog": [["config"]],
}

# Extra keys added to a body whenever a leaf below a parent changes.
BODY_EXTRAS = {
    "self/phy": {("e1", "fec"): {"commit": True}, ("e2", "fec"): {"commit": True}},
}

# Order in which endpoints are written on one device: media first,
# management-network changes last so a moved IP can't cut off the rest.
# self/protocols (mDNS) and self/system (IGMP) go after everything else on
# the same management address: on the MuoN, writing them made the card stop
# answering and reboot about 45 s later (test card, 2026-09-25).
ENDPOINT_ORDER = ["flow", "sdi_output", "clean_switch", "port", "ptp", "refclk", "sdi", "self/syslog", "lldp",
                  "self/phy", "self/diag/nmos", "self/static_route", "self/protocols", "self/system",
                  "self/ipconfig", "self/interfaces"]
