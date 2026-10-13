"""Fast checks for the Status page.

The page does not log in to the hub, list cameras, or request previews.
Hub and go2rtc checks are short TCP connections. The camera count is whatever
the last successful hub list already reported.
"""

import asyncio
import shutil
import time
from dataclasses import dataclass

import scripts.h500_poc as poc

from app.controls import EXTRA_DEVICES_FILE, ControlError, extra_devices_from
from app.hub import session

GO2RTC_HOST = "127.0.0.1"
GO2RTC_PORT = 1984
HUB_PORTS = (443, 80)
PROBE_TIMEOUT_S = 1.0


@dataclass(frozen=True)
class StatusCheck:
    """One row on the Status page. ``state`` is ok, bad, or off."""

    name: str
    value: str
    detail: str
    state: str


def format_uptime(seconds: float) -> str:
    """A short duration. Days hide the leftover seconds."""
    total = max(0, int(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append("1 day" if days == 1 else f"{days} days")
    if hours:
        parts.append("1 hour" if hours == 1 else f"{hours} hours")
    if minutes and days == 0:
        parts.append("1 minute" if minutes == 1 else f"{minutes} minutes")
    if parts:
        return " ".join(parts)
    return "1 second" if secs == 1 else f"{secs} seconds"


def ffmpeg_on_path() -> bool:
    """True when ``ffmpeg`` can be started by name."""
    return shutil.which("ffmpeg") is not None


def read_extra_count() -> tuple[int | None, str]:
    """How many extra devices are listed. An unreadable file returns an error."""
    try:
        devices = extra_devices_from(EXTRA_DEVICES_FILE)
    except ControlError as err:
        return None, str(err)
    return len(devices), ""


async def tcp_accepts(host: str, port: int, timeout: float = PROBE_TIMEOUT_S) -> bool:
    """True when a TCP connection to ``host`` and ``port`` is accepted."""
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
    except Exception:
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    return True


async def hub_accepts(address: str) -> bool:
    """True when the hub address accepts a connection. This does not log in."""
    results = await asyncio.gather(*(tcp_accepts(address, port) for port in HUB_PORTS))
    return any(results)


def checks_from(
    *,
    hub_address: str,
    hub_error: str,
    hub_open: bool,
    go2rtc_open: bool,
    ffmpeg_found: bool,
    camera_count: int | None,
    extra_count: int | None,
    extra_error: str,
    started_at: float,
    now: float,
) -> list[StatusCheck]:
    """Build the Status rows from checks that have already finished."""
    if hub_error:
        hub = StatusCheck("H500 hub", "Not set", hub_error, "bad")
    elif hub_open:
        hub = StatusCheck(
            "H500 hub",
            "Reachable",
            f"The H500 at {hub_address} accepted a connection.",
            "ok",
        )
    else:
        hub = StatusCheck(
            "H500 hub",
            "Not reachable",
            f"H500 not responding at {hub_address}.",
            "bad",
        )

    if go2rtc_open:
        go2rtc = StatusCheck(
            "go2rtc",
            "Reachable",
            "go2rtc accepted a connection on port 1984.",
            "ok",
        )
    else:
        go2rtc = StatusCheck(
            "go2rtc",
            "Not responding",
            "go2rtc not responding on port 1984.",
            "bad",
        )

    if ffmpeg_found:
        ffmpeg = StatusCheck("ffmpeg", "Installed", "ffmpeg is on PATH.", "ok")
    else:
        ffmpeg = StatusCheck(
            "ffmpeg",
            "Missing",
            "ffmpeg is not installed or not on PATH. Camera previews need it.",
            "bad",
        )

    if camera_count is None:
        cameras = StatusCheck(
            "Cameras",
            "Not loaded",
            "Open Cameras & Recordings to load the hub list. This page does not query cameras.",
            "off",
        )
    elif camera_count == 1:
        cameras = StatusCheck(
            "Cameras",
            "1",
            "The last hub camera list reported 1 camera.",
            "ok",
        )
    else:
        cameras = StatusCheck(
            "Cameras",
            str(camera_count),
            f"The last hub camera list reported {camera_count} cameras.",
            "ok",
        )

    if extra_error:
        extras = StatusCheck("Extra devices", "Invalid", extra_error, "bad")
    elif extra_count:
        label = "device" if extra_count == 1 else "devices"
        extras = StatusCheck(
            "Extra devices",
            str(extra_count),
            f"{extra_count} {label} listed in extra_devices.yaml.",
            "ok",
        )
    else:
        extras = StatusCheck(
            "Extra devices",
            "none",
            "No extra devices are listed in extra_devices.yaml.",
            "ok",
        )

    started = time.strftime("%Y-%m-%d %H:%M", time.localtime(started_at))
    uptime = StatusCheck(
        "Dashboard uptime",
        format_uptime(now - started_at),
        f"This process started at {started}.",
        "ok",
    )
    return [hub, go2rtc, ffmpeg, cameras, extras, uptime]


async def build_status_checks(started_at: float, now: float | None = None) -> list[StatusCheck]:
    """Run the fast checks and return the Status rows."""
    clock = time.time() if now is None else now
    try:
        address = poc.hub_ip()
        hub_error = ""
    except poc.HubAddressError as err:
        address = ""
        hub_error = str(err)

    if address:
        go2rtc_open, hub_open = await asyncio.gather(
            tcp_accepts(GO2RTC_HOST, GO2RTC_PORT),
            hub_accepts(address),
        )
    else:
        go2rtc_open = await tcp_accepts(GO2RTC_HOST, GO2RTC_PORT)
        hub_open = False

    extra_count, extra_error = read_extra_count()
    return checks_from(
        hub_address=address,
        hub_error=hub_error,
        hub_open=hub_open,
        go2rtc_open=go2rtc_open,
        ffmpeg_found=ffmpeg_on_path(),
        camera_count=session.camera_count,
        extra_count=extra_count,
        extra_error=extra_error,
        started_at=started_at,
        now=clock,
    )
