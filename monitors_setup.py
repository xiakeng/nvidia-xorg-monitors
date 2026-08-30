#!/usr/bin/env python3
"""Generate, install and remove NVIDIA Xorg headless monitors."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path
from typing import Any, Iterable, Sequence


class SetupError(RuntimeError):
    pass


MODE_RE = re.compile(r"^(\d+)x(\d+)@(\d+(?:\.\d+)?)$")
CONNECTOR_RE = re.compile(r"^(?:GPU-\d+\.)?(?:DFP|CRT|TV)-\d+$")
BUS_ID_RE = re.compile(r"^PCI:\d+:\d+:\d+$")
SAFE_SERVICE_RE = re.compile(r"^[A-Za-z0-9_.@-]+$")
CTA_VICS = {
    (1920, 1080, 60.0): 16,
    (1920, 1080, 120.0): 63,
    (3840, 2160, 60.0): 97,
    (3840, 2160, 120.0): 118,
}


@dataclasses.dataclass(frozen=True, order=True)
class Mode:
    width: int
    height: int
    refresh: float

    @classmethod
    def parse(cls, value: str) -> "Mode":
        match = MODE_RE.fullmatch(value.strip())
        if not match:
            raise SetupError(f"Invalid mode {value!r}; expected WIDTHxHEIGHT@HZ")
        width, height = int(match[1]), int(match[2])
        refresh = float(match[3])
        if not (320 <= width <= 16384 and 200 <= height <= 8640):
            raise SetupError(f"Mode dimensions out of range: {value}")
        if not (20 <= refresh <= 360):
            raise SetupError(f"Mode refresh rate out of range: {value}")
        return cls(width, height, refresh)

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}"

    @property
    def label(self) -> str:
        refresh = f"{self.refresh:g}"
        return f"{self.resolution}@{refresh}"

    @property
    def cta_vic(self) -> int | None:
        rounded = float(round(self.refresh))
        if abs(self.refresh - rounded) > 0.01:
            return None
        return CTA_VICS.get((self.width, self.height, rounded))

    def refresh_matches(self, actual: float) -> bool:
        return abs(actual - self.refresh) <= max(0.3, self.refresh * 0.002)


@dataclasses.dataclass(frozen=True)
class Monitor:
    name: str
    connector: str
    primary: bool
    default_mode: Mode
    modes: tuple[Mode, ...]

    @property
    def slug(self) -> str:
        value = re.sub(r"[^a-z0-9]+", "-", self.name.lower()).strip("-")
        return value or "monitor"


@dataclasses.dataclass(frozen=True)
class InstallConfig:
    xorg_config: Path
    edid_directory: Path
    state_directory: Path
    display_manager: str
    display: str
    xauthority: Path
    restart: bool
    verify_timeout_seconds: int


@dataclasses.dataclass(frozen=True)
class Config:
    source: Path
    gpu_bus_id: str
    mode_validation: tuple[str, ...]
    install: InstallConfig
    monitors: tuple[Monitor, ...]


@dataclasses.dataclass(frozen=True)
class Timing:
    pixel_clock_mhz: float
    h_active: int
    h_total: int
    h_sync_start: int
    h_sync_end: int
    v_active: int
    v_total: int
    v_sync_start: int
    v_sync_end: int


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SetupError(f"{label} must be a TOML table")
    return value


def _absolute_path(value: Any, label: str) -> Path:
    path = Path(str(value))
    if not path.is_absolute() or path == Path("/"):
        raise SetupError(f"{label} must be a specific absolute path")
    return path


def load_config(path: Path) -> Config:
    path = path.expanduser().resolve()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SetupError(f"Cannot read {path}: {exc}") from exc

    if raw.get("version") != 1:
        raise SetupError("Only config version 1 is supported")

    gpu = _require_mapping(raw.get("gpu"), "gpu")
    bus_id = str(gpu.get("bus_id", ""))
    if not BUS_ID_RE.fullmatch(bus_id):
        raise SetupError("gpu.bus_id must look like PCI:1:0:0")
    validation = tuple(str(item) for item in gpu.get("mode_validation", []))
    for item in validation:
        if not re.fullmatch(r"[A-Za-z0-9]+", item):
            raise SetupError(f"Unsafe ModeValidation token: {item!r}")

    install_raw = _require_mapping(raw.get("install"), "install")
    install = InstallConfig(
        xorg_config=_absolute_path(install_raw.get("xorg_config"), "install.xorg_config"),
        edid_directory=_absolute_path(
            install_raw.get("edid_directory"), "install.edid_directory"
        ),
        state_directory=_absolute_path(
            install_raw.get("state_directory"), "install.state_directory"
        ),
        display_manager=str(install_raw.get("display_manager", "lightdm")),
        display=str(install_raw.get("display", ":0")),
        xauthority=_absolute_path(install_raw.get("xauthority"), "install.xauthority"),
        restart=bool(install_raw.get("restart", True)),
        verify_timeout_seconds=int(install_raw.get("verify_timeout_seconds", 15)),
    )
    if not SAFE_SERVICE_RE.fullmatch(install.display_manager):
        raise SetupError("install.display_manager contains unsafe characters")
    if not re.fullmatch(r":\d+", install.display):
        raise SetupError("install.display must look like :0")
    if not 1 <= install.verify_timeout_seconds <= 120:
        raise SetupError("verify_timeout_seconds must be between 1 and 120")
    if not str(install.edid_directory).startswith("/etc/X11/edid/"):
        raise SetupError("edid_directory must be below /etc/X11/edid")
    if not str(install.state_directory).startswith("/var/lib/"):
        raise SetupError("state_directory must be below /var/lib")

    monitor_values = raw.get("monitors")
    if not isinstance(monitor_values, list) or not monitor_values:
        raise SetupError("At least one [[monitors]] table is required")

    monitors: list[Monitor] = []
    for index, value in enumerate(monitor_values, start=1):
        item = _require_mapping(value, f"monitors[{index}]")
        name = str(item.get("name", ""))
        try:
            encoded_name = name.encode("ascii")
        except UnicodeEncodeError as exc:
            raise SetupError(f"Monitor name must be ASCII: {name!r}") from exc
        if not (1 <= len(encoded_name) <= 12) or any(byte < 0x20 for byte in encoded_name):
            raise SetupError("Monitor name must contain 1-12 printable ASCII characters")
        connector = str(item.get("connector", ""))
        if not CONNECTOR_RE.fullmatch(connector):
            raise SetupError(f"Invalid NVIDIA connector: {connector!r}")
        raw_modes = item.get("modes")
        if not isinstance(raw_modes, list) or not raw_modes:
            raise SetupError(f"Monitor {name!r} must define at least one mode")
        parsed_modes = tuple(Mode.parse(str(mode)) for mode in raw_modes)
        if len(set(parsed_modes)) != len(parsed_modes):
            raise SetupError(f"Monitor {name!r} contains duplicate modes")
        default_mode = Mode.parse(str(item.get("default_mode", "")))
        if default_mode not in parsed_modes:
            raise SetupError(f"Monitor {name!r} default_mode is not in modes")
        ordered_modes = (default_mode,) + tuple(
            mode for mode in parsed_modes if mode != default_mode
        )
        monitors.append(
            Monitor(
                name=name,
                connector=connector,
                primary=bool(item.get("primary", False)),
                default_mode=default_mode,
                modes=ordered_modes,
            )
        )

    if len({monitor.name for monitor in monitors}) != len(monitors):
        raise SetupError("Monitor names must be unique")
    if len({monitor.slug for monitor in monitors}) != len(monitors):
        raise SetupError("Monitor names must produce unique file names")
    if len({monitor.connector for monitor in monitors}) != len(monitors):
        raise SetupError("Monitor connectors must be unique")
    if sum(monitor.primary for monitor in monitors) > 1:
        raise SetupError("Only one monitor may be primary")

    return Config(path, bus_id, validation, install, tuple(monitors))


def run_cvt(mode: Mode) -> Timing:
    refresh = f"{mode.refresh:g}"
    try:
        result = subprocess.run(
            ["cvt", "-r", str(mode.width), str(mode.height), refresh],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise SetupError("The 'cvt' command is required (package: xcvt)") from exc
    except subprocess.CalledProcessError as exc:
        raise SetupError(f"cvt rejected {mode.label}: {exc.stderr.strip()}") from exc

    modeline = next(
        (line for line in result.stdout.splitlines() if line.startswith("Modeline ")),
        None,
    )
    if modeline is None:
        raise SetupError(f"cvt did not return a Modeline for {mode.label}")
    tokens = shlex.split(modeline)
    if len(tokens) < 11:
        raise SetupError(f"Unexpected cvt output for {mode.label}: {modeline}")
    values = [float(tokens[2])] + [int(value) for value in tokens[3:11]]
    return Timing(
        pixel_clock_mhz=values[0],
        h_active=values[1],
        h_sync_start=values[2],
        h_sync_end=values[3],
        h_total=values[4],
        v_active=values[5],
        v_sync_start=values[6],
        v_sync_end=values[7],
        v_total=values[8],
    )


def encode_detailed_timing(timing: Timing) -> bytes:
    h_blank = timing.h_total - timing.h_active
    v_blank = timing.v_total - timing.v_active
    h_sync_offset = timing.h_sync_start - timing.h_active
    h_sync_width = timing.h_sync_end - timing.h_sync_start
    v_sync_offset = timing.v_sync_start - timing.v_active
    v_sync_width = timing.v_sync_end - timing.v_sync_start
    clock_10khz = round(timing.pixel_clock_mhz * 100)
    if clock_10khz > 0xFFFF:
        raise SetupError(
            f"Non-CTA mode {timing.h_active}x{timing.v_active} requires a pixel "
            "clock above the EDID detailed-timing limit"
        )

    width_mm = 600
    height_mm = max(1, round(width_mm * timing.v_active / timing.h_active))
    dtd = bytearray(18)
    dtd[0:2] = clock_10khz.to_bytes(2, "little")
    dtd[2] = timing.h_active & 0xFF
    dtd[3] = h_blank & 0xFF
    dtd[4] = ((timing.h_active >> 8) << 4) | (h_blank >> 8)
    dtd[5] = timing.v_active & 0xFF
    dtd[6] = v_blank & 0xFF
    dtd[7] = ((timing.v_active >> 8) << 4) | (v_blank >> 8)
    dtd[8] = h_sync_offset & 0xFF
    dtd[9] = h_sync_width & 0xFF
    dtd[10] = ((v_sync_offset & 0x0F) << 4) | (v_sync_width & 0x0F)
    dtd[11] = (
        ((h_sync_offset >> 8) & 0x03) << 6
        | ((h_sync_width >> 8) & 0x03) << 4
        | ((v_sync_offset >> 4) & 0x03) << 2
        | ((v_sync_width >> 4) & 0x03)
    )
    dtd[12] = width_mm & 0xFF
    dtd[13] = height_mm & 0xFF
    dtd[14] = ((width_mm >> 8) << 4) | (height_mm >> 8)
    dtd[17] = 0x1A  # digital separate sync, +HSync, -VSync
    return bytes(dtd)


def text_descriptor(tag: int, value: str) -> bytes:
    descriptor = bytearray(18)
    descriptor[3] = tag
    descriptor[5:18] = (value + "\n").encode("ascii")[:13].ljust(13, b" ")
    return bytes(descriptor)


def dummy_descriptor() -> bytes:
    descriptor = bytearray(18)
    descriptor[3] = 0x10
    return bytes(descriptor)


def set_checksum(block: bytearray) -> None:
    block[127] = (-sum(block[:127])) & 0xFF


def generate_edid(monitor: Monitor) -> bytes:
    cta_modes = [mode for mode in monitor.modes if mode.cta_vic is not None]
    detailed_modes = [mode for mode in monitor.modes if mode.cta_vic is None]
    dtds = [encode_detailed_timing(run_cvt(mode)) for mode in detailed_modes]

    base = bytearray(128)
    base[0:8] = bytes.fromhex("00 ff ff ff ff ff ff 00")
    base[8:10] = bytes.fromhex("0d e4")  # Manufacturer: COD
    digest = hashlib.sha256(monitor.name.encode("ascii")).digest()
    base[10:12] = digest[0:2]
    base[12:16] = digest[2:6]
    now = dt.datetime.now()
    base[16] = min(53, int(now.strftime("%U")) + 1)
    base[17] = now.year - 1990
    base[18:20] = bytes([1, 4])
    base[20] = 0xA5  # digital, 8 bpc, DisplayPort
    max_height_mm = max(round(600 * mode.height / mode.width) for mode in monitor.modes)
    base[21] = 60
    base[22] = min(255, (max_height_mm + 9) // 10)
    base[23] = 120  # gamma 2.20
    base[24] = 0x04  # sRGB
    base[25:35] = bytes.fromhex("ee 91 a3 54 4c 99 26 0f 50 54")
    base[35:38] = bytes([0x20, 0x00, 0x00])  # 640x480p60
    base[38:54] = bytes.fromhex("01 01 " * 8)

    descriptors = dtds[:3] + [text_descriptor(0xFC, monitor.name)]
    descriptors = descriptors[:4]
    while len(descriptors) < 4:
        descriptors.append(dummy_descriptor())
    for index, descriptor in enumerate(descriptors):
        start = 54 + index * 18
        base[start : start + 18] = descriptor

    remaining_dtds = dtds[3:]
    needs_cta = bool(cta_modes or remaining_dtds)
    base[126] = 1 if needs_cta else 0
    set_checksum(base)
    if not needs_cta:
        return bytes(base)

    cta = bytearray(128)
    cta[0:2] = bytes([0x02, 0x03])
    offset = 4
    if cta_modes:
        if len(cta_modes) > 31:
            raise SetupError(f"Monitor {monitor.name!r} has too many CTA modes")
        cta[offset] = 0x40 | len(cta_modes)
        offset += 1
        for mode in cta_modes:
            cta[offset] = int(mode.cta_vic)
            offset += 1
        cta[offset : offset + 3] = bytes([0xE2, 0x00, 0x4A])
        offset += 3
        cta[3] = 0x80
    max_cta_dtds = (127 - offset) // 18
    if len(remaining_dtds) > max_cta_dtds:
        raise SetupError(
            f"Monitor {monitor.name!r} has too many non-CTA modes for one EDID"
        )
    cta[2] = offset
    for descriptor in remaining_dtds:
        cta[offset : offset + 18] = descriptor
        offset += 18
    set_checksum(cta)
    return bytes(base + cta)


def validate_edid(edid: bytes, monitor: Monitor) -> None:
    if len(edid) not in (128, 256):
        raise SetupError(f"Generated EDID for {monitor.name!r} has invalid size")
    for offset in range(0, len(edid), 128):
        if sum(edid[offset : offset + 128]) % 256:
            raise SetupError(f"Generated EDID for {monitor.name!r} has bad checksum")


def render_xorg(config: Config) -> str:
    connectors = ",".join(monitor.connector for monitor in config.monitors)
    custom_edids = "; ".join(
        f"{monitor.connector}:{config.install.edid_directory / (monitor.slug + '.bin')}"
        for monitor in config.monitors
    )
    validation = "; ".join(
        f"{monitor.connector}: {', '.join(config.mode_validation)}"
        for monitor in config.monitors
        if config.mode_validation
    )
    positions: list[tuple[int, int]] = []
    x_offset = 0
    for monitor in config.monitors:
        positions.append((x_offset, 0))
        x_offset += monitor.default_mode.width
    metamodes = ", ".join(
        f"{monitor.connector}: {monitor.default_mode.resolution} +{x}+{y}"
        for monitor, (x, y) in zip(config.monitors, positions)
    )
    primary = next((monitor.connector for monitor in config.monitors if monitor.primary), None)

    options = [
        '    Option "AllowEmptyInitialConfiguration" "true"',
        f'    Option "ConnectedMonitor" "{connectors}"',
        f'    Option "UseDisplayDevice" "{connectors}"',
        f'    Option "CustomEDID" "{custom_edids}"',
        f'    Option "MetaModes" "{metamodes}"',
        '    Option "UseHotplugEvents" "false"',
    ]
    if validation:
        options.insert(4, f'    Option "ModeValidation" "{validation}"')
    if primary:
        options.append(f'    Option "nvidiaXineramaInfoOrder" "{primary}"')

    config_hash = hashlib.sha256(config.source.read_bytes()).hexdigest()
    return "\n".join(
        [
            "# Managed by monitors-setup. Re-run install.sh after editing config.toml.",
            f"# Source config SHA-256: {config_hash}",
            "",
            'Section "ServerLayout"',
            '    Identifier "MonitorsSetupLayout"',
            '    Screen 0 "MonitorsSetupScreen"',
            "EndSection",
            "",
            'Section "Monitor"',
            '    Identifier "MonitorsSetupMonitor"',
            '    Option "DPMS" "false"',
            "EndSection",
            "",
            'Section "Device"',
            '    Identifier "MonitorsSetupGPU"',
            '    Driver "nvidia"',
            f'    BusID "{config.gpu_bus_id}"',
            *options,
            "EndSection",
            "",
            'Section "Screen"',
            '    Identifier "MonitorsSetupScreen"',
            '    Device "MonitorsSetupGPU"',
            '    Monitor "MonitorsSetupMonitor"',
            "    DefaultDepth 24",
            '    SubSection "Display"',
            "        Depth 24",
            "    EndSubSection",
            "EndSection",
            "",
        ]
    )


def render(config: Config, destination: Path) -> dict[str, Path]:
    destination.mkdir(parents=True, exist_ok=True)
    result: dict[str, Path] = {}
    for monitor in config.monitors:
        edid = generate_edid(monitor)
        validate_edid(edid, monitor)
        path = destination / f"{monitor.slug}.bin"
        path.write_bytes(edid)
        result[monitor.slug] = path
    xorg_path = destination / "xorg.conf"
    xorg_path.write_text(render_xorg(config), encoding="utf-8")
    result["xorg"] = xorg_path
    return result


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SetupError(f"Cannot read installation state {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("version") != 1:
        raise SetupError(f"Unsupported installation state in {path}")
    return value


def require_root() -> None:
    if os.geteuid() != 0:
        raise SetupError("This operation must run as root; use install.sh/uninstall.sh")


def restart_display_manager(config: Config) -> None:
    subprocess.run(
        ["systemctl", "restart", config.install.display_manager], check=True
    )


def query_xrandr(config: Config) -> str:
    environment = os.environ.copy()
    environment["DISPLAY"] = config.install.display
    environment["XAUTHORITY"] = str(config.install.xauthority)
    result = subprocess.run(
        ["xrandr", "--query"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    return result.stdout


def parse_xrandr(output: str) -> dict[str, dict[str, list[float]]]:
    displays: dict[str, dict[str, list[float]]] = {}
    current: str | None = None
    for line in output.splitlines():
        connection = re.match(r"^(\S+)\s+connected(?:\s+primary)?\b", line)
        if connection:
            current = connection[1]
            displays[current] = {}
            continue
        if line and not line[0].isspace():
            current = None
            continue
        if current is None:
            continue
        mode_line = re.match(r"^\s+(\d+x\d+)\s+(.+)$", line)
        if not mode_line:
            continue
        rates: list[float] = []
        for token in mode_line[2].split():
            match = re.match(r"^(\d+(?:\.\d+)?)(?:[*+]+)?$", token)
            if match:
                rates.append(float(match[1]))
        displays[current][mode_line[1]] = rates
    return displays


def output_supports_monitor(
    output_modes: dict[str, list[float]], monitor: Monitor
) -> bool:
    for mode in monitor.modes:
        rates = output_modes.get(mode.resolution, [])
        if not any(mode.refresh_matches(rate) for rate in rates):
            return False
    return True


def displays_match_config(
    displays: dict[str, dict[str, list[float]]], monitors: Sequence[Monitor]
) -> bool:
    output_names = list(displays)

    def match(index: int, remaining: tuple[str, ...]) -> bool:
        if index == len(monitors):
            return True
        monitor = monitors[index]
        for output_name in remaining:
            if output_supports_monitor(displays[output_name], monitor):
                next_remaining = tuple(name for name in remaining if name != output_name)
                if match(index + 1, next_remaining):
                    return True
        return False

    return len(output_names) >= len(monitors) and match(0, tuple(output_names))


def verify_live(config: Config) -> str:
    deadline = time.monotonic() + config.install.verify_timeout_seconds
    last_error = ""
    while time.monotonic() < deadline:
        try:
            if subprocess.run(
                ["systemctl", "is-active", "--quiet", config.install.display_manager]
            ).returncode != 0:
                raise SetupError(f"{config.install.display_manager} is not active")
            output = query_xrandr(config)
            displays = parse_xrandr(output)
            if displays_match_config(displays, config.monitors):
                return output
            last_error = (
                f"connected outputs do not expose every configured mode: "
                f"{', '.join(displays) or 'none'}"
            )
        except (OSError, subprocess.CalledProcessError, SetupError) as exc:
            last_error = str(exc)
        time.sleep(0.5)
    raise SetupError(f"Live display verification timed out: {last_error}")


def install_config(config: Config, *, force: bool, no_restart: bool) -> None:
    require_root()
    state_dir = config.install.state_directory
    state_path = state_dir / "state.json"
    old_state = load_state(state_path)
    target = config.install.xorg_config

    if old_state:
        old_target = Path(old_state["xorg_config"])
        if target != old_target:
            raise SetupError(
                "install.xorg_config changed while installed; uninstall before moving it"
            )
        if target.exists():
            expected = old_state.get("installed_xorg_sha256")
            if expected and sha256_file(target) != expected and not force:
                raise SetupError(
                    f"{target} changed after the previous install; use --force to overwrite"
                )
        for value in old_state.get("installed_edids", []):
            old_edid = Path(value["path"])
            expected = value.get("sha256")
            if (
                old_edid.exists()
                and expected
                and sha256_file(old_edid) != expected
                and not force
            ):
                raise SetupError(
                    f"{old_edid} changed after the previous install; use --force to overwrite"
                )

    with tempfile.TemporaryDirectory(prefix="monitors-setup-") as temporary:
        rendered = render(config, Path(temporary))
        previous_xorg = target.read_bytes() if target.exists() else None
        previous_mode = target.stat().st_mode & 0o777 if target.exists() else 0o644
        previous_edids: dict[Path, bytes] = {}
        if old_state:
            for value in old_state.get("installed_edids", []):
                old_path = Path(value["path"])
                if old_path.exists():
                    previous_edids[old_path] = old_path.read_bytes()

        state_dir.mkdir(parents=True, exist_ok=True)
        original_dir = state_dir / "original"
        original_dir.mkdir(parents=True, exist_ok=True)
        if old_state:
            original_existed = bool(old_state["original_xorg_existed"])
            original_backup = old_state.get("original_xorg_backup")
        else:
            original_existed = target.exists()
            original_backup = str(original_dir / "xorg.conf") if original_existed else None
            if original_existed:
                atomic_write(Path(original_backup), target.read_bytes(), previous_mode)

        installed_edids: list[dict[str, str]] = []
        try:
            config.install.edid_directory.mkdir(parents=True, exist_ok=True)
            for monitor in config.monitors:
                source = rendered[monitor.slug]
                destination = config.install.edid_directory / f"{monitor.slug}.bin"
                atomic_write(destination, source.read_bytes())
                installed_edids.append(
                    {"path": str(destination), "sha256": sha256_file(destination)}
                )
            atomic_write(target, rendered["xorg"].read_bytes())

            should_restart = config.install.restart and not no_restart
            live_output = ""
            if should_restart:
                restart_display_manager(config)
                live_output = verify_live(config)

            new_paths = {Path(value["path"]) for value in installed_edids}
            if old_state:
                for value in old_state.get("installed_edids", []):
                    stale = Path(value["path"])
                    if stale not in new_paths and stale.exists():
                        stale.unlink()

            state = {
                "version": 1,
                "installed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "config_path": str(config.source),
                "config_sha256": hashlib.sha256(config.source.read_bytes()).hexdigest(),
                "xorg_config": str(target),
                "installed_xorg_sha256": sha256_file(target),
                "installed_edids": installed_edids,
                "original_xorg_existed": original_existed,
                "original_xorg_backup": original_backup,
            }
            atomic_write(state_path, (json.dumps(state, indent=2) + "\n").encode())
        except Exception:
            if previous_xorg is None:
                target.unlink(missing_ok=True)
            else:
                atomic_write(target, previous_xorg, previous_mode)
            current_paths = {
                config.install.edid_directory / f"{monitor.slug}.bin"
                for monitor in config.monitors
            }
            for path in current_paths:
                if path in previous_edids:
                    atomic_write(path, previous_edids[path])
                else:
                    path.unlink(missing_ok=True)
            if config.install.restart and not no_restart:
                try:
                    restart_display_manager(config)
                except Exception as rollback_error:
                    print(f"WARNING: rollback restart failed: {rollback_error}", file=sys.stderr)
            raise

    print(f"Installed {len(config.monitors)} virtual monitor(s).")
    print(f"Xorg config: {target}")
    for monitor in config.monitors:
        print(
            f"  {monitor.name}: {monitor.connector}, default {monitor.default_mode.label}, "
            f"{len(monitor.modes)} modes"
        )
    if config.install.restart and not no_restart:
        print("Live :0 verification passed.")
        if subprocess.run(
            ["pgrep", "-f", "Xorg :10 .*xrdp/xorg.conf"],
            stdout=subprocess.DEVNULL,
        ).returncode == 0:
            print("xrdp Xorg :10 is still running.")


def uninstall_config(config: Config, *, force: bool, no_restart: bool) -> None:
    require_root()
    state_path = config.install.state_directory / "state.json"
    state = load_state(state_path)
    if state is None:
        print("monitors-setup is not installed; nothing to do.")
        return

    target = Path(state["xorg_config"])
    if target.exists():
        expected = state.get("installed_xorg_sha256")
        if expected and sha256_file(target) != expected and not force:
            raise SetupError(f"{target} was modified; use --force to restore anyway")
    for value in state.get("installed_edids", []):
        path = Path(value["path"])
        expected = value.get("sha256")
        if path.exists() and expected and sha256_file(path) != expected and not force:
            raise SetupError(f"{path} was modified; use --force to remove it anyway")

    if state["original_xorg_existed"]:
        backup = Path(state["original_xorg_backup"])
        if not backup.exists():
            raise SetupError(f"Original Xorg backup is missing: {backup}")
        atomic_write(target, backup.read_bytes(), backup.stat().st_mode & 0o777)
    else:
        target.unlink(missing_ok=True)

    for value in state.get("installed_edids", []):
        path = Path(value["path"])
        if path.exists():
            path.unlink()

    state_path.unlink()
    if config.install.restart and not no_restart:
        restart_display_manager(config)
        if subprocess.run(
            ["systemctl", "is-active", "--quiet", config.install.display_manager]
        ).returncode != 0:
            raise SetupError(f"{config.install.display_manager} failed after uninstall")
    print("Virtual monitor configuration uninstalled.")
    print("The pre-install Xorg configuration was restored.")


def print_status(config: Config) -> None:
    state_path = config.install.state_directory / "state.json"
    try:
        state = load_state(state_path)
    except PermissionError:
        state = None
    if state is None:
        print("Status: not installed")
    else:
        target = Path(state["xorg_config"])
        intact = target.exists() and sha256_file(target) == state["installed_xorg_sha256"]
        print(f"Status: installed ({'intact' if intact else 'configuration drift'})")
        print(f"Installed at: {state['installed_at']}")
        print(f"Config: {state['config_path']}")
        print(f"Xorg config: {target}")
    try:
        output = query_xrandr(config)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"Live :0 query unavailable: {exc}")
        return
    displays = parse_xrandr(output)
    print(f"Live connected outputs: {', '.join(displays) or 'none'}")
    for name, modes in displays.items():
        print(f"  {name}:")
        for resolution, rates in modes.items():
            formatted_rates = ", ".join(f"{rate:g}" for rate in rates)
            print(f"    {resolution}: {formatted_rates} Hz")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def common(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument(
            "--config", type=Path, default=Path(__file__).with_name("config.toml")
        )

    render_parser = subparsers.add_parser("render", help="render without installing")
    common(render_parser)
    render_parser.add_argument(
        "--output", type=Path, default=Path(__file__).with_name("build")
    )

    install_parser = subparsers.add_parser("install", help="install or update")
    common(install_parser)
    install_parser.add_argument("--force", action="store_true")
    install_parser.add_argument("--no-restart", action="store_true")

    uninstall_parser = subparsers.add_parser("uninstall", help="restore original config")
    common(uninstall_parser)
    uninstall_parser.add_argument("--force", action="store_true")
    uninstall_parser.add_argument("--no-restart", action="store_true")

    status_parser = subparsers.add_parser("status", help="show install and live status")
    common(status_parser)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "render":
            rendered = render(config, args.output.resolve())
            print(f"Rendered {len(config.monitors)} monitor(s) to {args.output.resolve()}")
            for name, path in rendered.items():
                print(f"  {name}: {path}")
        elif args.command == "install":
            install_config(config, force=args.force, no_restart=args.no_restart)
        elif args.command == "uninstall":
            uninstall_config(config, force=args.force, no_restart=args.no_restart)
        elif args.command == "status":
            print_status(config)
        return 0
    except (SetupError, OSError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
