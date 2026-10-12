# Initial Plan — Local Tapo Apps

**Focus**: Local Tapo-related applications first (H500 hub, cameras, sensors, plugs).

## Phase 0: Inventory & Setup (Immediate)

- [ ] Document all Tapo devices (model, IP, role, firmware if known)
- [ ] Note which cameras are paired to the H500
- [ ] Confirm Camera Account credentials exist for cameras that need RTSP
- [ ] Verify network reachability (devices respond on expected ports)
- [ ] Create `DEVICES.md` with the inventory

## Phase 1: Connectivity Proof of Concept

**Goal**: Confirm we can talk to the H500 and cameras locally.

Priority order (based on prior research):

1. **H500 + recordings** using the `tapo` library (Rust or Python)
   - Connect to H500
   - List paired cameras
   - Query recording days
   - List recent clips
   - Download one MPEG-TS clip

2. **Camera live view**
   - Test RTSP streams
   - Optionally test ONVIF
   - Compare with proprietary media protocol if needed

3. **Basic device control**
   - Toggle a plug or read a sensor via the hub or direct connection

**Deliverable**: Working scripts + notes on what succeeded / failed on the actual hardware.

## Phase 2: Dashboard / App Prototype

Options to evaluate:

- Adapt or extend the existing `tapo-web` project
- Simple Python web app (FastAPI / Streamlit / Gradio) embedding RTSP (via HLS or WebRTC) + recording timeline
- Tauri desktop app for a more native feel
- Rust backend + web frontend if performance or long-term maintainability is prioritized

Desired features for the first dashboard:
- Multi-camera live grid
- Timeline / calendar for H500 or SD card recordings
- Basic device controls
- Fully local

## Phase 3: Expansion

- Sensor automations
- Multi-hub or mixed-device support
- Notifications / local event handling
- Integration with broader home automation (later)

## Decision Points (Need User Input)

- Preferred language / stack for the first app? (Python web, Rust, Tauri, other)
- Most important first feature?
  - Live multi-cam view
  - Recording playback from H500
  - Sensor / automation control
  - Overall dashboard
- Any existing Home Assistant setup we should align with?

## Risks & Mitigations

- Firmware breaks local protocol → Keep libraries updated; monitor the main repos
- Battery cameras have limited local features → Prefer wired cameras for advanced use
- Credential management → Store locally and securely; never hard-code in committed files

## Success Criteria for Phase 1

- Reliable connection to H500
- Ability to list and download at least one recording
- At least one live RTSP stream working
- Clear notes on any device-specific quirks

See [PROMPTS.md](PROMPTS.md) for ready-to-use development prompts.
