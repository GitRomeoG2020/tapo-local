"""List cameras paired to the local H500 and show recent hub recordings.

Uses the community ``tapo`` package (https://github.com/mihai-dinculescu/tapo).
The Tapo account is read from the environment. This file stores no secrets.

Requires tapo >= 0.10 (H500 recording support). tapo 0.11.1 is current on PyPI.
"""

import asyncio
import base64
import os
import re
import sys
from datetime import datetime, timedelta, timezone

# How far back to ask the hub which days have recordings.
LOOKBACK_DAYS = 7

# A busy day can return hundreds of clips. Print only the tail of that list.
MAX_CLIPS = 20

# Discovery wait used only when a direct login cannot open a session.
DISCOVERY_TIMEOUT_S = 10

# get_child_device_list_json pages 10 children at a time. Stop well past the
# H500's child-device capacity so a short page or a bad sum cannot loop.
JSON_PAGE_SIZE = 10
JSON_PAGE_LIMIT = 8

# Tapo camera and doorbell model prefixes. Child-list entries use these when
# the typed parser does not already label them as cameras.
_CAMERA_MODEL = re.compile(r"^(C\d|TC\d|D\d)", re.IGNORECASE)
_IPV4 = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")


class HubAddressError(Exception):
    """TAPO_HUB_IP is missing or is not an IPv4 address."""


def hub_ip() -> str:
    """Return the H500 address from TAPO_HUB_IP.

    The value is not a password. A missing or invalid address raises
    ``HubAddressError`` with a message the page or the script can show.
    """
    value = os.getenv("TAPO_HUB_IP", "").strip()
    if not value:
        raise HubAddressError("Set TAPO_HUB_IP to the LAN address of the H500 hub.")
    if not _IPV4.fullmatch(value) or any(int(part) > 255 for part in value.split(".")):
        raise HubAddressError(
            "TAPO_HUB_IP must be an IPv4 address, for example 192.168.1.10."
        )
    return value


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


class CameraRecord:
    """The four fields ``show_recordings`` already reads off a hub camera."""

    def __init__(self, alias, device_model, device_id, hub_storage_enabled):
        self.alias = alias or "(unnamed)"
        self.device_model = device_model or "(unknown model)"
        self.device_id = device_id
        self.hub_storage_enabled = hub_storage_enabled


def display_name(value) -> str:
    """Return a nickname, decoding the base64 form the hub uses in raw JSON."""
    if not isinstance(value, str) or not value.strip():
        return "(unnamed)"
    text = value.strip()
    if re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", text) and len(text) % 4 == 0:
        try:
            decoded = base64.b64decode(text, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return text
        if decoded and decoded.isprintable():
            return decoded
    return text


def as_dict(device) -> dict:
    """Return ``to_dict()`` when the library object provides it."""
    to_dict = getattr(device, "to_dict", None)
    if not callable(to_dict):
        return {}
    try:
        data = to_dict()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def first_value(source, *names):
    """First present attribute or dict entry among ``names``."""
    for name in names:
        if isinstance(source, dict):
            value = source.get(name)
        else:
            value = getattr(source, name, None)
        if value not in (None, ""):
            return value
    return None


def looks_like_camera(model, category="") -> bool:
    """True for a Tapo camera or doorbell model, or an explicit camera category.

    ``get_general_device_list`` is already the camera list, so this check is
    only for the child-device calls. Sensors such as T100 and T310 stay out.
    """
    blob = f"{model or ''} {category or ''}".lower()
    if any(word in blob for word in ("camera", "ipc", "doorbell")):
        return True
    return bool(_CAMERA_MODEL.match(str(model or "")))


def print_device_line(name, model, device_id, extra="") -> None:
    suffix = f"  {extra}" if extra else ""
    print(f"  name={display_name(name)}  model={model or '(unknown model)'}  id={device_id or '(no id)'}{suffix}")


def summarize_general_devices(devices) -> list[CameraRecord]:
    """Print the camera list and return a record for each entry.

    ``get_general_device_list`` returns the standalone cameras paired to an
    H200 or H500. These are the device IDs ``get_recording_dates`` and
    ``get_recordings`` take. Each entry has an alias, a model, an id, and
    whether hub storage is turned on for that camera.
    """
    cameras = []
    print(f"get_general_device_list(): {len(devices)} device(s)")
    if not devices:
        print("  (none)")
        return cameras
    for device in devices:
        data = as_dict(device)
        name = first_value(device, "alias", "nickname") or first_value(data, "alias", "nickname")
        model = first_value(device, "device_model", "model") or first_value(data, "device_model", "model")
        device_id = first_value(device, "device_id") or first_value(data, "device_id")
        storage = getattr(device, "hub_storage_enabled", None)
        if storage is None:
            storage = data.get("hub_storage_enabled")
        storage_label = "not reported" if storage is None else ("on" if storage else "off")
        print_device_line(name, model, device_id, f"hub_storage={storage_label}")
        if device_id:
            # An unreported flag still gets a recording query. An explicit
            # false is the only case show_recordings skips.
            cameras.append(
                CameraRecord(display_name(name), model, device_id, storage is not False)
            )
    return cameras


def summarize_child_devices(devices) -> list[CameraRecord]:
    """Print sensors, switches, and any camera-like child, and return the cameras.

    ``get_child_device_list`` returns the hub's child accessories: buttons,
    sensors, switches, and other sub-GHz devices. The typed parser does not
    treat cameras as children; the library points those at
    ``get_general_device_list``. A model it does not recognize comes back as
    ``OtherResult`` with a nickname, model, and id, so a battery camera can
    still appear here. Only entries that look like cameras are returned.
    """
    cameras = []
    print(f"get_child_device_list(): {len(devices)} device(s)")
    if not devices:
        print("  (none)")
        return cameras
    for device in devices:
        data = as_dict(device)
        name = first_value(device, "nickname", "alias") or first_value(data, "nickname", "alias")
        model = first_value(device, "model", "device_model") or first_value(data, "model", "device_model")
        device_id = first_value(device, "device_id") or first_value(data, "device_id")
        category = first_value(device, "category", "type") or first_value(data, "category", "type")
        kind = type(device).__name__
        print_device_line(name, model, device_id, f"type={kind}")
        if device_id and looks_like_camera(model, f"{category or ''} {kind}"):
            # Child entries do not report hub_storage_enabled. Leave it on so
            # the unchanged recording query still runs for this camera.
            cameras.append(CameraRecord(display_name(name), model, device_id, True))
    return cameras


def _json_devices(node) -> list[dict]:
    """Dicts in a raw child-list page that carry an id and a name or model."""
    found = []

    def walk(value) -> None:
        if isinstance(value, dict):
            device_id = first_value(value, "device_id", "deviceId")
            name = first_value(value, "alias", "nickname", "name")
            model = first_value(value, "device_model", "model")
            if device_id and (name or model):
                found.append(value)
                return
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(node)
    return found


async def summarize_child_device_json(hub) -> list[CameraRecord]:
    """Print the raw child list when the debug method exists.

    ``get_child_device_list_json`` is the same child list with no typed
    filtering, one page at a time. tapo-py ships it because the debug methods
    are enabled. A build without it is reported and skipped.
    """
    method = getattr(hub, "get_child_device_list_json", None)
    if not callable(method):
        print("get_child_device_list_json(): not available on this tapo build.")
        return []

    pages = []
    start = 0
    try:
        for _ in range(JSON_PAGE_LIMIT):
            page = await method(start)
            pages.append(page)
            found_here = _json_devices(page)
            total = page.get("sum") if isinstance(page, dict) else None
            if len(found_here) < JSON_PAGE_SIZE:
                break
            start += JSON_PAGE_SIZE
            if isinstance(total, int) and start >= total:
                break
    except Exception as err:
        print(f"get_child_device_list_json() failed: {err}", file=sys.stderr)
        if not pages:
            return []

    seen = set()
    rows = []
    for page in pages:
        for device in _json_devices(page):
            device_id = first_value(device, "device_id", "deviceId")
            if device_id in seen:
                continue
            seen.add(device_id)
            rows.append(device)

    print(f"get_child_device_list_json(): {len(rows)} device(s) across {len(pages)} page(s)")
    if not rows:
        print("  (none)")
        if pages and isinstance(pages[0], dict):
            print(f"  page keys: {', '.join(str(key) for key in pages[0])}")
        return []

    cameras = []
    for device in rows:
        name = first_value(device, "alias", "nickname", "name")
        model = first_value(device, "device_model", "model")
        device_id = first_value(device, "device_id", "deviceId")
        category = first_value(device, "category", "device_type", "type") or ""
        extra = f"category={category}" if category else ""
        print_device_line(name, model, device_id, extra)
        if looks_like_camera(model, str(category)):
            cameras.append(CameraRecord(display_name(name), model, device_id, True))
    return cameras


def cameras_for_recordings(general, children, raw_children) -> tuple[list[CameraRecord], str]:
    """Pick the call that actually listed cameras.

    The general list is the camera list, so it wins when it has entries. The
    child calls are the fallback for a camera that only shows up as an untyped
    child. When the raw JSON page lists more cameras than the typed child
    list, the raw page is the one that contains them.
    """
    if general:
        return general, "get_general_device_list()"
    if raw_children and len(raw_children) > len(children):
        return raw_children, "get_child_device_list_json()"
    if children:
        return children, "get_child_device_list()"
    if raw_children:
        return raw_children, "get_child_device_list_json()"
    return [], ""


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

    try:
        address = hub_ip()
    except HubAddressError as err:
        print(err, file=sys.stderr)
        raise SystemExit(1) from err

    print(f"Connecting to H500 at {address}...")
    client = ApiClient(username, password)
    try:
        hub = await connect(client, address)
        info = (await hub.get_device_info()).to_dict()
    except SystemExit:
        raise
    except Exception as err:
        print(f"Failed to connect to the H500 at {address}: {err}", file=sys.stderr)
        raise SystemExit(1) from err

    # Device info has model and nickname. It does not include the account password.
    model = info.get("model", "H500")
    nickname = info.get("nickname") or model
    firmware = info.get("fw_ver") or info.get("firmware_version") or "unknown firmware"
    print(f"Connected to {nickname} ({model}, {firmware}).")

    # Two different lists. The general list is the cameras. The child list is
    # sensors and switches; its raw JSON is the same list with nothing filtered
    # out. Ask all of them, then query recordings from the one that has cameras.
    print()
    try:
        general_devices = await hub.get_general_device_list()
    except Exception as err:
        print(f"get_general_device_list() failed: {err}", file=sys.stderr)
        general_devices = None
    general_cameras = [] if general_devices is None else summarize_general_devices(general_devices)

    print()
    try:
        child_devices = await hub.get_child_device_list()
    except Exception as err:
        print(f"get_child_device_list() failed: {err}", file=sys.stderr)
        child_devices = None
    child_cameras = [] if child_devices is None else summarize_child_devices(child_devices)

    print()
    raw_cameras = await summarize_child_device_json(hub)

    if general_devices is None and child_devices is None and not raw_cameras:
        print("Could not list devices from the hub.", file=sys.stderr)
        raise SystemExit(1)

    cameras, source = cameras_for_recordings(general_cameras, child_cameras, raw_cameras)
    print()
    if not cameras:
        print("No cameras were found in any list. Recordings were not queried.")
        return

    print(f"Using {source} for recordings ({len(cameras)} camera(s)).")
    for camera in cameras:
        await show_recordings(hub, camera)


if __name__ == "__main__":
    asyncio.run(main())
