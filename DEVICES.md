# Device Inventory

> This file is a **snapshot** of what we have observed.  
> The dashboard and scripts should **discover devices dynamically** from the H500 (and later from other hubs or direct devices).  
> Do **not** treat this as the source of truth for the application — update it only when useful for reference.  
> Do **not** store actual passwords here.

## Hubs

| Model | IP | Firmware | Notes |
|-------|----|----------|-------|
| H500  | 192.168.11.223 | 1.3.24 Build 20260814 rel.70065 | Primary local recording hub. Only device with a LAN IP. |

## Currently Observed Paired Devices (via `get_child_device_list_json()`)

| Name | Model | Device ID | Category | Hub Storage | Notes |
|------|-------|-----------|----------|-------------|-------|
| Front_Door | C420 | 8021626666AA71D19F11B8A7F4411D9021CADA15 | camera | on | |
| Front_doorbell | D230 | 80212C1AC05761C63F63F7069DAE5AA321FBF99D | camera | on | |
| Garage | C420 | 80210A1DDC1B9C86F76934F9243CDD0221CA5852 | camera | on | |
| Backyard | C420 | 80212445B5DBF96ED11D6835A4D60AB921C9D094 | camera | on | MAC 3C-52-A1-6F-69-25, fw 1.4.2 (from app) |
| Driveway | C420 | 8021992CC61434F900C6365B3F3CD2FB21CAEE16 | camera | on | |

> These were successfully listed and their recording days/clips retrieved on 2026-10-11.  
> New cameras, plugs, sensors, or other child devices should appear automatically when the software queries the hub.

## Other Devices (Plugs, Sensors, Lights, etc.)

| Model | IP / via Hub | Type | Notes |
|-------|--------------|------|-------|
| (none observed yet) | | | Will be discovered dynamically if connected to the hub or reachable on the LAN. |

## Credentials Location

- Cloud TP-Link account: stored in environment variables (`TAPO_USERNAME`, `TAPO_PASSWORD`) for scripts; password manager for long-term.
- Camera Accounts: not yet created (not needed for hub-mediated access).

## Network Notes

- Subnet: 192.168.11.0/24
- Static IPs preferred? no, unless needed
- Cameras and similar devices connect to H500 via low-power wireless (not main WiFi/LAN with their own IPs)

## Known Issues / Quirks

- Only H500 has a LAN IP; cameras are not directly addressable.
- `get_general_device_list()` returns empty; `get_child_device_list()` can hit a serde error; `get_child_device_list_json()` works reliably.
- Still primarily using the stock Tapo app for live view and configuration.

## Test Results

### H500 Recording Access
- Date tested: 2026-10-11
- Library used: tapo 0.11.1 (Python)
- Result: Success — listed 5 devices, retrieved recording days and recent clips for all.

### Live RTSP
- Not applicable for these battery/low-power cameras (no direct IP).
