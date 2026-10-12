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
installed ``tapo`` package already recognizes it. Cameras and the H500 are
skipped. The account is ``TAPO_USERNAME`` and ``TAPO_PASSWORD``.
"""

import asyncio
import os
import re
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

# One login per device. The method is the tapo client helper for that model.
# When the config omits model, a light handler is used: it can switch a plug
# and it can dim a bulb that reports brightness.
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
        if self.brightness is None or self.brightness < 1:
            return 100
        return min(self.brightness, 100)


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
    """tapo ``ApiClient`` method for a config model. Blank uses ``l510``."""
    text = (model or "").strip().upper()
    if not text:
        return "l510"
    method = _CLIENT_METHODS.get(text)
    if method is None:
        raise ControlError(
            f"extra_devices.yaml model {text} is not a supported Tapo model."
        )
    return method


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


def validate_command(
    ip: str, child_id: str, action: str, brightness: str
) -> tuple[str, str, str, int | None]:
    """Check a control form before any device is contacted."""
    address = normalize_ipv4(ip)
    if address is None or is_broadcast(address):
        raise ControlError("That address cannot be controlled.")
    outlet = (child_id or "").strip()
    if outlet and _CHILD_ID.fullmatch(outlet) is None:
        raise ControlError("That outlet id is not valid.")
    command = (action or "").strip().lower()
    if command not in {"on", "off", "brightness"}:
        raise ControlError("That control is not supported.")
    level = parse_brightness(brightness) if command == "brightness" else None
    return address, outlet, command, level


def parse_brightness(value: str) -> int:
    """Brightness accepted by ``set_brightness``: an integer from 1 to 100."""
    text = (value or "").strip()
    if not re.fullmatch(r"\d{1,3}", text):
        raise ControlError("Brightness must be a whole number from 1 to 100.")
    number = int(text)
    if not 1 <= number <= 100:
        raise ControlError("Brightness must be a whole number from 1 to 100.")
    return number


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
        self, ip: str, child_id: str, action: str, brightness: str
    ) -> None:
        """Turn a device on or off, or set its brightness."""
        address, outlet, command, level = validate_command(
            ip, child_id, action, brightness
        )
        async with self._guard():
            self._load_extras()
            client = await self._client()
            await self._invoke(client, address, outlet, command, level)

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
            handler = await self._open_direct(client, extra)
        except ControlError:
            raise
        if handler is not None:
            return await self._rows_from_login(handler, extra)
        try:
            discovered = await self._discover(client, extra.ip, EXTRA_DISCOVERY_TIMEOUT_S)
        except ControlError:
            raise
        except Exception:
            discovered = []
        for device in discovered:
            info = _read(device, "device_info")
            ip = normalize_ipv4(_text(_read(device, "ip"))) or normalize_ipv4(
                _text(_read(info, "ip"))
            )
            if ip != extra.ip:
                continue
            group, found = await self._rows_for(device)
            named = [prefer_name(row, extra.name) for row in group]
            if named:
                return named, found
        return [_offline(extra.ip, extra.name)], None

    async def _open_direct(self, client, extra: ExtraDevice):
        method_name = client_method(extra.model)
        opener = _read(client, method_name)
        if not callable(opener):
            return None
        try:
            return await asyncio.wait_for(opener(extra.ip), timeout=DIRECT_TIMEOUT_S)
        except asyncio.TimeoutError:
            return None
        except Exception as err:
            if poc.is_auth_failure(err):
                raise ControlError(_AUTH_MESSAGE) from err
            return None

    async def _rows_from_login(
        self, handler, extra: ExtraDevice
    ) -> tuple[list[ControlDevice], object]:
        logged_in = _DirectDevice(handler, extra.ip, extra.name)
        group, found = await self._rows_for(logged_in)
        raw = {}
        if any(row.brightness is None or row.model == "unknown" or row.power is None for row in group):
            raw = await _json_info(handler)
        filled = []
        for row in group:
            model = row.model
            if model == "unknown":
                model = _text(raw.get("model")) or "unknown"
            power = row.power if row.power is not None else _power(raw.get("device_on"))
            brightness = row.brightness
            if brightness is None:
                brightness = _brightness(raw.get("brightness"))
            can_dim = bool(row.can_dim and brightness is not None)
            filled.append(
                prefer_name(
                    replace(
                        row,
                        model=model,
                        power=power,
                        brightness=brightness,
                        can_dim=can_dim,
                    ),
                    extra.name,
                )
            )
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
        children = []
        if _can_call(handler, "get_child_device_list"):
            try:
                listed = await handler.get_child_device_list()
                children = list(listed or [])
            except Exception as err:
                if poc.is_auth_failure(err):
                    raise ControlError(_AUTH_MESSAGE) from err
                children = []
        parent_name = _label(_read(device, "nickname"), "") or _label(
            _read(info, "nickname"), ""
        )
        model = _text(_read(device, "model")) or _text(_read(info, "model")) or "unknown"
        if not children:
            return [
                ControlDevice(
                    name=parent_name or "Unnamed device",
                    model=model,
                    ip=ip,
                    online=True,
                    power=_power(_read(info, "device_on")),
                    brightness=_brightness(info),
                    child_id="",
                    can_switch=_can_call(handler, "on") and _can_call(handler, "off"),
                    can_dim=_can_call(handler, "set_brightness"),
                )
            ], handler
        rows = []
        for child in children:
            child_id = _text(_read(child, "device_id"))
            if _CHILD_ID.fullmatch(child_id) is None:
                child_id = ""
            child_name = _label(_read(child, "nickname"), "")
            if parent_name and child_name and child_name != parent_name:
                name = f"{parent_name} · {child_name}"
            else:
                name = child_name or parent_name or "Unnamed device"
            rows.append(
                ControlDevice(
                    name=name,
                    model=_text(_read(child, "model")) or model,
                    ip=ip,
                    online=True,
                    power=_power(_read(child, "device_on")),
                    brightness=_brightness(child),
                    child_id=child_id,
                    can_switch=bool(child_id),
                    can_dim=_brightness(child) is not None,
                )
            )
        return rows, handler

    async def _invoke(
        self,
        client,
        ip: str,
        child_id: str,
        action: str,
        brightness: int | None,
    ) -> None:
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
                    self._touch(ip, child_id, "on", None)
                elif action == "off":
                    await target.off()
                    self._touch(ip, child_id, "off", None)
                else:
                    setter = _read(target, "set_brightness")
                    if not callable(setter):
                        raise ControlError("This device does not support brightness.")
                    await setter(brightness)
                    self._touch(ip, child_id, "on", brightness)
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

    async def _handler_for(self, client, ip: str):
        cached = self._handlers.get(ip)
        if cached is not None:
            return cached
        extra = self._extras.get(ip)
        if extra is not None:
            handler = await self._open_direct(client, extra)
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

    def _touch(self, ip: str, child_id: str, power: str | None, brightness: int | None) -> None:
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
