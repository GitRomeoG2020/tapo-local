# Tapo Local Control Resources

All libraries below are **unofficial**. They reverse-engineer the local protocols used by Tapo devices.

## Primary Libraries

### 1. tapo (Rust + Python) — Recommended starting point for hubs
- **Repo**: https://github.com/mihai-dinculescu/tapo
- **Crates.io / PyPI**: Available as `tapo`
- **Supports**: Hubs (H100, H200, **H500**), plugs, lights, sensors, switches, and cameras (C210, C220, C225, C325WB, C520WS, TC40, TC70, and others)
- **Key H500 capabilities** (recent versions):
  - List cameras paired to the hub
  - See which days have recordings
  - List clips by time range
  - Download recordings as MPEG-TS
- **Also includes**: An MCP server that exposes Tapo devices as AI-callable tools
- **Auth**: Uses Tapo account credentials; talks directly to device IPs using the TPAP protocol
- **Docs**: See `SUPPORTED_DEVICES.md` and examples in the repo

### 2. pytapo + Home Assistant Tapo Control
- **Repo**: https://github.com/JurajNyiri/pytapo
- **HA Integration**: HomeAssistant-Tapo-Control (built on pytapo)
- **Strengths**: Excellent camera support — local streams, motion events (ONVIF where available), downloading recordings from SD cards
- **Modern auth**: Handles SPAKE2+ / TPAP login required by newer firmwares
- **Note**: Primarily camera-focused; often uses the Camera Account credentials created in the Tapo app

### 3. tapo-v4-protocol (Detailed reverse engineering)
- **Repo**: https://github.com/freeKC/tapo-v4-protocol
- Byte-level specification of the current camera control protocol (SPAKE2+ login + AES-CCM encrypted channel)
- Includes a working Python reference client
- Fast SD-card clip download and media port handling
- Useful for understanding or extending camera-specific behavior when higher-level libraries need updates

### 4. tapo-web (Ready-made self-hosted app)
- **Repo**: https://github.com/freeKC/tapo-web
- Self-hosted web app for Tapo cameras
- Features: Live view (HLS), SD card browser + timeline + fast download/playback, optional animal detection
- Built on the V4 protocol
- Tech: Python + FastAPI + ffmpeg
- Good reference or starting point if a local dashboard is desired quickly

## Official / Easier Live View Paths

### RTSP
Most wired Tapo cameras support RTSP. Create a separate **Camera Account** in the Tapo app (not the cloud login), then:

```
rtsp://camera_user:camera_password@CAMERA_IP:554/stream1   # HD
rtsp://camera_user:camera_password@CAMERA_IP:554/stream2   # SD
```

### ONVIF
Typically available on port 2020 (Profile S). Battery cameras have more limited support.

## Practical Notes

- H500 acts as a local NVR — useful for centralized recording access.
- Firmware updates can occasionally break reverse-engineered clients; the community usually adapts quickly.
- Prefer libraries that support the current TPAP / SPAKE2+ protocol.
- Credentials:
  - Cloud TP-Link account username/password (often needed for hub and some camera control)
  - Separate Camera Account (username + password set in Tapo app → Advanced Settings) for RTSP and some local camera features

## Related Community Projects

- tapo-rest (REST wrapper around the tapo library)
- Various Home Assistant integrations

Update this file as new libraries or limitations are discovered.
