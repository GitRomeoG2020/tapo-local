# Home Automation Project

## Overview

This project focuses on building **local-first** home automation tools and applications. The initial priority is the **TP-Link Tapo ecosystem**, particularly local control and monitoring of hubs (e.g., H500), cameras, sensors, plugs, and related devices.

TP-Link does not publish an official local API. All work relies on community reverse-engineered libraries and protocols. These are actively maintained but can occasionally break with firmware updates.

## Core Goals (Tapo Phase)

- Discover Tapo devices on the local network
- Live multi-camera viewing (RTSP/ONVIF or proprietary media protocol)
- Playback and download of recordings stored on the H500 hub or on camera SD cards
- Control of sensors, plugs, lights, switches, and other devices attached to hubs
- Fully local operation after initial authentication (no ongoing cloud dependency)
- Clean dashboard experience inspired by systems like UniFi Protect

## High-Level Approach

1. Inventory existing Tapo hardware (models, IPs, hub pairing status).
2. Prove connectivity and basic operations with existing libraries (especially H500 + cameras).
3. Build a focused proof-of-concept or adopt/adapt an existing local web app.
4. Expand into a unified local dashboard / automation layer.
5. Later phases: integrate other ecosystems, custom automations, and UI polish.

## Key Resources

See [TAPO_RESOURCES.md](TAPO_RESOURCES.md) for libraries, protocols, and related projects.

## Getting Started

- Review [INITIAL_PLAN.md](INITIAL_PLAN.md)
- Use the ready-made prompts in [PROMPTS.md](PROMPTS.md)
- Document device inventory in a new `DEVICES.md` as details are collected

## H500 recording check

`scripts/h500_poc.py` logs in to the H500 at `192.168.11.223`, lists paired cameras, and prints recording days from the last 7 days plus clips on the newest day. It does not download video.

Use the cloud TP-Link account that owns the hub. Set the variables in the shell. Do not put them in the script or in this repository.

```powershell
python -m pip install "tapo>=0.10"
$env:TAPO_USERNAME = "you@example.com"
$env:TAPO_PASSWORD = "your-tapo-password"
python scripts/h500_poc.py
```

`tapo` 0.10 or newer is required for H500 recordings. A direct login uses AES. If the hub only accepts TPAP (Third-Party Compatibility off in the Tapo app), the script scans the subnet once and uses the hub it finds at that address. `tapo` 0.11 speaks TPAP to an H500 through discovery. A rejected password is not retried, because a locked hub refuses further logins for a while.

## Status

- Project initialized from prior research chat
- First focus: local Tapo apps (H500 recordings, camera live view, basic control)

## Notes

- Prefer local-only paths wherever possible.
- Camera Account credentials (created in the Tapo app under Advanced Settings) are often required separately from the cloud TP-Link account credentials.
- RTSP is the simplest path for live view on most wired cameras:
  - `rtsp://camera_user:camera_password@CAMERA_IP:554/stream1` (HD)
  - `rtsp://camera_user:camera_password@CAMERA_IP:554/stream2` (SD)
- ONVIF is typically available on port 2020.
