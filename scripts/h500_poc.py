"""List cameras paired to the local H500 and show recent hub recordings.

Uses the community ``tapo`` package (https://github.com/mihai-dinculescu/tapo).
The Tapo account is read from the environment. This file stores no secrets.

Requires tapo >= 0.10 (H500 recording support). tapo 0.11.1 is current on PyPI.
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

# LAN address of the hub documented in DEVICES.md. This is not a credential.
H500_IP = "192.168.11.223"

# How far back to ask the hub which days have recordings.
LOOKBACK_DAYS = 7

# A busy day can return hundreds of clips. Print only the tail of that list.
MAX_CLIPS = 20

# Discovery wait used only when a direct login cannot open a session.
DISCOVERY_TIMEOUT_S = 10


def credentials_from_env() -> tuple[str, str]:
    """Return TAPO_USERNAME and TAPO_PASSWORD, or exit if either is missing.

    Values are never printed. An empty string counts as missing so a blank
    variable does not get sent to the hub as a password.
    """
    missing = [
        name
        for name in ("TAPO_USERNAME", "TAPO_PASSWORD")
        if not os.getenv(name, "").strip()
    ]
    if missing:
        print(
            "Missing environment variable(s): "
            + ", ".join(missing)
            + ".\nSet TAPO_USERNAME and TAPO_PASSWORD to the cloud TP-Link "
            "account that owns this hub, then run the script again.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return os.environ["TAPO_USERNAME"], os.environ["TAPO_PASSWORD"]


def is_auth_failure(err: BaseException) -> bool:
    """True when the hub rejected the account, so a second login must not run.

    tapo reports a wrong password as TPAP_CREDENTIALS and a locked device as
    TPAP_AUTH_ATTEMPTS_LIMIT. A locked hub refuses every login for a while.
    Protocol mismatches are left out so the discovery fallback can still run.
    """
    text = str(err).lower()
    markers = (
        "tpap_credentials",
        "tpap_auth_attempts_limit",
        "tpap_hash_mismatch",
        "invalid credential",
        "-1501",
    )
    return any(marker in text for marker in markers)


async def connect(client, ip: str):
    """Open a CameraHubHandler for the H500 at ``ip``.

    ``ApiClient.h500`` logs in by address over AES SSL. tapo 0.11 still speaks
    TPAP to an H200/H500 only when the hub is found by ``discover_devices``.
    Recent firmware requires TPAP when Third-Party Compatibility is off in the
    Tapo app, so a failed direct login falls back to one subnet scan and keeps
    the hub whose address is ``ip``. Auth failures are not retried.
    """
    try:
        return await client.h500(ip)
    except Exception as direct_error:
        if is_auth_failure(direct_error):
            print(f"Login to {ip} was rejected: {direct_error}", file=sys.stderr)
            print(
                "Check TAPO_USERNAME and TAPO_PASSWORD. "
                "Do not retry in a loop if the hub says it is locked.",
                file=sys.stderr,
            )
            raise SystemExit(1) from direct_error
        print(f"Direct login to {ip} failed: {direct_error}", file=sys.stderr)

    broadcast = ip.rsplit(".", 1)[0] + ".255"
    print(
        f"Scanning {broadcast} for {DISCOVERY_TIMEOUT_S}s. "
        "tapo uses TPAP for an H500 when discovery finds it.",
        file=sys.stderr,
    )
    try:
        found = await client.discover_devices(broadcast, DISCOVERY_TIMEOUT_S)
    except Exception as discover_error:
        print(f"Discovery on {broadcast} failed: {discover_error}", file=sys.stderr)
        raise SystemExit(1) from discover_error

    # Imported here so a missing package fails in main() with a clear message
    # before any of this runs. DiscoveryResult.CameraHub is an H200 or H500.
    from tapo import DiscoveryResult

    async for item in found:
        try:
            device = item.get()
        except Exception as item_error:
            print(f"Skipping one discovery reply: {item_error}", file=sys.stderr)
            continue
        # Camera hubs expose the address on device info. The shared `.ip`
        # accessor is there too; either one is enough to match this hub.
        address = getattr(device, "ip", None)
        if address is None and getattr(device, "device_info", None) is not None:
            address = getattr(device.device_info, "ip", None)
        if isinstance(device, DiscoveryResult.CameraHub) and address == ip:
            return device.handler

    print(f"No H500 responded at {ip}.", file=sys.stderr)
    raise SystemExit(1)


def clip_line(recording) -> str:
    """One clip as start time, length, and the hub's recording type."""
    start = recording.start_time
    end = recording.end_time
    length = int((end - start).total_seconds())
    kind = getattr(recording, "video_type", "")
    return f"{start.isoformat()}  {length}s  {kind}"


async def show_recordings(hub, camera) -> None:
    """Print recording days in the lookback window and clips on the newest day.

    Days come from ``get_recording_dates``. Each day carries the time range to
    pass to ``get_recordings``. Both calls need timezone-aware datetimes.
    A failure for one camera is reported and does not stop the others.
    """
    alias = camera.alias or "(unnamed)"
    storage = "on" if camera.hub_storage_enabled else "off"
    print(
        f"\n{alias}  model={camera.device_model}  "
        f"id={camera.device_id}  hub_storage={storage}"
    )
    if not camera.hub_storage_enabled:
        print("  Hub storage is off for this camera, so it has no hub clips.")
        return

    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(days=LOOKBACK_DAYS)
    try:
        days = await hub.get_recording_dates(camera.device_id, start_time, end_time)
    except Exception as err:
        print(f"  Could not list recording days: {err}", file=sys.stderr)
        return

    if not days:
        print(f"  No recordings in the last {LOOKBACK_DAYS} days.")
        return

    print(f"  Recording days ({len(days)}):")
    for day in days:
        print(f"    {day.date}")

    # The hub returns days in calendar order. The last entry is the newest.
    latest = days[-1]
    try:
        clips = await hub.get_recordings(
            camera.device_id, latest.start_time, latest.end_time
        )
    except Exception as err:
        print(f"  Could not list clips for {latest.date}: {err}", file=sys.stderr)
        return

    print(f"  Clips on {latest.date}: {len(clips)}")
    shown = clips[-MAX_CLIPS:]
    if len(clips) > MAX_CLIPS:
        print(f"  Showing the last {MAX_CLIPS} in hub order:")
    for clip in shown:
        print(f"    {clip_line(clip)}")


async def main() -> None:
    username, password = credentials_from_env()

    try:
        from tapo import ApiClient
    except ImportError:
        print(
            'The tapo package is not installed. Run: python -m pip install "tapo>=0.10"',
            file=sys.stderr,
        )
        raise SystemExit(1)

    print(f"Connecting to H500 at {H500_IP}...")
    client = ApiClient(username, password)
    try:
        hub = await connect(client, H500_IP)
        info = (await hub.get_device_info()).to_dict()
    except SystemExit:
        raise
    except Exception as err:
        print(f"Failed to connect to the H500 at {H500_IP}: {err}", file=sys.stderr)
        raise SystemExit(1) from err

    # Device info has model and nickname. It does not include the account password.
    model = info.get("model", "H500")
    nickname = info.get("nickname") or model
    firmware = info.get("fw_ver") or info.get("firmware_version") or "unknown firmware"
    print(f"Connected to {nickname} ({model}, {firmware}).")

    try:
        cameras = await hub.get_general_device_list()
    except Exception as err:
        print(f"Connected, but listing paired cameras failed: {err}", file=sys.stderr)
        raise SystemExit(1) from err

    if not cameras:
        print("No cameras are paired to this hub.")
        return

    print(f"Paired cameras: {len(cameras)}")
    for camera in cameras:
        await show_recordings(hub, camera)


if __name__ == "__main__":
    asyncio.run(main())
