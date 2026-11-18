# Roadmap

## Guided setup (`/guide`)

Done in version 1: connect (factory address from the MAC), name and network, time and
services, channels and audio, check and hand over to monitoring.

Next:
1. **Firmware step** - show the active firmware and compare it with a site target; later, update
   from the guide. Whether firmware can be uploaded through the API is not known yet (the device
   root page has an upload form that the config tool deliberately never touches) - test on a spare unit.
2. **German user interface** (the UI is English only for now).
3. **Spin-off as a stand-alone tool** that runs on the engineer's laptop (own repo, double-click
   start). It is already separate code (`guide_tool.py`, `guide.html`) on top of the config logic.
4. **"What to do" hints** on dashboard warnings, linking to the matching guide step.
5. Transmit (encapsulator) channel templates and multicast address allocation per site.
6. Group the devices of the Config page by the dashboard's groups.

- Public demo: per-visitor simulated fleet (today all visitors share one state and it resets every 30 min); packaged local program (one executable for Windows / macOS) for engineers.
