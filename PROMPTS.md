# Development Prompts

Copy-paste or adapt these prompts when starting work on the Tapo local apps. Update them as the project evolves.

## 1. H500 Recording Proof of Concept (Python + tapo library)

```
Create a Python script that uses the tapo library (pip install tapo) to:
1. Connect to a Tapo H500 hub at a given IP using Tapo account credentials (read from environment variables TAPO_USERNAME and TAPO_PASSWORD).
2. List all cameras paired to the hub.
3. For one camera, retrieve the days that have recordings.
4. List recent clips for a chosen day.
5. Download one clip as MPEG-TS to a local file.

Include clear error handling, logging, and comments explaining each step.
Make the script configurable via command-line arguments or a simple config.
```

## 2. H500 Recording Proof of Concept (Rust + tapo crate)

```
Create a Rust example that uses the tapo crate to connect to a Tapo H500 hub, list paired cameras, query recording days, list clips, and download one recording as MPEG-TS.
Use environment variables for credentials and IP.
Provide clear comments and basic error handling.
```

## 3. RTSP Live View Test

```
Write a short guide and minimal test commands (or a small Python/ffmpeg script) to verify RTSP streams from Tapo cameras.
Include the standard URLs for HD and SD streams, notes on creating the Camera Account in the Tapo app, and common troubleshooting steps (ports, credentials, firewall).
```

## 4. Device Inventory Template

```
Create a DEVICES.md template for documenting a Tapo home setup. Include sections for:
- Hub(s) (model, IP, firmware)
- Cameras (model, IP, paired to which hub, Camera Account status, RTSP working?)
- Other devices (plugs, sensors, etc.)
- Credentials notes (locations only — never actual secrets)
- Known issues or quirks
```

## 5. Simple Local Dashboard Prototype

```
Propose a minimal architecture and starter code for a local web dashboard that:
- Shows a multi-camera live grid (prefer HLS generated from RTSP or direct support)
- Provides a basic timeline or list for recordings available on an H500
- Allows simple control of at least one device type
- Runs fully locally

Recommend a tech stack (e.g., FastAPI + frontend, or adaptation of tapo-web) and outline the first implementation steps.
```

## 6. Evaluate Existing tapo-web App

```
Summarize how the freeKC/tapo-web project works, its features relevant to our goals (live view, SD card / recording browser, download), tech stack, and what would be involved in adapting it for H500 hub recordings instead of (or in addition to) camera SD cards.
```

## General Guidelines for Prompts

- Always prefer local-only solutions.
- Explicitly request environment-variable or config-file credential handling (never hard-coded secrets).
- Ask for comments and error handling.
- Reference the libraries in TAPO_RESOURCES.md when relevant.
- After testing on real hardware, update INITIAL_PLAN.md and DEVICES.md with results.

## Next Recommended Prompt to Run

Start with prompt **#1** (Python H500 POC) or **#4** (Device Inventory) depending on whether hardware details are already available.
