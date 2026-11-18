#!/usr/bin/env python3
"""Shortcut for the demo mode:  python tools/fake_fleet.py [--port 8190]  ==  python server.py --demo
Also re-exports the simulator (FakeDevice, REGISTRY, fake_get, fake_post) for tools/test_guide.py."""
import os
import sys

SERVICE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fusion-dashboard-service")
sys.path.insert(0, SERVICE)

from demo_fleet import REGISTRY, FakeDevice, fake_get, fake_post  # noqa: E402,F401


def main():
    os.chdir(SERVICE)
    os.execv(sys.executable, [sys.executable, os.path.join(SERVICE, "server.py"), "--demo"] + sys.argv[1:])


if __name__ == "__main__":
    main()
