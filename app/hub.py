"""Hub session shared by the dashboard.

Cameras and doorbells are loaded here. Plugs, lights, and live view can call
``HubSession.call`` for the same login instead of opening a second connection.

The account comes from ``TAPO_USERNAME`` and ``TAPO_PASSWORD``. This module
does not store or print those values.
"""

import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.h500_poc as poc

# Tapo device ids are hex. Reject anything else before it reaches a file path
# or a hub request that came from the query string.
_DEVICE_ID = re.compile(r"^[A-Fa-f0-9]{8,64}$")


class HubError(Exception):
    """A hub or request failure the page can show."""

    status_code = 502


class BadRequest(HubError):
    status_code = 400


class NotFound(HubError):
    status_code = 404


@dataclass
class Device:
    """One camera or doorbell the home page can link to."""

    name: str
    model: str
    device_id: str
    hub_storage: str
    kind: str = "camera"


@dataclass
class RecordingDay:
    date: str


@dataclass
class Clip:
    start: datetime
    end: datetime
    duration_s: int
    video_type: str

    @property
    def start_value(self) -> str:
        return _stamp(self.start)

    @property
    def end_value(self) -> str:
        return _stamp(self.end)

    @property
    def duration_label(self) -> str:
        return format_duration(self.duration_s)

    @property
    def timestamp_label(self) -> str:
        return self.start.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


@dataclass
class DeviceRecordings:
    device: Device
    source: str
    days: list[RecordingDay]
    clips: list[Clip]
    clip_total: int
    newest_day: str


def format_duration(seconds: int) -> str:
    """Compact length for a clip, such as ``1m 5s``."""
    remaining = max(0, int(seconds))
    minutes, secs = divmod(remaining, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _stamp(value: datetime) -> str:
    """UTC timestamp with a Z suffix so a form post does not treat + as a space."""
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_timestamp(value: str) -> datetime:
    """Parse a timezone-aware timestamp from a clip form."""
    text = (value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as err:
        raise BadRequest("Clip time is not a valid timestamp.") from err
    if parsed.tzinfo is None:
        raise BadRequest("Clip time must include a timezone.")
    return parsed


def storage_label(value) -> str:
    """Normalize the hub-storage flag to on, off, or not reported."""
    if value is None or value == "":
        return "not reported"
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "on", "enabled", "yes"}:
            return "on"
        if lowered in {"0", "false", "off", "disabled", "no"}:
            return "off"
        return value
    return "on" if value else "off"


def _require_device_id(device_id: str) -> str:
    if not _DEVICE_ID.fullmatch(device_id or ""):
        raise NotFound("That device id is not valid.")
    return device_id


def _recording_type(value) -> str:
    name = getattr(value, "name", None)
    if isinstance(name, str) and name:
        return name
    text = str(value or "").strip()
    return text or "unknown"


def cameras_from_json(rows: list[dict], storage_by_id: dict[str, str]) -> list[Device]:
    """Cameras and doorbells in a raw ``get_child_device_list_json`` payload.

    Hub storage is taken from the matching general-list entry when the raw
    row does not include the flag.
    """
    cameras = []
    seen = set()
    for row in rows:
        device_id = poc.first_value(row, "device_id", "deviceId")
        if not device_id or device_id in seen:
            continue
        model = poc.first_value(row, "device_model", "model") or "(unknown model)"
        category = poc.first_value(row, "category", "device_type", "type") or ""
        if not poc.looks_like_camera(model, str(category)):
            continue
        seen.add(device_id)
        raw_storage = poc.first_value(
            row, "hub_storage_enabled", "hubStorageEnabled", "storage_status"
        )
        storage = storage_label(raw_storage)
        if storage == "not reported":
            storage = storage_by_id.get(device_id, "not reported")
        name = poc.display_name(poc.first_value(row, "alias", "nickname", "name"))
        cameras.append(Device(name, str(model), str(device_id), storage))
    return cameras


def cameras_from_general(devices) -> tuple[list[Device], dict[str, str]]:
    """Typed general-list cameras, plus a device-id map of hub storage."""
    cameras = []
    storage_by_id = {}
    for device in devices or []:
        data = poc.as_dict(device)
        device_id = poc.first_value(device, "device_id") or poc.first_value(data, "device_id")
        if not device_id:
            continue
        storage = getattr(device, "hub_storage_enabled", None)
        if storage is None:
            storage = data.get("hub_storage_enabled")
        label = storage_label(storage)
        storage_by_id[str(device_id)] = label
        name = poc.display_name(
            poc.first_value(device, "alias", "nickname")
            or poc.first_value(data, "alias", "nickname")
        )
        model = (
            poc.first_value(device, "device_model", "model")
            or poc.first_value(data, "device_model", "model")
            or "(unknown model)"
        )
        cameras.append(Device(name, str(model), str(device_id), label))
    return cameras, storage_by_id


def cameras_from_children(devices, storage_by_id: dict[str, str]) -> list[Device]:
    """Camera-like entries from the typed child list."""
    cameras = []
    for device in devices or []:
        data = poc.as_dict(device)
        device_id = poc.first_value(device, "device_id") or poc.first_value(data, "device_id")
        model = (
            poc.first_value(device, "model", "device_model")
            or poc.first_value(data, "model", "device_model")
            or ""
        )
        category = poc.first_value(device, "category", "type") or poc.first_value(
            data, "category", "type"
        )
        kind = type(device).__name__
        if not device_id or not poc.looks_like_camera(model, f"{category or ''} {kind}"):
            continue
        name = poc.display_name(
            poc.first_value(device, "nickname", "alias")
            or poc.first_value(data, "nickname", "alias")
        )
        cameras.append(
            Device(
                name,
                str(model or "(unknown model)"),
                str(device_id),
                storage_by_id.get(str(device_id), "not reported"),
            )
        )
    return cameras


def choose_cameras(
    json_cameras: list[Device],
    general_cameras: list[Device],
    child_cameras: list[Device],
) -> tuple[list[Device], str]:
    """Prefer the raw child list, then the general list, then the typed child list."""
    if json_cameras:
        return json_cameras, "get_child_device_list_json()"
    if general_cameras:
        return general_cameras, "get_general_device_list()"
    if child_cameras:
        return child_cameras, "get_child_device_list()"
    return [], "get_child_device_list_json()"


async def fetch_json_rows(hub) -> list[dict]:
    """Read every page of ``get_child_device_list_json`` when the method exists.

    Pagination matches ``scripts/h500_poc.py``: 10 devices per page, and a
    short page or a reported total ends the walk.
    """
    method = getattr(hub, "get_child_device_list_json", None)
    if not callable(method):
        return []

    rows = []
    seen = set()
    start = 0
    for _ in range(poc.JSON_PAGE_LIMIT):
        page = await method(start)
        found = poc._json_devices(page)
        for device in found:
            device_id = poc.first_value(device, "device_id", "deviceId")
            if not device_id or device_id in seen:
                continue
            seen.add(device_id)
            rows.append(device)
        total = page.get("sum") if isinstance(page, dict) else None
        if len(found) < poc.JSON_PAGE_SIZE:
            break
        start += poc.JSON_PAGE_SIZE
        if isinstance(total, int) and start >= total:
            break
    return rows


def _session_expired(err: BaseException) -> bool:
    text = str(err).lower()
    return "session_timeout" in text or "session expired" in text


class HubSession:
    """One cached login. Requests share a lock because the hub session is not concurrent."""

    def __init__(self) -> None:
        self._hub = None
        self._lock = None

    def _guard(self):
        # Created lazily so importing this module does not require a running loop.
        import asyncio

        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def _open(self):
        if self._hub is not None:
            return self._hub

        username = os.getenv("TAPO_USERNAME", "").strip()
        password = os.getenv("TAPO_PASSWORD", "").strip()
        if not username or not password:
            raise HubError(
                "Set TAPO_USERNAME and TAPO_PASSWORD to the cloud TP-Link "
                "account that owns this hub."
            )
        try:
            from tapo import ApiClient
        except ImportError as err:
            raise HubError(
                'The tapo package is not installed. Run: python -m pip install "tapo>=0.10"'
            ) from err

        # connect() prints the library error and raises SystemExit. Catch that
        # so a failed login stays on the page instead of stopping the server.
        try:
            self._hub = await poc.connect(ApiClient(username, password), poc.H500_IP)
        except SystemExit as err:
            self._hub = None
            cause = err.__cause__ or err.__context__
            detail = f" {cause}" if cause else ""
            raise HubError(f"Could not log in to the H500 at {poc.H500_IP}.{detail}") from err
        return self._hub

    async def call(self, operation):
        async with self._guard():
            hub = await self._open()
            try:
                return await operation(hub)
            except HubError:
                raise
            except Exception as err:
                if poc.is_auth_failure(err) or not _session_expired(err):
                    raise HubError(str(err)) from err
                self._hub = None
                hub = await self._open()
                try:
                    return await operation(hub)
                except Exception as retry_err:
                    raise HubError(str(retry_err)) from retry_err

    async def list_cameras(self) -> tuple[list[Device], str]:
        """List cameras, preferring ``get_child_device_list_json``."""

        async def operation(hub):
            # JSON is preferred. A failure there still leaves the general list
            # and the typed child list as fallbacks.
            errors = []
            try:
                json_rows = await fetch_json_rows(hub)
            except Exception as err:
                json_rows = []
                errors.append(err)
            try:
                general_devices = await hub.get_general_device_list()
            except Exception as err:
                general_devices = []
                errors.append(err)
            general_cameras, storage_by_id = cameras_from_general(general_devices)
            json_cameras = cameras_from_json(json_rows, storage_by_id)
            if json_cameras:
                return choose_cameras(json_cameras, general_cameras, [])

            try:
                children = await hub.get_child_device_list()
            except Exception as err:
                children = []
                errors.append(err)
            if errors and not json_rows and not general_devices and not children:
                raise HubError(str(errors[0]))
            child_cameras = cameras_from_children(children, storage_by_id)
            return choose_cameras(json_cameras, general_cameras, child_cameras)

        return await self.call(operation)

    async def get_device(self, device_id: str) -> tuple[Device, str]:
        device_id = _require_device_id(device_id)
        cameras, source = await self.list_cameras()
        for camera in cameras:
            if camera.device_id == device_id:
                return camera, source
        raise NotFound("That camera or doorbell is not paired to this hub.")

    async def recordings(self, device_id: str, limit: int) -> DeviceRecordings:
        """Days in the lookback window, and the last ``limit`` clips on the newest day."""
        device, source = await self.get_device(device_id)
        limit = min(max(int(limit), 1), 50)

        async def operation(hub):
            end_time = datetime.now(timezone.utc)
            start_time = end_time - timedelta(days=poc.LOOKBACK_DAYS)
            days = await hub.get_recording_dates(device.device_id, start_time, end_time)
            day_rows = [RecordingDay(str(day.date)) for day in days]
            clips = []
            clip_total = 0
            newest = ""
            if days:
                latest = days[-1]
                newest = str(latest.date)
                recordings = await hub.get_recordings(
                    device.device_id, latest.start_time, latest.end_time
                )
                clip_total = len(recordings)
                for recording in recordings[-limit:]:
                    start = recording.start_time
                    end = recording.end_time
                    clips.append(
                        Clip(
                            start=start,
                            end=end,
                            duration_s=int((end - start).total_seconds()),
                            video_type=_recording_type(getattr(recording, "video_type", "")),
                        )
                    )
            return DeviceRecordings(device, source, day_rows, clips, clip_total, newest)

        return await self.call(operation)

    async def download_clip(self, device_id: str, start: datetime, end: datetime) -> Path:
        """Save one hub recording as MPEG-TS and return the temp file path."""
        if end <= start:
            raise BadRequest("The clip end time must be after the start time.")
        device, _source = await self.get_device(device_id)
        folder = Path(tempfile.mkdtemp(prefix="h500-clip-"))
        target = folder / "clip.ts"

        async def operation(hub):
            result = await hub.download_recording(device.device_id, start, end, target)
            if getattr(result, "byte_count", 0) <= 0 or not target.is_file():
                raise HubError("The hub did not send any recording data.")
            return target

        try:
            return await self.call(operation)
        except Exception:
            target.unlink(missing_ok=True)
            try:
                folder.rmdir()
            except OSError:
                pass
            raise


def remove_download(path: Path) -> None:
    """Delete a downloaded clip and its temp folder after the response is sent."""
    path.unlink(missing_ok=True)
    try:
        path.parent.rmdir()
    except OSError:
        pass


session = HubSession()
