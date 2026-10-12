# Initial Plan — Local Tapo Apps

**Focus**: Local Tapo-related applications first (H500 hub, cameras, sensors, plugs, lights).

**Core principle**: Prefer **dynamic discovery**. The application should query the H500 (and later other devices) at runtime for the current list of paired/connected devices rather than relying on static inventory files.

## Phase 0: Inventory & Setup

- [x] Confirm H500 IP and basic reachability
- [x] Verify cloud account credentials work locally
- [x] Keep discovery notes in DEVICES.md. Do not commit device ids or LAN addresses.
- [ ] Optional: note any non-hub devices that have their own LAN IPs

## Phase 1: Connectivity Proof of Concept — **COMPLETE**

**Goal**: Confirm we can talk to the H500 and retrieve camera/recording data locally.

Achieved with `scripts/h500_poc.py` + tapo library:

- Connected to the H500 at the address in `TAPO_HUB_IP`
- Listed paired devices via `get_child_device_list_json()` (5 cameras/doorbell)
- Retrieved recording days and recent clips for each
- Confirmed hub storage is enabled

**Deliverable**: Working script + confirmed local access. (Download of a clip not yet implemented but library supports it.)

## Phase 2: Dynamic Local Dashboard / App Prototype — **CURRENT**

**Preferred approach**: Simple Python web app (FastAPI) that:

- Dynamically discovers all devices paired to the H500 on each load / refresh
- Lists cameras, doorbells, and any other child devices
- Shows recording days and recent clips per device
- Allows downloading selected clips
- Remains fully local (credentials only from environment variables)
- Is structured so it can later include:
  - Non-camera devices (plugs, lights, sensors) that are either hub children or directly addressable
  - Basic controls (on/off, status)
  - Live view where technically possible

Other options (evaluate later if needed):
- Adapt existing `tapo-web`
- Tauri desktop app
- Rust backend

**Desired first features**:
- Dynamic device list from hub
- Recording timeline / clip list per device
- Clip download
- Clean, simple UI
- Easy to extend

## Phase 3: Expansion

- Live view support (where possible; battery cameras are limited)
- Direct control of plugs, lights, and other non-camera devices
- Sensor automations
- Notifications / local event handling
- Multi-hub or broader home automation integration

## Decision Points

- Stack for first dashboard: Python + FastAPI (recommended for speed of iteration)
- Priority: Dynamic device listing + recordings first; live view and non-camera controls next

## Risks & Mitigations

- Firmware breaks local protocol → Keep `tapo` library updated; monitor the main repos
- Battery/low-power cameras have limited direct local features → Route through H500; treat live view as a later enhancement
- Credential management → Environment variables / secure local config only; never commit secrets
- Static inventory drift → Application must discover devices dynamically

## Success Criteria for Phase 2

- Dashboard loads and shows the current list of hub-paired devices without manual edits to inventory files
- User can browse recording days and recent clips
- At least one clip can be downloaded
- Adding a new camera or other device to the hub makes it appear in the UI after a refresh

See [PROMPTS.md](PROMPTS.md) for ready-to-use development prompts.
