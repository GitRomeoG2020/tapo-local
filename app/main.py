"""Local FastAPI dashboard for cameras and recordings on the Tapo H500.

Run from the project root:

    python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

Routes:
    GET  /                                 paired cameras and doorbells
    GET  /live                             links to go2rtc live streams
    GET  /devices/{device_id}              recording days and one day's clips
    POST /devices/{device_id}/download     one clip as MPEG-TS
    POST /devices/{device_id}/download-day every clip from one day as a zip
    GET  /devices/{device_id}/download-day/progress
                                           clip counter for that zip

Every page shares the top navigation. Live View reads stream names from
``go2rtc.yaml`` and links each one to the go2rtc player on port 1984 of the
same host. Stream URL values stay in that file. Plugs and lights can add
routes beside these. They should use ``app.hub.session`` so they share the
hub login.
"""

import re
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask

from app.hub import (
    BadRequest,
    HubError,
    clip_archive_name,
    parse_timestamp,
    remove_download,
    session,
)
from scripts.h500_poc import LOOKBACK_DAYS

ROOT = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(ROOT / "templates"))

GO2RTC_PORT = 1984
GO2RTC_CONFIG = ROOT / "go2rtc.yaml"
# go2rtc stream names are short tokens. Reject anything else before it is
# placed in a link.
_STREAM_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_IPV4 = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")
_HOSTNAME = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)

app = FastAPI(title="H500 dashboard")


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


def stream_names(path: Path) -> list[str]:
    """Return go2rtc stream names. Stream URL values are never returned."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    names: list[str] = []
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
        name = stripped.split(":", 1)[0].strip().strip("'\"")
        if _STREAM_NAME.fullmatch(name):
            names.append(name)
    return names


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


def live_streams(hostname: str | None) -> tuple[str, list[dict[str, str]]]:
    """Full go2rtc UI URL, plus one player link per configured stream."""
    origin = go2rtc_origin(hostname)
    streams = [
        {
            "name": name,
            "label": stream_label(name),
            "url": f"{origin}/stream.html?src={quote(name, safe='')}",
        }
        for name in stream_names(GO2RTC_CONFIG)
    ]
    return f"{origin}/", streams


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


@app.get("/live", response_class=HTMLResponse)
async def live_view(request: Request) -> HTMLResponse:
    go2rtc_url, streams = live_streams(request.url.hostname)
    return render(
        request,
        "live.html",
        {"title": "Live view", "streams": streams, "go2rtc_url": go2rtc_url},
    )


@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    cameras, source = await session.list_cameras()
    return render(
        request,
        "index.html",
        {"title": "Cameras", "devices": cameras, "source": source},
    )


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


@app.post("/devices/{device_id}/download-day")
async def download_day(device_id: str, day: str = Form(...)) -> FileResponse:
    path = await session.download_day(device_id, day)
    return FileResponse(
        path,
        media_type="application/zip",
        filename=clip_archive_name(device_id, day),
        background=BackgroundTask(remove_download, path),
    )
