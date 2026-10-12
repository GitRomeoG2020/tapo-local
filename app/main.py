"""Local FastAPI dashboard for cameras and recordings on the Tapo H500.

Run from the project root:

    python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

Routes:
    GET  /                                 paired cameras and doorbells
    GET  /devices/{device_id}              recording days and one day's clips
    POST /devices/{device_id}/download     one clip as MPEG-TS
    POST /devices/{device_id}/download-day every clip from one day as a zip
    GET  /devices/{device_id}/download-day/progress
                                           clip counter for that zip

Plugs, lights, and live view can add routes beside these. They should use
``app.hub.session`` so they share the hub login.
"""

from pathlib import Path

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

app = FastAPI(title="H500 dashboard")


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
