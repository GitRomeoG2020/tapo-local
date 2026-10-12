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
