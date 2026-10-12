"""Add devices from devices.example.json that are missing in devices.json.

Only appends entries whose IP isn't in devices.json yet - existing entries
(names, tags) are never changed or removed. Stop the service first, because it
rewrites devices.json itself, then start it again:

    Stop-ScheduledTask  -TaskName PLSFusionDashboardService
    python merge_devices.py
    Start-ScheduledTask -TaskName PLSFusionDashboardService
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
LIVE = os.path.join(HERE, "devices.json")
SEED = os.path.join(HERE, "devices.example.json")


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main():
    seed = load(SEED)
    live = load(LIVE) if os.path.exists(LIVE) else []
    known = {d["ip"] for d in live}
    added = [d for d in seed if d["ip"] not in known]
    if not added:
        print("devices.json already contains every device from devices.example.json")
        return
    with open(LIVE, "w", encoding="utf-8") as f:
        json.dump(live + added, f, indent=2, ensure_ascii=False)
    print("Added %d device(s):" % len(added))
    for d in added:
        print("  %s  %s" % (d["ip"], d.get("name", "")))


if __name__ == "__main__":
    main()
