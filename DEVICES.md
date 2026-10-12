# Device Inventory

> Fill this in as details are collected. Do **not** store actual passwords here.

## Hubs

| Model | IP | Firmware | Notes |
|-------|----|----------|-------|
| H500  | 192.168.11.223 | 1.3.24 Build 20260814 rel.70065 | Primary local recording hub. Only device with a LAN IP. |

## Cameras

| Model | IP | Paired to Hub | Camera Account Created? | RTSP Working? | Notes |
|-------|----|---------------|-------------------------|---------------|-------|
| C420  | N/A | Yes (assumed) | Unknown | Unknown | Talks to H500 on low-power wireless network (not on main LAN with its own IP). Stock Tapo app only so far. |

## Other Devices (Plugs, Sensors, Lights, etc.)

| Model | IP / via Hub | Type | Notes |
|-------|--------------|------|-------|
|       |              |      |       |

## Credentials Location

- Cloud TP-Link account: (describe where stored, e.g., password manager)
- Camera Accounts: (per-camera or shared?) — not yet created; still using stock Tapo app

## Network Notes

- Subnet: 192.168.11.0/24
- Any VLANs or isolation? 
- Static IPs preferred? no, unless needed
- Cameras connect to H500 via low-power wireless (not main WiFi/LAN)

## Known Issues / Quirks

- Newbie setup: still fully stock Tapo app on Android Pixel 7 + Samsung Tablet.
- Only H500 has an IP; cameras are not directly addressable on the LAN.
- Have not yet explored third-party local libraries or projects.

## Test Results

### H500 Recording Access
- Date tested:
- Library used:
- Result:

### Live RTSP
- Date tested:
- Result: (may not apply directly if cameras lack their own IP / RTSP endpoint)
