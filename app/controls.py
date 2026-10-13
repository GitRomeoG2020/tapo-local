"""Plugs, lights, and other non-camera Tapo devices.

Addresses come from ``devices.txt`` in the project root. One IPv4 address per
line. An address ending in ``.255`` scans that subnet. When the file is
missing or has no addresses, the page scans the subnet of ``TAPO_HUB_IP``. Adding
an address does not require a code change.

``extra_devices.yaml`` is optional. Each entry is an additional device,
including one on another subnet or VLAN. Those addresses are logged in to
directly because a subnet scan does not cross VLANs. A missing file leaves
the scan unchanged.

Discovery picks the handler, so a new plug or light model works when the
installed ``tapo`` package already recognizes it. A blank ``model`` uses a
model token in the device name, then one unicast discovery, then a light
login that is upgraded when the device reports a different model. A bulb
with ``set_brightness`` gets a 0–100 slider. Color, hue, and color
temperature are shown only when that handler has the matching method. A
power strip's outlets come from ``get_child_device_list``, or from the JSON
list when that typed list is empty. Anything else keeps on/off. Cameras and
the H500 are skipped. The account is ``TAPO_USERNAME`` and ``TAPO_PASSWORD``.
"""

import asyncio
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

import scripts.h500_poc as poc

ROOT = Path(__file__).resolve().parents[1]
DEVICES_FILE = ROOT / "devices.txt"
EXTRA_DEVICES_FILE = ROOT / "extra_devices.yaml"

# discover_devices waits up to this long for replies. A cold page load pays
# that wait once per address, then reuses the result briefly.
DISCOVERY_TIMEOUT_S = 5
# A listed device on another VLAN often will not answer discovery. Keep that
# wait short, then log in to the address directly.
EXTRA_DISCOVERY_TIMEOUT_S = 2
DIRECT_TIMEOUT_S = 8
LIST_CACHE_S = 30

# stderr, not the uvicorn logger tree, so a missing outlet list is visible
# under systemd as well as a local console.
_controls_log = logging.getLogger("tapo.controls")
_controls_log.setLevel(logging.INFO)
if not _controls_log.handlers:
    _log_handler = logging.StreamHandler(sys.stderr)
    _log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    _controls_log.addHandler(_log_handler)
_controls_log.propagate = False

# One login per device. The method is the tapo client helper for that model.
# A blank model is not a light: the name, discovery, or the device's own
# model string picks the handler. Longest name wins, so P316M is not P316.
_CLIENT_METHODS = {
    "L510": "l510",
    "L520": "l520",
    "L530": "l530",
    "L535": "l535",
    "L610": "l610",
    "L630": "l630",
    "L900": "l900",
    "L920": "l920",
    "L930": "l930",
    "P100": "p100",
    "P105": "p105",
    "P110": "p110",
    "P110M": "p110",
    "P115": "p115",
    "P300": "p300",
    "P304": "p304",
    "P304M": "p304",
    "P306": "p306",
    "P316": "p316",
    "P316M": "p316",
}
_STRIP_MODELS = {"P300", "P304", "P304M", "P306", "P316", "P316M"}
_COLOR_MODELS = {"L530", "L535", "L630", "L900", "L920", "L930"}
# Preset names are tapo.Color attributes. Labels are the control text.
COLOR_PRESETS = (
    ("WarmWhite", "Warm white"),
    ("CoolWhite", "Cool white"),
    ("Daylight", "Daylight"),
    ("Candlelight", "Candlelight"),
    ("Gold", "Gold"),
    ("Tomato", "Red"),
    ("Lime", "Green"),
    ("DeepSkyBlue", "Blue"),
)

_IPV4 = re.compile(r"(?:\d{1,3}\.){3}\d{1,3}")
_CHILD_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_CAMERA_MODEL = re.compile(r"^(C\d|TC\d|D\d)", re.IGNORECASE)
_AUTH_MESSAGE = (
    "Login was rejected. Check TAPO_USERNAME and TAPO_PASSWORD. "
    "The device was not tried again."
)


class ControlError(Exception):
    """A device lookup or control failure the page can show."""


@dataclass
class ControlDevice:
    """One plug, light, outlet, or other non-camera device."""

    name: str
    model: str
    ip: str
    online: bool
    power: str | None
    brightness: int | None
    child_id: str
    can_switch: bool
    can_dim: bool
    hue: int | None = None
    saturation: int | None = None
    color_temp: int | None = None
    can_color: bool = False
    can_temperature: bool = False
    can_preset: bool = False
    note: str = ""

    @property
    def status(self) -> str:
        if not self.online:
            return "Offline"
        if self.power == "on":
            return "Online · On"
        if self.power == "off":
            return "Online · Off"
        return "Online"

    @property
    def brightness_input(self) -> int:
        if self.brightness is None:
            return 100
        return min(max(self.brightness, 0), 100)

    @property
    def hue_input(self) -> int:
        if self.hue is None:
            return 0
        return min(max(self.hue, 0), 360)

    @property
    def saturation_input(self) -> int:
        if self.saturation is None:
            return 100
        return min(max(self.saturation, 1), 100)

    @property
    def color_temp_input(self) -> int:
        if self.color_temp is None:
            return 4000
        return min(max(self.color_temp, 2500), 6500)


@dataclass(frozen=True)
class ExtraDevice:
    """One address from ``extra_devices.yaml``."""

    ip: str
    name: str
    model: str = ""


@dataclass
class ControlList:
    """The Devices page: rows, where they came from, and a page-level error."""

    devices: list[ControlDevice]
    source: str
    error: str


def normalize_ipv4(value: str) -> str | None:
    """Return a canonical IPv4 address, or None when ``value`` is not one."""
    text = (value or "").strip()
    if not _IPV4.fullmatch(text):
        return None
    numbers = []
    for part in text.split("."):
        number = int(part)
        if number > 255:
            return None
        numbers.append(str(number))
    return ".".join(numbers)


def is_broadcast(ip: str) -> bool:
    """True for a /24 broadcast address. Those lines scan a subnet."""
    return ip == "255.255.255.255" or ip.endswith(".255")


def subnet_broadcast(ip: str) -> str:
    """``192.168.1.10`` becomes ``192.168.1.255``."""
    address = normalize_ipv4(ip)
    if address is None:
        return "255.255.255.255"
    return ".".join(address.split(".")[:3] + ["255"])


def load_addresses(text: str) -> list[str]:
    """Address lines from ``devices.txt``. Blank lines and comments are ignored."""
    addresses = []
    for line_number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        address = normalize_ipv4(line)
        if address is None:
            shown = line if len(line) <= 40 else line[:40]
            raise ControlError(
                f"devices.txt line {line_number} is not an IPv4 address: {shown}"
            )
        if address not in addresses:
            addresses.append(address)
    return addresses


def targets_for(path: Path, fallback: str) -> tuple[list[str], str]:
    """Addresses to check, and the short source line for the page."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return [fallback], f"Scanned {fallback}."
    addresses = load_addresses(text)
    if not addresses:
        return [fallback], f"Scanned {fallback}."
    return addresses, "Checked devices.txt."


def client_method(model: str) -> str:
    """tapo ``ApiClient`` method for a config model. Blank means not chosen yet."""
    text = (model or "").strip().upper()
    if not text:
        return ""
    method = _CLIENT_METHODS.get(text)
    if method is None:
        raise ControlError(
            f"extra_devices.yaml model {text} is not a supported Tapo model."
        )
    return method


def model_in_text(text: str) -> str:
    """Longest known model token in a device name, or an empty string."""
    padded = f" {re.sub(r'[^A-Z0-9]+', ' ', (text or '').upper())} "
    for model in sorted(_CLIENT_METHODS, key=len, reverse=True):
        if f" {model} " in padded:
            return model
    return ""


def method_for_reported_model(model: str) -> str:
    """Client method for a model string read from the device. Unknown stays blank."""
    text = (model or "").strip().upper()
    if not text:
        return ""
    return _CLIENT_METHODS.get(text) or client_method(model_in_text(text))


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _yaml_pair(text: str, line_number: int) -> tuple[str, str]:
    if ":" not in text:
        raise ControlError(f"extra_devices.yaml line {line_number} is not valid.")
    key, value = text.split(":", 1)
    key = key.strip().lower()
    value = value.strip()
    if not key or not value:
        raise ControlError(f"extra_devices.yaml line {line_number} is not valid.")
    return key, _unquote(value)


def _clean_name(value: str, fallback: str) -> str:
    text = " ".join((value or "").split())
    return text[:80] if text else fallback


def load_extra_devices(text: str) -> list[ExtraDevice]:
    """Parse ``extra_devices.yaml``. Comments and a missing list yield no devices."""
    devices: list[ExtraDevice] = []
    seen: set[str] = set()
    in_list = False
    saw_key = False
    current: dict[str, str] = {}

    def finish() -> None:
        if not current:
            return
        ip = current.get("ip", "")
        if not ip:
            raise ControlError("extra_devices.yaml has a device without an ip.")
        if ip in seen:
            raise ControlError(f"extra_devices.yaml lists {ip} more than once.")
        seen.add(ip)
        devices.append(
            ExtraDevice(ip, _clean_name(current.get("name", ""), ip), current.get("model", ""))
        )
        current.clear()

    for line_number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" \t"))
        if indent == 0 and not (in_list and stripped.startswith("- ")):
            finish()
            if stripped in {"extra_devices:", "extra_devices: []"}:
                if saw_key:
                    raise ControlError("extra_devices.yaml repeats extra_devices.")
                saw_key = True
                in_list = stripped == "extra_devices:"
                continue
            raise ControlError(f"extra_devices.yaml line {line_number} is not valid.")
        if not saw_key or not in_list:
            raise ControlError(f"extra_devices.yaml line {line_number} is not valid.")
        if stripped.startswith("- "):
            finish()
            body = stripped[2:].strip()
            if not body:
                continue
            key, value = _yaml_pair(body, line_number)
            _store_extra_field(current, key, value, line_number)
            continue
        if stripped.startswith("-"):
            raise ControlError(f"extra_devices.yaml line {line_number} is not valid.")
        key, value = _yaml_pair(stripped, line_number)
        _store_extra_field(current, key, value, line_number)
    finish()
    return devices


def _store_extra_field(current: dict[str, str], key: str, value: str, line_number: int) -> None:
    if key == "ip":
        address = normalize_ipv4(value)
        if address is None or is_broadcast(address):
            raise ControlError(
                f"extra_devices.yaml line {line_number} is not a device IPv4 address."
            )
        current["ip"] = address
        return
    if key == "name":
        current["name"] = value.strip()
        return
    if key == "model":
        current["model"] = value.strip()
        return
    raise ControlError(f"extra_devices.yaml line {line_number} has an unknown field.")


def extra_devices_from(path: Path) -> list[ExtraDevice]:
    """Devices listed in the config. A missing file means there are none."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return []
    return load_extra_devices(text)


def describe_source(base: str, extra_count: int) -> str:
    """Source line. No extra devices leaves the original sentence unchanged."""
    if extra_count <= 0:
        return base
    sentence = base[:-1] if base.endswith(".") else base
    label = "device" if extra_count == 1 else "devices"
    return f"{sentence} and {extra_count} {label} from extra_devices.yaml."


def prefer_name(device: ControlDevice, name: str) -> ControlDevice:
    """Use the config name, keeping a power-strip outlet name after it."""
    if not name or name == device.name:
        return device
    if device.child_id and " · " in device.name:
        outlet = device.name.split(" · ", 1)[1]
        return replace(device, name=f"{name} · {outlet}")
    if device.child_id:
        return replace(device, name=f"{name} · {device.name}")
    return replace(device, name=name)


def is_camera_device(device) -> bool:
    """Cameras and camera hubs stay on the recordings pages."""
    if "Camera" in type(device).__name__:
        return True
    model = _text(_read(device, "model"))
    if model and _CAMERA_MODEL.match(model):
        return True
    ip = normalize_ipv4(_text(_read(device, "ip")))
    if not ip:
        return False
    try:
        return ip == poc.hub_ip()
    except poc.HubAddressError:
        return False


@dataclass(frozen=True)
class ControlCommand:
    """One checked control form."""

    ip: str
    child_id: str
    action: str
    brightness: int | None = None
    hue: int | None = None
    saturation: int | None = None
    color_temp: int | None = None
    preset: str = ""


def validate_command(
    ip: str,
    child_id: str,
    action: str,
    brightness: str = "",
    hue: str = "",
    saturation: str = "",
    color_temp: str = "",
    preset: str = "",
) -> ControlCommand:
    """Check a control form before any device is contacted."""
    address = normalize_ipv4(ip)
    if address is None or is_broadcast(address):
        raise ControlError("That address cannot be controlled.")
    outlet = (child_id or "").strip()
    if outlet and _CHILD_ID.fullmatch(outlet) is None:
        raise ControlError("That outlet id is not valid.")
    command = (action or "").strip().lower()
    if command not in {"on", "off", "brightness", "color", "temperature", "preset"}:
        raise ControlError("That control is not supported.")
    if outlet and command not in {"on", "off"}:
        raise ControlError("This outlet only supports on and off.")
    level = hue_value = saturation_value = temp_value = None
    chosen = ""
    if command == "brightness":
        level = parse_brightness(brightness)
    elif command == "color":
        hue_value = _whole_number(hue, 0, 360, "Hue")
        saturation_value = _whole_number(saturation, 1, 100, "Saturation")
    elif command == "temperature":
        temp_value = _whole_number(color_temp, 2500, 6500, "Color temperature")
    elif command == "preset":
        chosen = parse_preset(preset)
    return ControlCommand(
        address,
        outlet,
        command,
        level,
        hue_value,
        saturation_value,
        temp_value,
        chosen,
    )


def _whole_number(value: str, low: int, high: int, label: str) -> int:
    text = (value or "").strip()
    if not re.fullmatch(r"\d{1,5}", text):
        raise ControlError(f"{label} must be a whole number from {low} to {high}.")
    number = int(text)
    if not low <= number <= high:
        raise ControlError(f"{label} must be a whole number from {low} to {high}.")
    return number


def parse_brightness(value: str) -> int:
    """Slider value from 0 to 100. Zero turns the device off.

    ``set_brightness`` itself accepts 1 to 100 and turns the device on.
    """
    return _whole_number(value, 0, 100, "Brightness")


def parse_preset(value: str) -> str:
    """A ``tapo.Color`` preset name from the form."""
    text = (value or "").strip()
    allowed = {name for name, _label in COLOR_PRESETS}
    if text not in allowed:
        raise ControlError("That color preset is not supported.")
    return text


def device_cards(devices: list[ControlDevice]) -> list[dict]:
    """One card per address. Outlets on the same strip stay on that card."""
    grouped: dict[str, list[ControlDevice]] = {}
    order: list[str] = []
    for device in devices:
        if device.ip not in grouped:
            order.append(device.ip)
            grouped[device.ip] = []
        grouped[device.ip].append(device)
    cards = []
    for ip in order:
        rows = grouped[ip]
        outlets = [row for row in rows if row.child_id]
        if outlets:
            cards.append(_strip_card(rows, outlets))
        else:
            cards.append(_single_card(rows[0]))
    return cards


def _appearance(device: ControlDevice, *, presets: bool) -> dict:
    """Color fields for a card. A strip card passes ``presets`` false."""
    return {
        "can_color": device.can_color,
        "can_temperature": device.can_temperature,
        "can_preset": device.can_preset,
        "hue_input": device.hue_input,
        "saturation_input": device.saturation_input,
        "color_temp_input": device.color_temp_input,
        "color_temp": device.color_temp,
        "presets": (
            [{"id": name, "label": label} for name, label in COLOR_PRESETS]
            if presets
            else []
        ),
        "note": device.note,
    }


def _single_card(device: ControlDevice) -> dict:
    """A plug or bulb. An online device with no special features still switches."""
    # A note means this card already explained a missing feature. Do not add a
    # switch that the handler does not have.
    return {
        "name": device.name,
        "model": device.model,
        "ip": device.ip,
        "online": device.online,
        "status": device.status,
        "can_switch": device.can_switch or (device.online and not device.note),
        "can_dim": device.can_dim,
        "brightness_input": device.brightness_input,
        "child_id": device.child_id,
        "outlets": [],
        **_appearance(device, presets=device.can_preset),
    }


def _strip_card(rows: list[ControlDevice], outlets: list[ControlDevice]) -> dict:
    """One power strip and an on/off control for each outlet the device reported."""
    title = outlets[0].name.split(" · ", 1)[0]
    model = next(
        (row.model for row in rows if row.model and row.model != "unknown"),
        outlets[0].model,
    )
    online = any(row.online for row in outlets)
    shown = []
    for row in outlets:
        label = row.name.split(" · ", 1)[1] if " · " in row.name else row.name
        shown.append(
            {
                "name": label,
                "child_id": row.child_id,
                "status": row.status,
                "online": row.online,
                "can_switch": row.can_switch or row.online,
            }
        )
    return {
        "name": title,
        "model": model,
        "ip": outlets[0].ip,
        "online": online,
        "status": "Online" if online else "Offline",
        "can_switch": False,
        "can_dim": False,
        "brightness_input": 100,
        "child_id": "",
        "outlets": shown,
        **_appearance(
            ControlDevice(
                name=title,
                model=model,
                ip=outlets[0].ip,
                online=online,
                power=None,
                brightness=None,
                child_id="",
                can_switch=False,
                can_dim=False,
            ),
            presets=False,
        ),
    }


def _read(source, name: str):
    if source is None:
        return None
    try:
        return getattr(source, name)
    except Exception:
        return None


def _text(value) -> str:
    if isinstance(value, str):
        return value.strip()
    return ""


def _label(value, fallback: str) -> str:
    text = _text(value)
    if not text:
        return fallback
    try:
        name = poc.display_name(text)
    except Exception:
        return fallback
    if not name or name == "(unnamed)":
        return fallback
    return name


def _power(value) -> str | None:
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"on", "true", "1"}:
            return "on"
        if lowered in {"off", "false", "0"}:
            return "off"
    return None


def _brightness(value) -> int | None:
    """Brightness from a device info object, or from an integer already read off one."""
    if isinstance(value, bool) or value is None:
        return None
    number = value if isinstance(value, int) else _read(value, "brightness")
    if isinstance(number, bool) or number is None:
        return None
    try:
        parsed = int(number)
    except (TypeError, ValueError):
        return None
    if 0 <= parsed <= 100:
        return parsed
    return None


def _can_call(handler, name: str) -> bool:
    return callable(_read(handler, name))


def _span(value, low: int, high: int) -> int | None:
    """An integer inside ``low``..``high``. Booleans are not numbers here."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if low <= number <= high:
        return number
    return None


def _is_strip_handler(handler) -> bool:
    return _can_call(handler, "get_child_device_list") or _can_call(handler, "plug_unchecked")


def _annotate(device: ControlDevice, handler) -> ControlDevice:
    """Hide controls the handler cannot perform, and say so on the card."""
    if device.child_id:
        return replace(
            device,
            can_dim=False,
            can_color=False,
            can_temperature=False,
            can_preset=False,
        )
    model = (device.model or "").strip().upper()
    if _is_strip_handler(handler) or model in _STRIP_MODELS:
        if _is_strip_handler(handler):
            note = "This power strip did not report any outlets."
            log = "%s reported no outlets."
        else:
            note = "Individual outlets are not available from this session."
            log = "%s did not open as a power strip, so its outlets were not listed."
        if device.note != note:
            _controls_log.warning(log, device.model if device.model != "unknown" else "Power strip")
        return replace(
            device,
            can_dim=False,
            can_color=False,
            can_temperature=False,
            can_preset=False,
            brightness=None,
            note=note,
        )
    if device.can_color or device.can_temperature or device.can_preset or not device.can_dim:
        if device.note:
            return replace(device, note="")
        return device
    if model in _COLOR_MODELS:
        note = "Color controls are not available in this session. On/off and brightness still work."
        if device.note != note:
            _controls_log.warning(
                "%s did not open as a color light. Color controls were left off.",
                device.model,
            )
        return replace(device, note=note)
    return replace(device, note="Color and color temperature are not available on this light.")


def _light_fields(handler, info) -> dict:
    """Color capability and current values. Strips and outlets stay switched only."""
    can_color = _can_call(handler, "set_hue_saturation")
    can_temperature = _can_call(handler, "set_color_temperature")
    can_preset = _can_call(handler, "set_color")
    return {
        "can_color": can_color,
        "can_temperature": can_temperature,
        "can_preset": can_preset,
        "hue": _span(_read(info, "hue"), 0, 360) if can_color else None,
        "saturation": _span(_read(info, "saturation"), 0, 100) if can_color else None,
        "color_temp": _span(_read(info, "color_temp"), 2500, 6500) if can_temperature else None,
    }


def _parent_row(handler, info, name: str, model: str, ip: str) -> ControlDevice:
    """One plug or bulb. A strip handler does not become a brightness slider."""
    fields = _light_fields(handler, info)
    strip = _is_strip_handler(handler)
    can_dim = _can_call(handler, "set_brightness") and not strip
    if strip:
        fields = {"can_color": False, "can_temperature": False, "can_preset": False, "hue": None, "saturation": None, "color_temp": None}
    return ControlDevice(
        name=name or "Unnamed device",
        model=model,
        ip=ip,
        online=True,
        power=_power(_read(info, "device_on")),
        brightness=_brightness(info) if can_dim else None,
        child_id="",
        can_switch=_can_call(handler, "on") and _can_call(handler, "off"),
        can_dim=can_dim,
        **fields,
    )


class _AttrDict:
    """A JSON object with attribute access for ``_read``."""

    def __init__(self, data: dict) -> None:
        self._data = data

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return self._data.get(name)


def _child_payload(payload) -> list:
    """Outlet rows from a power-strip child list, including a wrapped result."""
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = None
        for key in ("child_device_list", "device_list"):
            value = payload.get(key)
            if isinstance(value, list):
                rows = value
                break
        if rows is None:
            result = payload.get("result")
            if isinstance(result, dict):
                return _child_payload(result)
            rows = []
    else:
        rows = []
    found = []
    for row in rows:
        if isinstance(row, dict):
            found.append(_AttrDict(row))
        elif row is not None:
            found.append(row)
    return found


def _outlet_order(child) -> tuple[int, int, str]:
    """Sort outlets by slot, then position, then name."""
    def number(value) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 999
    return (
        number(_read(child, "slot_number")),
        number(_read(child, "position")),
        _text(_read(child, "nickname")).casefold(),
    )


def _outlet_label(child, index: int) -> str:
    """A readable outlet name when the device has no nickname."""
    slot = _read(child, "slot_number")
    position = _read(child, "position")
    if isinstance(slot, int) and not isinstance(slot, bool) and slot > 0:
        return f"Outlet {slot}"
    if isinstance(position, int) and not isinstance(position, bool) and position >= 0:
        return f"Outlet {position + 1}"
    return f"Outlet {index}"


async def _child_plugs(handler) -> list:
    """Outlets reported by the strip. The JSON list is the fallback."""
    children = []
    if _can_call(handler, "get_child_device_list"):
        try:
            listed = await handler.get_child_device_list()
            children = [item for item in list(listed or []) if item is not None]
        except Exception as err:
            if poc.is_auth_failure(err):
                raise ControlError(_AUTH_MESSAGE) from err
            children = []
    if children or not _can_call(handler, "get_child_device_list_json"):
        return children
    try:
        payload = await handler.get_child_device_list_json()
    except Exception as err:
        if poc.is_auth_failure(err):
            raise ControlError(_AUTH_MESSAGE) from err
        return []
    return _child_payload(payload)


def _short(err: BaseException) -> str:
    text = " ".join(str(err).split())
    return text[:300] if text else "The device lookup failed."


def _session_expired(err: BaseException) -> bool:
    text = str(err).lower()
    return "session_timeout" in text or "session expired" in text


def _credentials() -> tuple[str, str]:
    username = os.getenv("TAPO_USERNAME", "").strip()
    password = os.getenv("TAPO_PASSWORD", "").strip()
    if not username or not password:
        raise ControlError(
            "Set TAPO_USERNAME and TAPO_PASSWORD to the cloud TP-Link "
            "account that owns these devices."
        )
    return username, password


def _config_token(*paths: Path) -> tuple:
    parts = []
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            parts.append((path.name, "missing"))
        else:
            parts.append((path.name, stat.st_mtime_ns, stat.st_size))
    return tuple(parts)


def _copy_listing(listing: ControlList) -> ControlList:
    return ControlList(
        [replace(device) for device in listing.devices],
        listing.source,
        listing.error,
    )


def _offline(ip: str, name: str = "") -> ControlDevice:
    return ControlDevice(
        name=name or ip,
        model="unknown",
        ip=ip,
        online=False,
        power=None,
        brightness=None,
        child_id="",
        can_switch=False,
        can_dim=False,
    )


class ControlSession:
    """One Tapo client for plugs and lights. The hub login stays in ``app.hub``."""

    def __init__(self) -> None:
        self._api = None
        self._lock = None
        self._handlers: dict[str, object] = {}
        self._extras: dict[str, ExtraDevice] = {}
        self._listed: ControlList | None = None
        self._listed_at = 0.0
        self._token: tuple = ()

    def _guard(self):
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def _client(self):
        if self._api is not None:
            return self._api
        username, password = _credentials()
        try:
            from tapo import ApiClient
        except ImportError as err:
            raise ControlError(
                'The tapo package is not installed. Run: python -m pip install "tapo>=0.10"'
            ) from err
        self._api = ApiClient(username, password)
        return self._api

    async def list_devices(self, *, refresh: bool = False) -> ControlList:
        """Devices for the page. A recent result is reused unless ``refresh`` is set."""
        async with self._guard():
            token = _config_token(DEVICES_FILE, EXTRA_DEVICES_FILE)
            cached = (
                not refresh
                and self._listed is not None
                and token == self._token
                and time.monotonic() - self._listed_at < LIST_CACHE_S
            )
            if cached:
                return _copy_listing(self._listed)
            try:
                listing = await self._scan(token)
            except ControlError as err:
                self._listed = None
                return ControlList([], "", str(err))
            except Exception as err:
                self._listed = None
                return ControlList([], "", _short(err))
            return listing

    async def set_power(
        self,
        ip: str,
        child_id: str,
        action: str,
        brightness: str = "",
        hue: str = "",
        saturation: str = "",
        color_temp: str = "",
        preset: str = "",
    ) -> None:
        """Turn a device on or off, or set brightness, color, or color temperature."""
        command = validate_command(
            ip,
            child_id,
            action,
            brightness,
            hue,
            saturation,
            color_temp,
            preset,
        )
        async with self._guard():
            self._load_extras()
            client = await self._client()
            await self._invoke(client, command)

    def _load_extras(self) -> str:
        """Read ``extra_devices.yaml``. A bad file returns the page error."""
        try:
            extras = extra_devices_from(EXTRA_DEVICES_FILE)
        except ControlError as err:
            self._extras = {}
            return str(err)
        self._extras = {item.ip: item for item in extras}
        return ""

    async def _scan(self, token: tuple) -> ControlList:
        try:
            fallback = subnet_broadcast(poc.hub_ip())
        except poc.HubAddressError as err:
            raise ControlError(str(err)) from err
        targets, source = targets_for(DEVICES_FILE, fallback)
        extra_error = self._load_extras()
        extras = list(self._extras.values())
        client = await self._client()
        rows: list[ControlDevice] = []
        seen: set[tuple[str, str]] = set()
        found_ips: set[str] = set()
        problems: list[BaseException] = []
        for target in targets:
            try:
                discovered = await self._discover(client, target)
            except ControlError:
                raise
            except Exception as err:
                if poc.is_auth_failure(err):
                    raise ControlError(_AUTH_MESSAGE) from err
                problems.append(err)
                continue
            for device in discovered:
                group, handler = await self._rows_for(device)
                ip = ""
                for row in group:
                    ip = row.ip
                    key = (row.ip, row.child_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    found_ips.add(row.ip)
                    rows.append(row)
                if handler is not None and ip:
                    self._handlers[ip] = handler
        for target in targets:
            if is_broadcast(target) or target in found_ips:
                continue
            rows.append(_offline(target))
        for extra in extras:
            if any(row.ip == extra.ip and row.online for row in rows):
                rows = [
                    prefer_name(row, extra.name) if row.ip == extra.ip else row
                    for row in rows
                ]
                continue
            rows = [row for row in rows if row.ip != extra.ip]
            seen = {key for key in seen if key[0] != extra.ip}
            try:
                group, handler = await self._connect_extra(client, extra)
            except ControlError as err:
                extra_error = str(err)
                break
            ip = ""
            for row in group:
                ip = row.ip
                key = (row.ip, row.child_id)
                if key in seen:
                    continue
                seen.add(key)
                if row.online:
                    found_ips.add(row.ip)
                rows.append(row)
            if handler is not None and ip:
                self._handlers[ip] = handler
        rows.sort(key=lambda row: (not row.online, row.name.casefold(), row.ip, row.child_id))
        error = extra_error
        if not error and problems and not rows:
            error = _short(problems[0])
        listing = ControlList(rows, describe_source(source, len(extras)), error)
        if rows or not error:
            self._listed = listing
            self._listed_at = time.monotonic()
            self._token = token
        return _copy_listing(listing)

    async def _connect_extra(self, client, extra: ExtraDevice) -> tuple[list[ControlDevice], object | None]:
        """Log in to one configured address. Failure becomes an offline row."""
        try:
            handler = await self._open_for_extra(client, extra)
        except ControlError:
            raise
        if handler is None:
            return [_offline(extra.ip, extra.name)], None
        return await self._rows_from_login(handler, extra)

    async def _open_for_extra(self, client, extra: ExtraDevice):
        """Handler for one extra device. A known model is one login, not a light."""
        hint = (extra.model or "").strip() or model_in_text(extra.name)
        method = client_method(hint) if hint else ""
        if method:
            opened = await self._login(client, method, extra.ip)
            if opened is not None:
                return opened
            return await self._handler_from_discovery(client, extra.ip)
        found = await self._handler_from_discovery(client, extra.ip)
        if found is not None:
            return found
        return await self._probe_and_upgrade(client, extra.ip)

    async def _login(self, client, method_name: str, ip: str):
        opener = _read(client, method_name)
        if not callable(opener):
            return None
        try:
            return await asyncio.wait_for(opener(ip), timeout=DIRECT_TIMEOUT_S)
        except asyncio.TimeoutError:
            return None
        except Exception as err:
            if poc.is_auth_failure(err):
                raise ControlError(_AUTH_MESSAGE) from err
            return None

    async def _handler_from_discovery(self, client, ip: str):
        """Unicast discovery for one address. A miss is None, not another login."""
        try:
            discovered = await self._discover(client, ip, EXTRA_DISCOVERY_TIMEOUT_S)
        except ControlError:
            raise
        except Exception:
            return None
        for device in discovered:
            info = _read(device, "device_info")
            found = normalize_ipv4(_text(_read(device, "ip"))) or normalize_ipv4(
                _text(_read(info, "ip"))
            )
            handler = _read(device, "handler")
            if found == ip and handler is not None:
                return handler
        return None

    async def _probe_and_upgrade(self, client, ip: str):
        """One light login, then one upgrade when the device reports another model."""
        probe = await self._login(client, "l510", ip)
        if probe is None:
            return None
        reported = await _reported_model(probe)
        upgrade = method_for_reported_model(reported)
        if not upgrade or upgrade == "l510":
            return probe
        opened = await self._login(client, upgrade, ip)
        if opened is not None:
            return opened
        _controls_log.warning(
            "Reported model %s needs %s, but that login failed. Color or outlets may be missing.",
            reported.strip(),
            upgrade,
        )
        return probe

    async def _rows_from_login(
        self, handler, extra: ExtraDevice
    ) -> tuple[list[ControlDevice], object]:
        logged_in = _DirectDevice(handler, extra.ip, extra.name)
        group, found = await self._rows_for(logged_in)
        raw = {}
        needs_raw = any(
            row.model == "unknown"
            or row.power is None
            or (row.can_dim and row.brightness is None)
            or (row.can_color and (row.hue is None or row.saturation is None))
            or (row.can_temperature and row.color_temp is None)
            for row in group
        )
        if needs_raw:
            raw = await _json_info(handler)
        filled = []
        for row in group:
            model = row.model if row.model != "unknown" else (_text(raw.get("model")) or "unknown")
            power = row.power if row.power is not None else _power(raw.get("device_on"))
            brightness = row.brightness
            if brightness is None and row.can_dim and not row.child_id:
                brightness = _brightness(raw.get("brightness"))
            hue = row.hue
            saturation = row.saturation
            color_temp = row.color_temp
            if row.can_color and not row.child_id:
                if hue is None:
                    hue = _span(raw.get("hue"), 0, 360)
                if saturation is None:
                    saturation = _span(raw.get("saturation"), 0, 100)
            if row.can_temperature and color_temp is None and not row.child_id:
                color_temp = _span(raw.get("color_temp"), 2500, 6500)
            updated = prefer_name(
                replace(
                    row,
                    model=model,
                    power=power,
                    brightness=brightness if row.can_dim else None,
                    hue=hue,
                    saturation=saturation,
                    color_temp=color_temp,
                ),
                extra.name,
            )
            filled.append(_annotate(updated, handler))
        return filled or [_offline(extra.ip, extra.name)], found or handler

    async def _discover(self, client, target: str, timeout: int | None = None) -> list:
        found = await client.discover_devices(target, timeout or DISCOVERY_TIMEOUT_S)
        devices = []
        async for item in found:
            try:
                device = item.get()
            except Exception as err:
                if poc.is_auth_failure(err):
                    raise ControlError(_AUTH_MESSAGE) from err
                continue
            if is_camera_device(device):
                continue
            devices.append(device)
        return devices

    async def _rows_for(self, device) -> tuple[list[ControlDevice], object | None]:
        handler = _read(device, "handler")
        info = _read(device, "device_info")
        if _can_call(handler, "get_device_info"):
            try:
                info = await handler.get_device_info()
            except Exception as err:
                if poc.is_auth_failure(err):
                    raise ControlError(_AUTH_MESSAGE) from err
        ip = normalize_ipv4(_text(_read(device, "ip"))) or normalize_ipv4(
            _text(_read(info, "ip"))
        )
        if ip is None:
            return [], handler
        children = await _child_plugs(handler)
        parent_name = _label(_read(device, "nickname"), "") or _label(
            _read(info, "nickname"), ""
        )
        model = _text(_read(device, "model")) or _text(_read(info, "model")) or "unknown"
        if not children:
            return [_annotate(_parent_row(handler, info, parent_name, model, ip), handler)], handler
        rows = []
        for child in sorted(children, key=_outlet_order):
            child_id = _text(_read(child, "device_id"))
            if _CHILD_ID.fullmatch(child_id) is None:
                continue
            child_name = _label(_read(child, "nickname"), "") or _outlet_label(child, len(rows) + 1)
            if parent_name and child_name and child_name != parent_name:
                name = f"{parent_name} · {child_name}"
            else:
                name = child_name or parent_name or "Unnamed device"
            rows.append(
                _annotate(
                    ControlDevice(
                        name=name,
                        model=_text(_read(child, "model")) or model,
                        ip=ip,
                        online=True,
                        power=_power(_read(child, "device_on")),
                        brightness=None,
                        child_id=child_id,
                        can_switch=bool(child_id),
                        can_dim=False,
                    ),
                    handler,
                )
            )
        if not rows:
            return [_annotate(_parent_row(handler, info, parent_name, model, ip), handler)], handler
        return rows, handler

    async def _invoke(self, client, command: ControlCommand) -> None:
        ip = command.ip
        child_id = command.child_id
        action = command.action
        for attempt in (1, 2):
            handler = await self._handler_for(client, ip)
            if handler is None:
                raise ControlError("That device did not respond.")
            try:
                target = handler
                if child_id:
                    opener = _read(handler, "plug_unchecked")
                    if not callable(opener):
                        raise ControlError("That outlet was not found on this device.")
                    target = await opener(child_id)
                if action == "on":
                    await target.on()
                    self._touch(ip, child_id, power="on")
                elif action == "off":
                    await target.off()
                    self._touch(ip, child_id, power="off")
                elif action == "brightness":
                    await self._set_brightness(target, command.brightness)
                    if command.brightness == 0:
                        self._touch(ip, child_id, power="off", brightness=0)
                    else:
                        self._touch(ip, child_id, power="on", brightness=command.brightness)
                elif action == "color":
                    setter = _read(target, "set_hue_saturation")
                    if not callable(setter):
                        raise ControlError("This light does not support color.")
                    await setter(command.hue, command.saturation)
                    self._touch(
                        ip,
                        child_id,
                        power="on",
                        hue=command.hue,
                        saturation=command.saturation,
                    )
                elif action == "temperature":
                    setter = _read(target, "set_color_temperature")
                    if not callable(setter):
                        raise ControlError("This light does not support color temperature.")
                    await setter(command.color_temp)
                    self._touch(ip, child_id, power="on", color_temp=command.color_temp)
                elif action == "preset":
                    setter = _read(target, "set_color")
                    if not callable(setter):
                        raise ControlError("This light does not support color presets.")
                    color = _preset_color(command.preset)
                    await setter(color)
                    hue, saturation, color_temp = _preset_levels(color)
                    self._touch(
                        ip,
                        child_id,
                        power="on",
                        hue=hue,
                        saturation=saturation,
                        color_temp=color_temp,
                    )
                else:
                    raise ControlError("That control is not supported.")
                return
            except ControlError:
                raise
            except Exception as err:
                self._handlers.pop(ip, None)
                if poc.is_auth_failure(err):
                    raise ControlError(_AUTH_MESSAGE) from err
                if attempt == 1 and _session_expired(err):
                    continue
                raise ControlError(_short(err)) from err

    async def _set_brightness(self, target, brightness: int | None) -> None:
        if brightness == 0:
            await target.off()
            return
        setter = _read(target, "set_brightness")
        if not callable(setter):
            raise ControlError("This device does not support brightness.")
        await setter(brightness)

    async def _handler_for(self, client, ip: str):
        cached = self._handlers.get(ip)
        if cached is not None:
            return cached
        extra = self._extras.get(ip)
        if extra is not None:
            handler = await self._open_for_extra(client, extra)
            if handler is not None:
                self._handlers[ip] = handler
                return handler
        try:
            discovered = await self._discover(client, ip)
        except ControlError:
            raise
        except Exception:
            discovered = []
        for device in discovered:
            found = normalize_ipv4(_text(_read(device, "ip")))
            handler = _read(device, "handler")
            if found == ip and handler is not None:
                self._handlers[ip] = handler
                return handler
        return None

    def _touch(
        self,
        ip: str,
        child_id: str,
        power: str | None = None,
        brightness: int | None = None,
        hue: int | None = None,
        saturation: int | None = None,
        color_temp: int | None = None,
    ) -> None:
        listing = self._listed
        if listing is None:
            return
        for device in listing.devices:
            if device.ip != ip or device.child_id != child_id:
                continue
            device.online = True
            if power is not None:
                device.power = power
            if brightness is not None:
                device.brightness = brightness
            if hue is not None:
                device.hue = hue
            if saturation is not None:
                device.saturation = saturation
            if color_temp is not None:
                device.color_temp = color_temp
            self._listed_at = time.monotonic()
            return
        self._listed = None


class _DirectDevice:
    """Logged-in handler shaped like a discovery result for ``_rows_for``."""

    def __init__(self, handler, ip: str, name: str) -> None:
        self.handler = handler
        self.ip = ip
        self.nickname = name
        self.model = ""
        self.device_info = None


def _preset_color(name: str):
    try:
        from tapo.requests import Color
    except ImportError as err:
        raise ControlError(
            'The tapo package is not installed. Run: python -m pip install "tapo>=0.10"'
        ) from err
    color = getattr(Color, name, None)
    if color is None:
        raise ControlError("That color preset is not supported.")
    return color


def _preset_levels(color) -> tuple[int | None, int | None, int | None]:
    try:
        hue, saturation, color_temp = color.get_color_config()
    except Exception:
        return None, None, None
    return (
        _span(hue, 0, 360),
        _span(saturation, 0, 100),
        _span(color_temp, 2500, 6500),
    )


async def _reported_model(handler) -> str:
    """Model string from device info, then from the JSON info when that is empty."""
    if _can_call(handler, "get_device_info"):
        try:
            info = await handler.get_device_info()
        except Exception as err:
            if poc.is_auth_failure(err):
                raise ControlError(_AUTH_MESSAGE) from err
            info = None
        reported = _text(_read(info, "model"))
        if reported:
            return reported
    return _text((await _json_info(handler)).get("model"))


async def _json_info(handler) -> dict:
    getter = _read(handler, "get_device_info_json")
    if not callable(getter):
        return {}
    try:
        data = await getter()
    except Exception as err:
        if poc.is_auth_failure(err):
            raise ControlError(_AUTH_MESSAGE) from err
        return {}
    return data if isinstance(data, dict) else {}


session = ControlSession()
