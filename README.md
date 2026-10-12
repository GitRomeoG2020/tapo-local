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

## H500 dashboard

`app/main.py` is a local page for the same hub. It lists paired cameras and doorbells with name, model, and hub storage. Open a device to see recording days from the last 7 days. The page opens on the newest day. Each day is a link to that day's clips. Each clip has a Download button that saves that recording as MPEG-TS. Download all clips for this day saves every clip from the selected day into one zip file. That can take a while, because the hub sends the clips one at a time.

The camera list prefers `get_child_device_list_json()`. If that list has no cameras or doorbells, the page uses `get_general_device_list()`, then `get_child_device_list()`. Login and device filtering are the same helpers as `scripts/h500_poc.py`.

Every page has the same top navigation. Cameras & Recordings is this home page. Live View lists the stream names in `bin/go2rtc.yaml` and links each one to the go2rtc player at `http://<host>:1984/stream.html?src=<name>`. It also links to the full go2rtc UI on port 1984. go2rtc is assumed to be running on the same computer.

Devices lists plugs, lights, and other non-camera Tapo devices. With no addresses in `devices.txt`, the page scans the H500 subnet. Add an IPv4 address to that file to check a device directly. A broadcast address such as `192.168.11.255` scans that subnet. Each row shows the name, model, and online status. Plugs and lights can be switched on or off. A light that supports brightness has a brightness control. New addresses do not require a code change. The same `TAPO_USERNAME` and `TAPO_PASSWORD` variables are used. Camera recordings and live view are unchanged.

To add a plug or light on another subnet or VLAN, list it in `extra_devices.yaml` in the project root. The file is optional. When it is missing, or it only has comments, the Devices page stays on the scan above. Listed devices are shown together with anything that scan finds. The app logs in to each listed address with `TAPO_USERNAME` and `TAPO_PASSWORD` and shows its name, model, and on/off control. Brightness is shown when the device reports it. An address that does not respond is shown as offline.

```yaml
extra_devices:
  - ip: 192.168.0.24
    name: Music Room Bulb
  - ip: 192.168.0.55
    name: Smart Plug
```

`model` is optional (`L530`, `P110`, and similar). Put the devices for this house in `extra_devices.yaml`. The example addresses above are only a pattern.

The account is read from `TAPO_USERNAME` and `TAPO_PASSWORD`. Do not put those values in the code or in this repository.

```powershell
python -m pip install -r requirements.txt
$env:TAPO_USERNAME = "you@example.com"
$env:TAPO_PASSWORD = "your-tapo-password"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000` on this computer. The server listens on localhost only.

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
