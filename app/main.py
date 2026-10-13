"""Local FastAPI dashboard for cameras and recordings on the Tapo H500.

Run from the project root:

    python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

Routes:
    GET  /                                 paired cameras and doorbells
    GET  /live                             links to go2rtc live streams
    GET  /live/multi                       tiled go2rtc players for every stream
    GET  /live/thumbnail/{stream_name}     one go2rtc frame, or a placeholder image
    POST /live/sync                        add missing cameras to bin/go2rtc.yaml
    GET  /controls                         plugs, lights, and other non-camera devices
    POST /controls/power                   on, off, brightness, color, or color temperature
    GET  /status                           hub, go2rtc, ffmpeg, and uptime checks
    GET  /about                            project description and credits
    GET  /devices/{device_id}              recording days and one day's clips
    GET  /devices/{device_id}/thumbnail    one go2rtc frame, or a placeholder image
    POST /devices/{device_id}/download     one clip as MPEG-TS
    POST /devices/{device_id}/download-day every clip from one day as a zip
    GET  /devices/{device_id}/download-day/progress
                                           clip counter for that zip

Every page shares the top navigation. Live View reads stream names from
``bin/go2rtc.yaml`` and links each one to the go2rtc player on port 1984 of
the same host. Stream URL values stay in that file. Sync cameras appends
any paired camera that is not already listed there, and the page does not
show those URLs. Camera thumbnails are a separate request: the hub library
has no snapshot for an H500 child, so the thumbnail route asks go2rtc for
one JPEG frame and returns a placeholder image when that fails. Plugs and lights are
listed by ``app.controls`` from ``devices.txt`` or the H500 subnet, plus any
devices in ``extra_devices.yaml``. They use the same account variables and
do not use the hub recording session.
"""

import asyncio
import hashlib
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask

from app.controls import ControlError, device_cards, session as control_session
from app.status import build_status_checks
from app.hub import (
    BadRequest,
    HubError,
    clip_archive_name,
    parse_timestamp,
    remove_download,
    session,
)
import scripts.h500_poc as poc
from scripts.h500_poc import LOOKBACK_DAYS

ROOT = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(ROOT / "templates"))

GO2RTC_PORT = 1984
GO2RTC_CONFIG = ROOT / "bin" / "go2rtc.yaml"
# A sleeping battery camera, and a Pi encoding that frame, can take a while.
# The cameras page does not wait on this. One thumbnail request gets 15
# seconds and a single retry, then the card shows a placeholder.
FRAME_TIMEOUT_S = 15
FRAME_ATTEMPTS = 2
FRAME_RETRY_PAUSE_S = 1
# Cards are about 200px wide. go2rtc already turns an H264 frame into JPEG;
# width=320 keeps that encode small. cache=1m reuses a snapshot it just made.
FRAME_WIDTH = 320
FRAME_CACHE = "1m"
FRAME_MAX_BYTES = 2_000_000
# One frame at a time. Several ffmpeg jobs at once are what drop previews on a Pi.
_FRAME_LOCK = threading.Lock()
_PLACEHOLDER_SVG = b"""<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180" viewBox="0 0 320 180" role="img" aria-label="No preview">
  <rect width="320" height="180" fill="#e2e8f0"/>
  <g fill="none" stroke="#64748b" stroke-width="2">
    <rect x="128" y="58" width="64" height="44" rx="6"/>
    <circle cx="160" cy="80" r="10"/>
  </g>
  <text x="160" y="128" text-anchor="middle" font-family="Segoe UI, sans-serif" font-size="14" fill="#64748b">No preview</text>
</svg>
"""
# go2rtc stream names are short tokens. Reject anything else before it is
# placed in a link.
_STREAM_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_IPV4 = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")
_HOSTNAME = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)

app = FastAPI(title="H500 dashboard")
app.mount("/images", StaticFiles(directory=str(ROOT / "images")), name="images")
# Wall clock when this process loaded the app. The Status page reports it.
APP_STARTED_AT = time.time()

# stderr, not the uvicorn logger tree, so INFO lines show up under systemd too.
_preview_log = logging.getLogger("tapo.preview")
_preview_log.setLevel(logging.INFO)
if not _preview_log.handlers:
    _preview_handler = logging.StreamHandler(sys.stderr)
    _preview_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    _preview_log.addHandler(_preview_handler)
_preview_log.propagate = False


def _indent_width(line: str) -> int:
    width = 0
    for char in line:
        if char == " ":
            width += 1
        elif char == "\t":
            width += 2
        else:
            break
    return width


class Go2rtcConfigError(Exception):
    """bin/go2rtc.yaml could not be read or written. The message has no secrets."""


_DEVICE_ID = re.compile(r"^[A-Fa-f0-9]{8,64}$")
_DEVICE_ID_IN_URL = re.compile(r"deviceid=([A-Fa-f0-9]{8,64})", re.IGNORECASE)


def iter_stream_entries(lines: list[str]):
    """Yield ``(name, value)`` for each stream. Callers must not show ``value``."""
    in_streams = False
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = _indent_width(line)
        if indent == 0:
            in_streams = stripped == "streams:"
            continue
        if not in_streams or indent != 2 or ":" not in stripped:
            continue
        name, value = stripped.split(":", 1)
        name = name.strip().strip("'\"")
        if _STREAM_NAME.fullmatch(name):
            yield name, value.strip()


def stream_device_ids(path: Path) -> list[tuple[str, str | None]]:
    """``(stream name, device id)`` for each stream. URL values are not returned.

    The device id is the ``deviceId`` query on that stream's URL. A stream
    with no id still keeps its name so Live View can link to it.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows = []
    for name, value in iter_stream_entries(lines):
        found = _DEVICE_ID_IN_URL.findall(value)
        rows.append((name, found[0] if found else None))
    return rows


def stream_names(path: Path) -> list[str]:
    """Return go2rtc stream names. Stream URL values are never returned."""
    return [name for name, _device_id in stream_device_ids(path)]


def _read_go2rtc_text(path: Path) -> str | None:
    """Return the yaml text, or None when the file does not exist yet."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as err:
        reason = err.strerror or "the file could not be opened"
        raise Go2rtcConfigError(
            f"Could not read bin/go2rtc.yaml: {reason}. Nothing was changed."
        ) from err


def _streams_insert_at(lines: list[str]) -> int | None:
    """Line index where new streams belong, or None when there is no streams key."""
    start = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _indent_width(line) == 0 and stripped == "streams:":
            start = index
            break
    if start is None:
        return None
    insert = start + 1
    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        if not stripped or stripped.startswith("#"):
            insert = index + 1
            continue
        if _indent_width(lines[index]) == 0:
            break
        insert = index + 1
    return insert


def _stream_name_for(camera_name: str, used: set[str]) -> str:
    """A go2rtc name from a camera label. An existing name gets a numeric suffix."""
    words = re.sub(r"[^a-z0-9]+", "_", (camera_name or "").strip().lower()).strip("_")
    base = words[:64] if words else "camera"
    if not _STREAM_NAME.fullmatch(base):
        base = "camera"
    candidate = base
    number = 2
    while candidate in used or not _STREAM_NAME.fullmatch(candidate):
        suffix = f"_{number}"
        candidate = f"{base[: 64 - len(suffix)]}{suffix}"
        number += 1
        if number > 100:
            candidate = f"camera_{number}"
            break
    used.add(candidate)
    return candidate


def _password_sha256() -> str:
    """Uppercase SHA256 of TAPO_PASSWORD. The hash is not returned to the page."""
    password = os.getenv("TAPO_PASSWORD", "").strip()
    if not password:
        raise Go2rtcConfigError(
            "Set TAPO_PASSWORD to the cloud TP-Link account password. "
            "A new go2rtc stream uses the uppercase SHA256 of that password, "
            "and bin/go2rtc.yaml was not changed."
        )
    return hashlib.sha256(password.encode("utf-8")).hexdigest().upper()


def _english_list(names: list[str]) -> str:
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{', '.join(names[:-1])}, and {names[-1]}"


def sync_notice(synced: str | None) -> str:
    """Success text for /live. Only validated stream names are included."""
    if not synced:
        return ""
    if synced == "none":
        return "Every paired camera already has a stream in bin/go2rtc.yaml."
    names = [part.strip() for part in synced.split(",")]
    names = [name for name in names if _STREAM_NAME.fullmatch(name)]
    if not names:
        return ""
    added = _english_list(names)
    return (
        f"Added {added} to bin/go2rtc.yaml. "
        "Restart go2rtc for the changes to take effect: "
        "sudo systemctl restart go2rtc. "
        "On Windows, stop go2rtc.exe and start it again. "
        "The new tapo:// password is the uppercase SHA256 of TAPO_PASSWORD "
        "and is stored only in that file."
    )


def sync_go2rtc_file(path: Path, cameras) -> list[str]:
    """Append hub cameras whose device id is not already in the yaml.

    Existing lines are kept. The returned names are safe to show. The written
    URL is not.
    """
    current = _read_go2rtc_text(path)
    lines = [] if current is None else current.splitlines()
    present = {
        device_id.lower()
        for _name, value in iter_stream_entries(lines)
        for device_id in _DEVICE_ID_IN_URL.findall(value)
    }
    used = {name for name, _value in iter_stream_entries(lines)}
    additions = []
    for camera in cameras:
        device_id = str(getattr(camera, "device_id", "") or "").strip()
        if not _DEVICE_ID.fullmatch(device_id) or device_id.lower() in present:
            continue
        name = _stream_name_for(getattr(camera, "name", ""), used)
        additions.append((name, device_id))
        present.add(device_id.lower())
    if not additions:
        return []

    password_hash = _password_sha256()
    try:
        hub_ip = poc.hub_ip()
    except poc.HubAddressError as err:
        raise Go2rtcConfigError(str(err)) from err
    new_lines = [
        (
            f"  {name}: tapo://admin:{password_hash}@{hub_ip}/?deviceId={device_id}"
        )
        for name, device_id in additions
    ]
    insert_at = _streams_insert_at(lines)
    if insert_at is None:
        if lines and lines[-1] != "":
            lines.append("")
        lines.append("streams:")
        lines.extend(new_lines)
    else:
        lines[insert_at:insert_at] = new_lines
    ending = "\r\n" if current and "\r\n" in current else "\n"
    text = ending.join(lines)
    if text and not text.endswith(("\n", "\r\n")):
        text += ending

    temporary = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(text, encoding="utf-8", newline="")
        temporary.replace(path)
    except OSError as err:
        reason = err.strerror or "the file could not be saved"
        raise Go2rtcConfigError(
            f"Could not write bin/go2rtc.yaml: {reason}. "
            "Existing streams were left as they were."
        ) from err
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass
    return [name for name, _device_id in additions]


def stream_label(name: str) -> str:
    """Readable label for a go2rtc stream name such as ``front_door``."""
    words = name.replace("_", " ").replace("-", " ").split()
    return " ".join(word.capitalize() for word in words) or name


def go2rtc_origin(hostname: str | None) -> str:
    """go2rtc Web UI on the same host the dashboard was opened from."""
    host = (hostname or "").strip().rstrip(".")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if _IPV4.fullmatch(host):
        if any(int(part) > 255 for part in host.split(".")):
            host = "127.0.0.1"
    elif not _HOSTNAME.fullmatch(host):
        host = "127.0.0.1"
    return f"http://{host}:{GO2RTC_PORT}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse a redirect so a frame request stays on this host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


_FRAME_OPENER = urllib.request.build_opener(_NoRedirect)


def stream_name_for_device(path: Path, device_id: str) -> str | None:
    """go2rtc stream whose URL contains this device id. The URL is not returned."""
    if not _DEVICE_ID.fullmatch(device_id or ""):
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    wanted = device_id.lower()
    for name, value in iter_stream_entries(lines):
        if any(found.lower() == wanted for found in _DEVICE_ID_IN_URL.findall(value)):
            return name
    return None


def _frame_url(stream_name: str) -> str:
    """Small cached JPEG. The stream name is the only value taken from the caller."""
    src = quote(stream_name, safe="")
    return (
        f"http://127.0.0.1:{GO2RTC_PORT}/api/frame.jpeg"
        f"?src={src}&width={FRAME_WIDTH}&cache={quote(FRAME_CACHE, safe='')}"
    )


def _exception_detail(err: BaseException) -> str:
    """Exception type, message, cause, and a short HTTP body. No stream URL."""
    text = f"{type(err).__name__}: {err}"
    cause = err.__cause__ or err.__context__
    if isinstance(cause, BaseException):
        text = f"{text}; cause {type(cause).__name__}: {cause}"
    if isinstance(err, urllib.error.HTTPError):
        try:
            raw = err.read(800)
        except Exception:
            raw = b""
        snippet = raw.decode("utf-8", errors="replace").replace("\n", " ").strip()
        if "tapo://" in snippet.lower():
            snippet = "tapo://(redacted)"
        if snippet:
            text = f"{text}; body: {snippet[:500]}"
    return text[:1000]


def _read_frame(stream_name: str) -> tuple[bytes | None, str | None]:
    """One attempt. ``(jpeg, None)`` or ``(None, reason)``."""
    request = urllib.request.Request(_frame_url(stream_name), headers={"Accept": "image/jpeg"})
    try:
        with _FRAME_OPENER.open(request, timeout=FRAME_TIMEOUT_S) as response:
            content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if content_type and content_type not in {"image/jpeg", "image/jpg"}:
                return None, f"content type {content_type}"
            data = response.read(FRAME_MAX_BYTES + 1)
    except Exception as err:
        return None, _exception_detail(err)
    if len(data) < 3 or len(data) > FRAME_MAX_BYTES or not data.startswith(b"\xff\xd8"):
        return None, f"not a jpeg ({len(data)} bytes)"
    return data, None


def fetch_go2rtc_jpeg(stream_name: str) -> bytes | None:
    """One JPEG from local go2rtc, or None when both attempts fail.

    ``CameraHubHandler`` has no snapshot for a child camera. go2rtc already
    holds the stream URL, so this asks it for ``/api/frame.jpeg``. The page
    that shows the camera list does not call this. A failure is logged and
    the caller still receives None so the card can show the placeholder.
    """
    if not _STREAM_NAME.fullmatch(stream_name or ""):
        _preview_log.warning("snapshot rejected: stream name %r is not usable", stream_name)
        return None
    last = "no frame"
    with _FRAME_LOCK:
        for attempt in range(1, FRAME_ATTEMPTS + 1):
            frame, detail = _read_frame(stream_name)
            if frame is not None:
                _preview_log.info("snapshot %s: %s bytes", stream_name, len(frame))
                return frame
            last = detail or "no frame"
            _preview_log.warning(
                "snapshot %s attempt %s/%s failed: %s",
                stream_name,
                attempt,
                FRAME_ATTEMPTS,
                last,
            )
            if attempt < FRAME_ATTEMPTS:
                time.sleep(FRAME_RETRY_PAUSE_S)
    _preview_log.error("snapshot %s failed after %s attempts: %s", stream_name, FRAME_ATTEMPTS, last)
    return None


def _thumbnail_bytes(device_id: str) -> bytes | None:
    name = stream_name_for_device(GO2RTC_CONFIG, device_id)
    if not name:
        _preview_log.warning("snapshot skipped: no go2rtc stream for device %s", device_id)
        return None
    return fetch_go2rtc_jpeg(name)


def _placeholder_response() -> Response:
    return Response(
        content=_PLACEHOLDER_SVG,
        media_type="image/svg+xml",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _frame_response(frame: bytes | None) -> Response:
    """JPEG when a frame was read, otherwise the placeholder image."""
    if not frame:
        return _placeholder_response()
    return Response(
        content=frame,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "private, max-age=60",
            "X-Content-Type-Options": "nosniff",
        },
    )


def live_streams(hostname: str | None, cameras=()) -> tuple[str, list[dict[str, str]]]:
    """Full go2rtc UI URL, plus one player link per configured stream.

    ``cameras`` supplies battery labels matched by device id. The stream URL
    values stay in the yaml. A stream with no matching camera uses an em dash.
    """
    by_id = {}
    for camera in cameras or []:
        device_id = str(getattr(camera, "device_id", "") or "").strip()
        if device_id:
            by_id[device_id.lower()] = camera
    origin = go2rtc_origin(hostname)
    streams = []
    for name, device_id in stream_device_ids(GO2RTC_CONFIG):
        camera = by_id.get(device_id.lower()) if device_id else None
        battery_label = camera.battery_label if camera is not None else "—"
        streams.append(
            {
                "name": name,
                "label": stream_label(name),
                "url": f"{origin}/stream.html?src={quote(name, safe='')}",
                "battery_label": battery_label,
                "thumbnail": f"/live/thumbnail/{quote(name, safe='')}",
            }
        )
    return f"{origin}/", streams


def multi_view_streams(hostname: str | None) -> list[dict[str, str]]:
    """One go2rtc player per configured stream. URL values stay in the yaml.

    Each player is go2rtc's own ``stream.html`` page, which selects WebRTC
    and then MSE. A camera that is offline fails inside that frame.
    """
    origin = go2rtc_origin(hostname)
    streams = []
    for name, _device_id in stream_device_ids(GO2RTC_CONFIG):
        src = quote(name, safe="")
        streams.append(
            {
                "name": name,
                "label": stream_label(name),
                "player": f"{origin}/stream.html?src={src}&mode=webrtc,mse",
            }
        )
    return streams


def render(request: Request, name: str, context: dict, status_code: int = 200) -> HTMLResponse:
    payload = {"request": request, **context}
    return templates.TemplateResponse(request, name, payload, status_code=status_code)


@app.exception_handler(HubError)
async def show_hub_error(request: Request, exc: HubError) -> HTMLResponse:
    if exc.status_code == 404:
        heading = "Device not found"
    elif exc.status_code >= 500:
        heading = "Could not reach the hub"
    else:
        heading = "That request could not be completed"
    return render(
        request,
        "error.html",
        {"title": heading, "heading": heading, "message": str(exc)},
        status_code=exc.status_code,
    )


async def _cameras_or_empty():
    """Hub cameras for battery labels. A hub failure leaves those labels blank."""
    try:
        cameras, _source = await session.list_cameras()
    except HubError:
        return []
    return cameras


@app.get("/live", response_class=HTMLResponse)
async def live_view(request: Request) -> HTMLResponse:
    cameras = await _cameras_or_empty()
    go2rtc_url, streams = live_streams(request.url.hostname, cameras)
    return render(
        request,
        "live.html",
        {
            "title": "Live view",
            "streams": streams,
            "go2rtc_url": go2rtc_url,
            "notice": sync_notice(request.query_params.get("synced")),
            "error": "",
        },
    )


@app.post("/live/sync", response_class=HTMLResponse)
async def sync_live_cameras(request: Request) -> HTMLResponse:
    """Add H500 cameras that are missing from bin/go2rtc.yaml."""
    try:
        cameras, _source = await session.list_cameras()
        added = sync_go2rtc_file(GO2RTC_CONFIG, cameras)
    except Go2rtcConfigError as err:
        go2rtc_url, streams = live_streams(request.url.hostname, cameras)
        return render(
            request,
            "live.html",
            {
                "title": "Live view",
                "streams": streams,
                "go2rtc_url": go2rtc_url,
                "notice": "",
                "error": str(err),
            },
            status_code=400,
        )
    synced = "none" if not added else ",".join(added)
    return RedirectResponse(f"/live?synced={quote(synced, safe=',')}", status_code=303)


@app.get("/live/multi", response_class=HTMLResponse)
async def live_multi(request: Request) -> HTMLResponse:
    """Tiled live players. This does not call the hub or request previews."""
    return render(
        request,
        "multiview.html",
        {
            "title": "Multi-view",
            "streams": multi_view_streams(request.url.hostname),
        },
    )


@app.get("/live/thumbnail/{stream_name}")
async def live_thumbnail(stream_name: str) -> Response:
    """One frame for a Live View card. Failure is a placeholder, not an error page.

    The name has to be a stream already listed in bin/go2rtc.yaml. This does
    not call the hub and does not read the stream URL into the response.
    """
    if stream_name not in set(stream_names(GO2RTC_CONFIG)):
        _preview_log.warning("snapshot skipped: stream %s is not configured", stream_name)
        return _placeholder_response()
    try:
        frame = await asyncio.to_thread(fetch_go2rtc_jpeg, stream_name)
    except Exception as err:
        _preview_log.error("snapshot %s failed: %s", stream_name, _exception_detail(err))
        frame = None
    return _frame_response(frame)


@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    cameras, source = await session.list_cameras()
    return render(
        request,
        "index.html",
        {"title": "Cameras", "devices": cameras, "source": source},
    )


@app.get("/devices/{device_id}/thumbnail")
async def camera_thumbnail(device_id: str) -> Response:
    """One frame for a camera card. Failure is a placeholder image, not an error page.

    This does not call the hub. The cameras page renders the list first and
    loads this URL from an image tag.
    """
    try:
        frame = await asyncio.to_thread(_thumbnail_bytes, device_id)
    except Exception as err:
        _preview_log.error("snapshot %s failed: %s", device_id, _exception_detail(err))
        frame = None
    return _frame_response(frame)


@app.get("/devices/{device_id}", response_class=HTMLResponse)
async def device_page(
    request: Request,
    device_id: str,
    limit: int = 20,
    day: str | None = None,
) -> HTMLResponse:
    recordings = await session.recordings(device_id, limit, day)
    return render(
        request,
        "device.html",
        {
            "title": recordings.device.name,
            "recordings": recordings,
            "lookback_days": LOOKBACK_DAYS,
            "limit": min(max(limit, 1), 50),
        },
    )


@app.post("/devices/{device_id}/download")
async def download_clip(
    device_id: str,
    start: str = Form(...),
    end: str = Form(...),
) -> FileResponse:
    start_time = parse_timestamp(start)
    end_time = parse_timestamp(end)
    if end_time <= start_time:
        raise BadRequest("The clip end time must be after the start time.")

    path = await session.download_clip(device_id, start_time, end_time)
    stamp = start_time.strftime("%Y%m%dT%H%M%SZ")
    filename = f"clip-{device_id[:12]}-{stamp}.ts"
    return FileResponse(
        path,
        media_type="video/mp2t",
        filename=filename,
        background=BackgroundTask(remove_download, path),
    )


@app.get("/devices/{device_id}/download-day/progress")
async def download_day_progress(device_id: str, day: str) -> dict:
    return session.day_download_progress(device_id, day)


@app.get("/about", response_class=HTMLResponse)
async def about_page(request: Request) -> HTMLResponse:
    """Project description. This does not call the hub or go2rtc."""
    return render(request, "about.html", {"title": "About"})


@app.get("/status", response_class=HTMLResponse)
async def status_page(request: Request) -> HTMLResponse:
    """Local health checks. This does not list cameras or open a hub login."""
    checks = await build_status_checks(APP_STARTED_AT)
    return render(request, "status.html", {"title": "Status", "checks": checks})


def _controls_page(listing, error: str = "", error_ip: str = "", notice: str = "") -> dict:
    return {
        "title": "Devices",
        "devices": device_cards(listing.devices),
        "source": listing.source,
        "error": error or listing.error,
        "error_ip": error_ip,
        "notice": notice,
    }


@app.get("/controls", response_class=HTMLResponse)
async def controls_page(request: Request, refresh: bool = False) -> HTMLResponse:
    listing = await control_session.list_devices(refresh=refresh)
    notice = "Updated." if request.query_params.get("updated") == "1" else ""
    return render(request, "controls.html", _controls_page(listing, notice=notice))


@app.post("/controls/power")
async def control_power(
    request: Request,
    ip: str = Form(...),
    action: str = Form(...),
    child_id: str = Form(""),
    brightness: str = Form(""),
    hue: str = Form(""),
    saturation: str = Form(""),
    color_temp: str = Form(""),
    preset: str = Form(""),
):
    try:
        await control_session.set_power(
            ip,
            child_id,
            action,
            brightness,
            hue,
            saturation,
            color_temp,
            preset,
        )
    except ControlError as err:
        listing = await control_session.list_devices()
        return render(
            request,
            "controls.html",
            _controls_page(listing, error=str(err), error_ip=ip.strip()),
            status_code=400,
        )
    return RedirectResponse("/controls?updated=1", status_code=303)


@app.post("/devices/{device_id}/download-day")
async def download_day(device_id: str, day: str = Form(...)) -> FileResponse:
    path = await session.download_day(device_id, day)
    return FileResponse(
        path,
        media_type="application/zip",
        filename=clip_archive_name(device_id, day),
        background=BackgroundTask(remove_download, path),
    )
