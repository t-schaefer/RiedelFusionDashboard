"""Build channel-names.json (receiver channel -> button name) from a Cerebrum
destination-table export.

Usage:  python build_channel_names.py "DST new.txt"

The export is a tab-separated table with one destination (panel button) per
row: "_Alternate Mnemonic 1" is the button name ("|" = line break on the
button) and the "_Level ID n" columns reference the NMOS receivers, e.g.
"NMOS1|501|CS3.IP/HI 01 Device CH1|CS3.IP/HI 01 VidRx 000" - device name and
channel number. That pair is the key; "N/A", "0" and blank names are skipped.
An .rtf copy is converted with macOS `textutil` first.
Result: {"CS3.IP/HI 01": {"1": "CS3 DESK / PATCH LEFT", ...}, ...}
"""
import csv
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "channel-names.json")
LEVEL_RE = re.compile(r"^NMOS\d+\|\d+\|(.+?) Device CH(\d+)\|")
UNUSED = {"", "0", "n/a", "na", "-"}


def read_rows(path):
    if open(path, "rb").read(5) == b"{\\rtf":
        tmp = os.path.join(tempfile.mkdtemp(), "dst.txt")
        subprocess.check_call(["textutil", "-convert", "txt", "-output", tmp, path])
        path = tmp
    csv.field_size_limit(10 ** 7)
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        return list(csv.reader(f, delimiter="\t"))


def main(path):
    rows = read_rows(path)
    header = [h.strip().lower() for h in rows[0]]
    name_col = header.index("_alternate mnemonic 1")
    level_cols = [i for i, h in enumerate(header) if h.startswith("_level id")]
    names, conflicts = {}, 0
    for r in rows[1:]:
        if len(r) <= max(level_cols):
            continue
        label = r[name_col].strip().replace("|", " / ").replace("  ", " ").strip(" /")
        if label.lower() in UNUSED:
            continue
        for key in {m.groups() for c in (r[i] for i in level_cols) if (m := LEVEL_RE.match(c))}:
            dev, ch = key[0], str(int(key[1]))
            if ch in names.get(dev, {}) and names[dev][ch] != label:
                conflicts += 1       # same device channel on two rows: keep the first
                continue
            names.setdefault(dev, {}).setdefault(ch, label)
    names = {d: dict(sorted(c.items(), key=lambda kv: int(kv[0]))) for d, c in sorted(names.items())}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(names, f, indent=1, ensure_ascii=False)
    print("%d devices, %d named channels -> %s (%d conflicting duplicates ignored)" % (
        len(names), sum(len(c) for c in names.values()), OUT, conflicts))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
