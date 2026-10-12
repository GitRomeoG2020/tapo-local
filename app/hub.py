"""Hub session shared by the dashboard.

Cameras and doorbells are loaded here. Plugs, lights, and live view can call
``HubSession.call`` for the same login instead of opening a second connection.

The account comes from ``TAPO_USERNAME`` and ``TAPO_PASSWORD``. This module
does not store or print those values. An expired hub session is logged in
again once, and the same request is tried again. A rejected password is not
retried.
"""

import os
import re
import shutil
import sys
import tempfile
import zipfile
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
    """One camera or doorbell the home page can link to.

    ``battery`` is a percentage from the same hub payload that listed the
    camera. It stays unset when that payload does not report one.
    """

    name: str
    model: str
    device_id: str
    hub_storage: str
    kind: str = "camera"
    battery: int | None = None

    @property
    def battery_label(self) -> str:
        """Percentage the hub reported, or an em dash when it did not."""
        if self.battery is None:
            return "—"
        return f"{self.battery}%"


@dataclass
class RecordingDay:
    """One day the hub reports as having recordings.

    ``start`` and ``end`` are the timezone-aware bounds ``get_recordings`` expects.
    """

    date: str
    start: datetime
    end: datetime


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
class DayDownloadProgress:
    """In-memory status for the zip that is currently being built.

    The page polls this while the zip response is still open. It is not stored
    on the hub and it does not change which clips go into the file.
    """

    device_id: str
    day: str
    completed: int
    total: int
    phase: str
    message: str


@dataclass
class DeviceRecordings:
    device: Device
    source: str
    days: list[RecordingDay]
    clips: list[Clip]
    clip_total: int
    newest_day: str
    selected_day: str


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


# Names the hub uses for a 0–100 battery reading. A bare "battery" value is
# accepted only when it is already a number in that range.
_BATTERY_KEYS = (
    "battery_percentage",
    "batteryPercentage",
    "battery_percent",
    "batteryPercent",
)


def _percent_number(value) -> int | None:
    """Return ``value`` as a percentage, or None when it is not 0–100."""
    if isinstance(value, bool) or isinstance(value, (dict, list)) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip().rstrip("%").strip()
        if not text:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    else:
        return None
    if number < 0 or number > 100:
        return None
    return int(round(number))


def _battery_from_mapping(data: dict) -> int | None:
    """Battery percentage from one device object, including one nested level."""
    for key in _BATTERY_KEYS:
        if key in data:
            found = _percent_number(data.get(key))
            if found is not None:
                return found
    battery = data.get("battery")
    if isinstance(battery, dict):
        for key in _BATTERY_KEYS:
            if key in battery:
                found = _percent_number(battery.get(key))
                if found is not None:
                    return found
        found = _percent_number(battery.get("battery"))
        if found is not None:
            return found
    else:
        found = _percent_number(battery)
        if found is not None:
            return found
    for value in data.values():
        if not isinstance(value, dict):
            continue
        for key in _BATTERY_KEYS:
            if key in value:
                found = _percent_number(value.get(key))
                if found is not None:
                    return found
    return None


def parse_battery(source) -> int | None:
    """0–100 when ``source`` reports a battery percentage, else None."""
    if isinstance(source, dict):
        return _battery_from_mapping(source)
    for key in (*_BATTERY_KEYS, "battery"):
        found = _percent_number(getattr(source, key, None))
        if found is not None:
            return found
    data = poc.as_dict(source)
    if data:
        return _battery_from_mapping(data)
    return None


def _camera_battery(source, device_id: str, battery_by_id: dict[str, int] | None) -> int | None:
    """Percentage on this row, or the same device's percentage from another list."""
    battery = parse_battery(source)
    if battery is None and battery_by_id:
        return battery_by_id.get(str(device_id))
    return battery


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


def select_day(days: list[RecordingDay], requested: str | None) -> RecordingDay | None:
    """Pick the requested day, or the newest day when ``requested`` is empty.

    The hub list is in calendar order, so the last entry is the newest. A day
    that is not in that list is rejected instead of querying an arbitrary range.
    """
    if not days:
        return None
    text = (requested or "").strip()
    if not text:
        return days[-1]
    for day in days:
        if day.date == text:
            return day
    raise BadRequest("That recording day was not found for this camera.")


def clips_from_recordings(recordings, limit: int | None = None) -> list[Clip]:
    """Turn hub recording rows into clip rows, keeping the last ``limit`` when set."""
    chosen = list(recordings)
    if limit is not None:
        chosen = chosen[-limit:]
    clips = []
    for recording in chosen:
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
    return clips


def write_clip_zip(folder: Path, parts: list[Path]) -> Path:
    """Pack MPEG-TS files into ``clips.zip`` inside ``folder``.

    Stored, not deflated: these files are already compressed video.
    """
    archive = folder / "clips.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as bundle:
        for path in parts:
            bundle.write(path, arcname=path.name)
    return archive


def day_progress_message(phase: str, completed: int, total: int, error: str = "") -> str:
    """Short status line for the download-all button."""
    if phase == "done":
        return "Zip ready. Your download should start shortly."
    if phase == "error":
        return error or "The zip could not be prepared."
    if phase == "zipping":
        return "Packing the zip… this can take several minutes."
    if total:
        return f"Fetched {completed} of {total} clips… this can take several minutes."
    return "Preparing zip… this can take several minutes."


def clip_archive_name(device_id: str, day: str) -> str:
    """Browser filename for a day's zip. The day is not used as a filesystem path."""
    safe_day = re.sub(r"[^0-9A-Za-z_-]", "", day) or "day"
    return f"clips-{device_id[:12]}-{safe_day}.zip"


def _recording_type(value) -> str:
    name = getattr(value, "name", None)
    if isinstance(name, str) and name:
        return name
    text = str(value or "").strip()
    return text or "unknown"


def cameras_from_json(
    rows: list[dict],
    storage_by_id: dict[str, str],
    battery_by_id: dict[str, int] | None = None,
) -> list[Device]:
    """Cameras and doorbells in a raw ``get_child_device_list_json`` payload.

    Hub storage is taken from the matching general-list entry when the raw
    row does not include the flag. Battery is read from the row, then from
    ``battery_by_id`` when the row omits it.
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
        cameras.append(
            Device(
                name,
                str(model),
                str(device_id),
                storage,
                battery=_camera_battery(row, str(device_id), battery_by_id),
            )
        )
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
        cameras.append(
            Device(name, str(model), str(device_id), label, battery=parse_battery(device))
        )
    return cameras, storage_by_id


def cameras_from_children(
    devices,
    storage_by_id: dict[str, str],
    battery_by_id: dict[str, int] | None = None,
) -> list[Device]:
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
                battery=_camera_battery(device, str(device_id), battery_by_id),
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
    """True when the cached hub login is stale and one new login can be tried.

    tapo reports that as ``Unauthorized`` with kind ``SESSION_TIMEOUT`` on a
    camera hub, or ``SESSION_EXPIRED`` (code ``-40401``) on an AES handshake.
    A wrong or locked password is ``Unauthorized`` too, and must not be retried.
    """
    if poc.is_auth_failure(err):
        return False
    text = str(err).lower()
    markers = (
        "session_timeout",
        "session_expired",
        "session expired",
        "session has expired",
        "-40401",
        "re-authentication is required",
    )
    if any(marker in text for marker in markers):
        return True
    # Older tapo builds reported the same expiry as HTTP 401.
    return "401" in text and ("unauthorized" in text or "http" in text)


class HubSession:
    """One cached login. Requests share a lock because the hub session is not concurrent."""

    def __init__(self) -> None:
        self._hub = None
        self._lock = None
        self._day_progress: DayDownloadProgress | None = None

    def _set_day_progress(
        self,
        device_id: str,
        day: str,
        completed: int,
        total: int,
        phase: str,
        error: str = "",
    ) -> None:
        self._day_progress = DayDownloadProgress(
            device_id,
            day,
            completed,
            total,
            phase,
            day_progress_message(phase, completed, total, error),
        )

    def day_download_progress(self, device_id: str, day: str) -> dict:
        """Progress for the zip in progress, or idle when this day is not the one running."""
        progress = self._day_progress
        text = (day or "").strip()
        if progress is None or progress.device_id != device_id or progress.day != text:
            return {
                "active": False,
                "completed": 0,
                "total": 0,
                "phase": "idle",
                "message": day_progress_message("preparing", 0, 0),
            }
        return {
            "active": True,
            "completed": progress.completed,
            "total": progress.total,
            "phase": progress.phase,
            "message": progress.message,
        }

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

    async def _reauthenticate(self, hub):
        """Open a fresh login after the cached session expires. One attempt."""
        refresher = getattr(hub, "refresh_session", None)
        if callable(refresher):
            try:
                await refresher()
                return hub
            except Exception as err:
                if poc.is_auth_failure(err):
                    self._hub = None
                    raise HubError(str(err)) from err
        self._hub = None
        return await self._open()

    async def call(self, operation):
        """Run one hub request. A stale session is logged in again and tried once more."""
        async with self._guard():
            hub = await self._open()
            try:
                return await operation(hub)
            except Exception as err:
                if not _session_expired(err):
                    if isinstance(err, HubError):
                        raise
                    raise HubError(str(err)) from err
                hub = await self._reauthenticate(hub)
            try:
                return await operation(hub)
            except HubError:
                raise
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
                if _session_expired(err):
                    raise
                json_rows = []
                errors.append(err)
            try:
                general_devices = await hub.get_general_device_list()
            except Exception as err:
                if _session_expired(err):
                    raise
                general_devices = []
                errors.append(err)
            general_cameras, storage_by_id = cameras_from_general(general_devices)
            battery_by_id = {
                camera.device_id: camera.battery
                for camera in general_cameras
                if camera.battery is not None
            }
            json_cameras = cameras_from_json(json_rows, storage_by_id, battery_by_id)
            if json_cameras:
                return choose_cameras(json_cameras, general_cameras, [])

            try:
                children = await hub.get_child_device_list()
            except Exception as err:
                if _session_expired(err):
                    raise
                children = []
                errors.append(err)
            if errors and not json_rows and not general_devices and not children:
                raise HubError(str(errors[0]))
            child_cameras = cameras_from_children(children, storage_by_id, battery_by_id)
            return choose_cameras(json_cameras, general_cameras, child_cameras)

        return await self.call(operation)

    async def get_device(self, device_id: str) -> tuple[Device, str]:
        device_id = _require_device_id(device_id)
        cameras, source = await self.list_cameras()
        for camera in cameras:
            if camera.device_id == device_id:
                return camera, source
        raise NotFound("That camera or doorbell is not paired to this hub.")

    async def recordings(
        self, device_id: str, limit: int, day: str | None = None
    ) -> DeviceRecordings:
        """Days in the lookback window, and the last ``limit`` clips on one day.

        With no day, that is the newest day. A day link passes that day's date
        and the same window the hub reported for it.
        """
        device, source = await self.get_device(device_id)
        limit = min(max(int(limit), 1), 50)

        async def operation(hub):
            day_rows = await self._recording_days(hub, device.device_id)
            selected = select_day(day_rows, day)
            clips = []
            clip_total = 0
            if selected is not None:
                recordings = await hub.get_recordings(
                    device.device_id, selected.start, selected.end
                )
                clip_total = len(recordings)
                clips = clips_from_recordings(recordings, limit)
            newest = day_rows[-1].date if day_rows else ""
            selected_day = selected.date if selected is not None else ""
            return DeviceRecordings(
                device, source, day_rows, clips, clip_total, newest, selected_day
            )

        return await self.call(operation)

    async def _recording_days(self, hub, device_id: str) -> list[RecordingDay]:
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(days=poc.LOOKBACK_DAYS)
        days = await hub.get_recording_dates(device_id, start_time, end_time)
        return [
            RecordingDay(str(day.date), day.start_time, day.end_time) for day in days
        ]

    async def download_day(self, device_id: str, day: str) -> Path:
        """Download every clip on one recording day and return a zip path.

        The day must be one ``get_recording_dates`` returned. Clips are fetched
        one at a time on the shared hub session, then packed into the zip.
        """
        text = (day or "").strip()
        if not text:
            raise BadRequest("Choose a recording day to download.")
        # Visible to the progress poll before the first clip request returns.
        self._set_day_progress(device_id, text, 0, 0, "preparing")
        folder: Path | None = None
        try:
            device, _source = await self.get_device(device_id)
            folder = Path(tempfile.mkdtemp(prefix="h500-day-"))

            async def operation(hub):
                selected = select_day(
                    await self._recording_days(hub, device.device_id), text
                )
                if selected is None:
                    raise BadRequest("That day has no clips to download.")
                recordings = await hub.get_recordings(
                    device.device_id, selected.start, selected.end
                )
                if not recordings:
                    raise BadRequest("That day has no clips to download.")
                total = len(recordings)
                self._set_day_progress(device.device_id, text, 0, total, "fetching")
                parts = []
                for index, recording in enumerate(recordings, start=1):
                    stamp = recording.start_time.astimezone(timezone.utc).strftime(
                        "%Y%m%dT%H%M%SZ"
                    )
                    target = folder / f"{index:03d}-{stamp}.ts"
                    result = await hub.download_recording(
                        device.device_id,
                        recording.start_time,
                        recording.end_time,
                        target,
                    )
                    if getattr(result, "byte_count", 0) <= 0 or not target.is_file():
                        raise HubError(f"The hub did not send clip {target.name}.")
                    parts.append(target)
                    self._set_day_progress(
                        device.device_id, text, index, total, "fetching"
                    )
                self._set_day_progress(device.device_id, text, total, total, "zipping")
                archive = write_clip_zip(folder, parts)
                for path in parts:
                    path.unlink(missing_ok=True)
                return archive

            path = await self.call(operation)
            current = self._day_progress
            total = current.total if current is not None else 0
            self._set_day_progress(device_id, text, total, total, "done")
            return path
        except Exception as err:
            current = self._day_progress
            completed = current.completed if current is not None else 0
            total = current.total if current is not None else 0
            self._set_day_progress(device_id, text, completed, total, "error", str(err))
            if folder is not None:
                shutil.rmtree(folder, ignore_errors=True)
            raise

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
    """Delete a downloaded clip or zip and its temp folder after the response is sent."""
    folder = path.parent
    if folder.name.startswith(("h500-clip-", "h500-day-")):
        shutil.rmtree(folder, ignore_errors=True)
        return
    path.unlink(missing_ok=True)
    try:
        folder.rmdir()
    except OSError:
        pass


session = HubSession()
