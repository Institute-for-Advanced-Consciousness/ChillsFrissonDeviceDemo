#!/usr/bin/env python3
"""
ChillsDemo — GUI station for running chills / ASMR experiments.

Two modes:
  * **Frisson** — control of the new Arduino-based Frisson haptic device
    (USB serial primary; BLE coming in v2 of the device firmware) with
    editable trigger timings per song and an operator-driven Verify
    Device check sequence.
  * **Suuvi** — audio-only playback from Stimuli/Suuvi/ with ArcTop EEG
    headphone integration.

Launch:
    source venv/bin/activate && python app.py
"""

import array
import asyncio
import atexit
import contextlib
import csv
import json
import math
import os
import random
import re
import subprocess
import sys
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone

# ── Dependency checks ────────────────────────────────────────────────────

_missing = []
try:
    import customtkinter as ctk
except ImportError:
    _missing.append("customtkinter")
try:
    import pygame
except ImportError:
    _missing.append("pygame")
try:
    from bleak import BleakClient, BleakScanner  # noqa: F401 — used by FrissonBLENew stub
except ImportError:
    _missing.append("bleak")
try:
    import websockets
except ImportError:
    _missing.append("websockets")
try:
    import serial
    import serial.tools.list_ports
except ImportError:
    _missing.append("pyserial")

if _missing:
    print("Missing dependencies: " + ", ".join(_missing))
    print("Install them with:  pip install -r requirements.txt")
    sys.exit(1)

# ── Configuration ────────────────────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STIMULI_DIR = os.path.join(BASE_DIR, "Stimuli")
SUUVI_DIR = os.path.join(STIMULI_DIR, "Suuvi")
DATA_DIR = os.path.join(BASE_DIR, "Data")

AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".flac", ".aac", ".m4a"}

SONGS = {
    "Arameic": {
        "file": "Arameic.mp3",
        "triggers": [44, 79, 172],
        "duration_est": 264,
    },
    "Hallelujah": {
        "file": "Hallelujah.mp3",
        "triggers": [74, 93, 145],
        "duration_est": 312,
    },
    "Misere": {
        "file": "Misere.mp3",
        "triggers": [30, 81, 100],
        "duration_est": 270,
    },
}

# ── New Frisson Device — ASCII command protocol ────────────────────────
# Each command is a literal string + '\n', sent over USB serial @ 115200 baud
# (or eventually BLE). Fire-and-forget; the device queues commands while
# busy, so don't overlap commands shorter than a pattern's runtime.
#
# Single channel:  p{1,2,3}-{low,med,high,max}        (~2 s duration)
# Rolling wave:    wave-{low,med,high,max}            P3→P2→P1, ~3.5 s total
# Sequential:      seq-{low,med,high,max}             P1→P2→P3, ~3 s total
# Simultaneous:    sim-{low,med,high,max}             all three, ~2 s
# Emergency stop:  off                                 all channels off NOW
INTENSITY_LEVELS = ("low", "med", "high", "max")
INTENSITY_DUTY = {"low": 20, "med": 50, "high": 80, "max": 100}
SERIAL_BAUD = 115200
ARDUINO_BOOT_DELAY = 1.5  # opening the port resets the Arduino (DTR pulse);
                          # wait for setup() to finish before sending commands.
TRACK_OVERRIDES_FILE = "track_overrides.json"  # project-root-relative

# ── ArcTop EEG (Suuvi mode) ─────────────────────────────────────────────

ARCTOP_HEADPHONES_NAME = "MW75 Neuro"
ARCTOP_WS_URL = (
    "wss://hegemon42.arctop.com/proxy/65c3bbbb/arctop-api/ws"
    "?token=90f698091d3382098ce6998c47d2632e6c5dcf54caa083983f8f6b39c084d146"
)

# ── Colours ──────────────────────────────────────────────────────────────

C_PRIMARY = "#e94560"
C_SUCCESS = "#4ecca3"
C_WARNING = "#f39c12"
C_DANGER = "#e74c3c"
C_MUTED = "#7f8c9b"
C_ACCENT = "#0f3460"
C_SUUVI = "#6c5ce7"  # purple accent for Suuvi mode


def _utc_now() -> str:
    """ISO-8601 UTC timestamp with milliseconds."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _fmt_num(v: float) -> str:
    """Format a number for display in the trigger-times entry: drop trailing .0."""
    if isinstance(v, int) or v == int(v):
        return str(int(v))
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _scan_suuvi_tracks() -> list[str]:
    """Return sorted list of audio filenames in Stimuli/Suuvi/."""
    try:
        return sorted(
            f for f in os.listdir(SUUVI_DIR)
            if os.path.splitext(f)[1].lower() in AUDIO_EXTENSIONS
        )
    except OSError:
        return []


def _analyze_peak(filepath: str) -> int:
    """Load an audio file and return its peak sample amplitude (0–32767).

    Falls back to a sensible default if the file can't be decoded.
    """
    try:
        sound = pygame.mixer.Sound(filepath)
        raw = sound.get_raw()
        samples = array.array("h", raw)  # signed 16-bit interleaved L/R
        # Sample every Nth frame to keep analysis fast on long tracks
        step = max(1, len(samples) // 200_000)
        peak = 0
        for i in range(0, len(samples), step):
            v = abs(samples[i])
            if v > peak:
                peak = v
        del sound, raw, samples
        return peak if peak > 0 else 16384
    except Exception:
        return int(32767 * 0.7)


def _generate_warm_tone(peak_amp: int, sample_rate: int = 44100,
                        duration: float = 3.0) -> "pygame.mixer.Sound":
    """Create a warm, loopable stereo pad tone at *peak_amp* level.

    A detuned A-major chord (A3 root) with gentle tremolo — pleasant
    enough to keep a participant relaxed during volume calibration.
    """
    n_frames = int(sample_rate * duration)

    # Voices: (frequency_hz, relative_amplitude)
    voices = [
        (220.0, 0.35),   # A3 root
        (221.5, 0.25),   # A3 slightly detuned → chorus warmth
        (277.2, 0.20),   # C#4 major third
        (329.6, 0.18),   # E4 perfect fifth
        (440.0, 0.08),   # A4 gentle octave shimmer
    ]
    total_weight = sum(a for _, a in voices)

    buf = array.array("h")  # stereo 16-bit PCM
    for i in range(n_frames):
        t = i / sample_rate
        val = sum(amp * math.sin(2.0 * math.pi * freq * t)
                  for freq, amp in voices)
        # Subtle tremolo for organic feel (0.5 Hz, ±3 %)
        val *= 1.0 + 0.03 * math.sin(2.0 * math.pi * 0.5 * t)
        # Scale so the peak of the mixed signal equals *peak_amp*
        scaled = val / total_weight * peak_amp
        sample = max(-32768, min(32767, int(scaled)))
        buf.append(sample)  # L
        buf.append(sample)  # R

    return pygame.mixer.Sound(buffer=buf)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  FrissonDevice — abstract interface + USB serial implementation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# Threading model: each implementation runs an asyncio event loop in a
# daemon thread (mirroring the prior FrissonBLE pattern that the rest of
# the app already understands). Public methods are blocking sync wrappers
# that submit coroutines via run_coroutine_threadsafe — call them from
# worker threads via _worker(), never the tk main thread.


class FrissonDevice(ABC):
    """Abstract Frisson device. Concrete impls: FrissonSerial, FrissonBLENew."""

    # Last-line-of-feedback notification; not parsed for control flow.
    on_disconnect_callback = None

    @property
    @abstractmethod
    def is_connected(self) -> bool: ...

    @property
    @abstractmethod
    def device_name(self) -> str: ...

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def connect(self) -> tuple[bool, str]: ...

    @abstractmethod
    def disconnect(self) -> tuple[bool, str]: ...

    @abstractmethod
    def send_command(self, command: str) -> tuple[bool, str]:
        """Send one ASCII command (without trailing newline). Fire-and-forget."""

    # ── high-level helpers (shared) ──────────────────────────────────
    def fire_channel(self, channel: int, intensity: str) -> tuple[bool, str]:
        if channel not in (1, 2, 3):
            return False, f"bad channel {channel}"
        if intensity not in INTENSITY_LEVELS:
            return False, f"bad intensity {intensity}"
        return self.send_command(f"p{channel}-{intensity}")

    def fire_wave(self, intensity: str) -> tuple[bool, str]:
        return self.send_command(f"wave-{intensity}")

    def fire_seq(self, intensity: str) -> tuple[bool, str]:
        return self.send_command(f"seq-{intensity}")

    def fire_sim(self, intensity: str) -> tuple[bool, str]:
        return self.send_command(f"sim-{intensity}")

    def emergency_stop(self) -> tuple[bool, str]:
        return self.send_command("off")


class FrissonSerial(FrissonDevice):
    """USB serial connection to the new Arduino Nano ESP32 device.

    Blocking pyserial I/O is wrapped in run_in_executor on a dedicated
    asyncio loop, mirroring the existing async-loop-in-background-thread
    pattern used elsewhere in this app. We chose this over pyserial-asyncio
    because the latter is unmaintained on Python 3.12+, and run_in_executor
    is sufficient for a serial port that's never write-saturated.
    """

    def __init__(self, port: str | None = None, baud: int = SERIAL_BAUD):
        self.port = port
        self.baud = baud
        self._ser: serial.Serial | None = None
        self._connected = False
        self._reader_task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._latest_lines: list[str] = []  # rolling diagnostic log
        self._max_lines = 200

    # ── lifecycle ────────────────────────────────────────────────────
    def start(self):
        if self._loop:
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()

    def stop(self):
        if self._loop and self._connected:
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(
                    self._async_disconnect(), self._loop).result(timeout=3)
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._connected = False

    # ── public API (blocking) ────────────────────────────────────────
    @property
    def is_connected(self) -> bool:
        return self._connected and self._ser is not None and self._ser.is_open

    @property
    def device_name(self) -> str:
        return f"USB {self.port}" if self.port else "USB"

    def connect(self) -> tuple[bool, str]:
        return self._submit(self._async_connect(), timeout=ARDUINO_BOOT_DELAY + 5)

    def disconnect(self) -> tuple[bool, str]:
        return self._submit(self._async_disconnect(), timeout=4)

    def send_command(self, command: str) -> tuple[bool, str]:
        return self._submit(self._async_send(command), timeout=3)

    def recent_log(self) -> list[str]:
        return list(self._latest_lines)

    def _submit(self, coro, timeout=8):
        if not self._loop:
            return False, "Serial loop not started"
        try:
            return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=timeout)
        except Exception as exc:
            return False, str(exc)[:120]

    # ── async internals ──────────────────────────────────────────────
    async def _async_connect(self) -> tuple[bool, str]:
        if self._connected:
            await self._async_disconnect()
        if not self.port:
            return False, "No serial port selected"
        loop = asyncio.get_running_loop()
        try:
            self._ser = await loop.run_in_executor(
                None,
                lambda: serial.Serial(self.port, self.baud, timeout=0.2,
                                      write_timeout=2.0))
        except Exception as exc:
            self._ser = None
            return False, f"Open failed: {str(exc)[:100]}"

        # Opening the port pulses DTR which resets most Arduino boards.
        # Wait for setup() to finish before sending real commands.
        await asyncio.sleep(ARDUINO_BOOT_DELAY)
        with contextlib.suppress(Exception):
            self._ser.reset_input_buffer()

        self._connected = True
        self._reader_task = asyncio.create_task(self._reader_loop())
        return True, self.device_name

    async def _async_disconnect(self) -> tuple[bool, str]:
        # Try a graceful "off" before closing (best-effort; ignore errors).
        if self._ser and self._ser.is_open:
            with contextlib.suppress(Exception):
                self._ser.write(b"off\n")
                self._ser.flush()
        self._connected = False
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await self._reader_task
        self._reader_task = None
        if self._ser:
            with contextlib.suppress(Exception):
                self._ser.close()
        self._ser = None
        return True, "Disconnected"

    async def _async_send(self, command: str) -> tuple[bool, str]:
        if not self.is_connected:
            return False, "Not connected"
        payload = (command.strip() + "\n").encode("ascii", errors="replace")
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._blocking_write, payload)
            return True, "OK"
        except Exception as exc:
            self._connected = False
            return False, f"Write failed: {str(exc)[:100]}"

    def _blocking_write(self, payload: bytes):
        # Called inside the executor; safe to do blocking serial I/O here.
        self._ser.write(payload)
        self._ser.flush()

    async def _reader_loop(self):
        """Drain the Arduino's diagnostic stdout into a rolling buffer."""
        loop = asyncio.get_running_loop()
        try:
            while self._connected and self._ser and self._ser.is_open:
                try:
                    line = await loop.run_in_executor(None, self._blocking_readline)
                except Exception:
                    await asyncio.sleep(0.1)
                    continue
                if not line:
                    await asyncio.sleep(0.02)
                    continue
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    self._latest_lines.append(text)
                    if len(self._latest_lines) > self._max_lines:
                        self._latest_lines = self._latest_lines[-self._max_lines:]
                    print(f"[Frisson] {text}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[Frisson] reader stopped: {exc}")
            self._connected = False
            if self.on_disconnect_callback:
                with contextlib.suppress(Exception):
                    self.on_disconnect_callback()

    def _blocking_readline(self) -> bytes:
        if not (self._ser and self._ser.is_open):
            return b""
        return self._ser.readline()


class FrissonBLENew(FrissonDevice):
    """Stub: BLE support for the new device firmware (v2). Not implemented."""
    # TODO: implement BLE in v2 of the device firmware. The transport will
    # send the same ASCII commands as the serial path; only the connection
    # mechanics differ. Keep the public surface identical.

    def __init__(self):
        self._loop = None

    @property
    def is_connected(self) -> bool:
        return False

    @property
    def device_name(self) -> str:
        return "BLE (not yet supported)"

    def start(self): pass
    def stop(self): pass

    def connect(self) -> tuple[bool, str]:
        return False, "BLE not yet supported on the new device firmware — use USB Serial"

    def disconnect(self) -> tuple[bool, str]:
        return True, "noop"

    def send_command(self, command: str) -> tuple[bool, str]:
        return False, "BLE not connected"


def list_serial_ports() -> list[dict]:
    """Enumerate live USB serial ports, filtering out macOS noise.

    macOS emits two persistent virtual TTYs we never want to see:
      * /dev/cu.Bluetooth-Incoming-Port — generic RFCOMM listener,
        present on every Mac regardless of any pairings
      * /dev/cu.<DeviceName>-…           — every device that has ever
        paired and offered a Serial Port Profile (e.g. AirPods, MX keys),
        whether or not it's currently powered or in range

    These all have vid=None (no USB descriptor) and we drop them. We
    also drop anything whose name explicitly contains 'Bluetooth' as a
    belt-and-suspenders check.
    """
    out = []
    for p in serial.tools.list_ports.comports():
        device = p.device or ""
        description = (p.description or "").strip()
        manufacturer = (p.manufacturer or "").strip()
        product = (p.product or "").strip()
        interface = (p.interface or "").strip()
        vid = p.vid

        # Drop macOS Bluetooth virtual TTYs (no USB descriptor + name says so).
        if vid is None:
            continue
        joined = f"{device} {description} {manufacturer} {product}".lower()
        if "bluetooth" in joined:
            continue

        out.append({
            "device": device,
            "description": description,
            "manufacturer": manufacturer,
            "product": product,
            "interface": interface,
            "is_debug": "debug" in (description + " " + interface).lower(),
        })
    return out


def auto_pick_arduino_port(ports: list[dict]) -> str | None:
    """Pick the best Arduino/ESP32 port.

    Prefers entries whose description/manufacturer mentions Arduino or
    ESP32; among matches, prefers the one that is *not* the debug
    console (Nano ESP32 enumerates two CDC interfaces — sketch + debug).
    """
    pat = re.compile(r"arduino|esp32", re.IGNORECASE)
    matches = [
        p for p in ports
        if pat.search(f"{p['description']} {p['manufacturer']} {p['product']}")
    ]
    if not matches:
        return None
    # Sketch's Serial first, debug last.
    matches.sort(key=lambda p: (p["is_debug"], p["device"]))
    return matches[0]["device"]


def load_track_overrides() -> dict[str, list[float]]:
    """Load per-track trigger time overrides from project-root JSON.

    Returns dict[track_name -> list_of_seconds]. Errors silently → {}.
    """
    path = os.path.join(BASE_DIR, TRACK_OVERRIDES_FILE)
    try:
        with open(path) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    out = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(v, list) and all(isinstance(x, (int, float)) for x in v):
                out[k] = [float(x) for x in v]
    return out


def save_track_overrides(overrides: dict[str, list[float]]) -> None:
    path = os.path.join(BASE_DIR, TRACK_OVERRIDES_FILE)
    with contextlib.suppress(OSError):
        with open(path, "w") as f:
            json.dump(overrides, f, indent=2)


def parse_trigger_times(text: str, max_duration: float | None) -> tuple[list[float] | None, str]:
    """Parse 'a, b, c' → [a, b, c]. Returns (times, error_message).

    Validates: comma-separated non-negative floats, non-decreasing, all under
    max_duration if provided.
    """
    raw = (text or "").strip()
    if not raw:
        return [], ""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    times: list[float] = []
    for p in parts:
        try:
            v = float(p)
        except ValueError:
            return None, f"'{p}' is not a number"
        if v < 0:
            return None, f"'{p}' is negative"
        times.append(v)
    for i in range(1, len(times)):
        if times[i] < times[i - 1]:
            return None, "trigger times must be non-decreasing"
    if max_duration is not None:
        for v in times:
            if v >= max_duration:
                return None, f"{v}s is past track duration ({max_duration:.1f}s)"
    return times, ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ArcTop EEG — environment checks + WebSocket recorder
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _check_mw75_bluetooth() -> tuple[bool, bool, str]:
    """Look for the MW75 Neuro in macOS's Bluetooth listing.

    Returns (paired, connected, status_text).
    """
    try:
        r = subprocess.run(
            ["system_profiler", "SPBluetoothDataType", "-json"],
            capture_output=True, text=True, timeout=8,
        )
        data = json.loads(r.stdout or "{}")
    except Exception as exc:
        return False, False, f"BT query failed: {str(exc)[:60]}"

    target = ARCTOP_HEADPHONES_NAME.lower()

    def _find(devices):
        if not isinstance(devices, list):
            return None
        for entry in devices:
            if not isinstance(entry, dict):
                continue
            for name in entry.keys():
                if isinstance(name, str) and target in name.lower():
                    return name
        return None

    for block in data.get("SPBluetoothDataType", []):
        if not isinstance(block, dict):
            continue
        name = _find(block.get("device_connected"))
        if name:
            return True, True, f"{name} connected"
        name = _find(block.get("device_not_connected"))
        if name:
            return True, False, f"{name} paired (not connected)"

    return False, False, f"{ARCTOP_HEADPHONES_NAME} not found"


def _check_arctop_app_running() -> bool:
    """Return True if a process whose name/cmdline matches 'Arctop' is running."""
    try:
        r = subprocess.run(
            ["pgrep", "-i", "-f", "[Aa]rctop"],
            capture_output=True, text=True, timeout=3,
        )
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        return False


class ArcTopRecorder:
    """Persistent ArcTop WebSocket client.

    The connection lifecycle is independent of recording:

      connect_stream(url)   — open the WS and keep it open (auto-reconnects)
      disconnect_stream()   — tear it down

    Recording layers on top of the live stream:

      start_recording(csv)  — start writing each event to CSV
      stop_recording()      — stop writing, but the WS stays open

    This way the test, the session, and any subsequent sessions all share
    the same connection — Arctop's portal sees one continuous client.
    """

    CSV_HEADER = ["recv_utc", "msg_timestamp_ms", "type",
                  "event_subtype", "paradigm", "score", "raw_json"]

    RECONNECT_DELAY = 2.0

    def __init__(self):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stream_task: asyncio.Task | None = None

        self.is_connected = False     # WS currently open
        self.is_recording = False     # writing CSV
        self.events_received = 0      # since connect_stream
        self.last_event_utc: str | None = None
        self.last_error: str | None = None

        self._stream_url: str | None = None
        self._stop_requested = False
        self._record_start_count = 0

        self.csv_path: str | None = None
        self._csv_fh = None
        self._csv_writer = None

    def start(self):
        if self._loop:
            return
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True)
        self._thread.start()

    def stop(self):
        with contextlib.suppress(Exception):
            self.disconnect_stream()
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

    # ── Connection lifecycle ───────────────────────────────────────
    def connect_stream(self, url: str) -> tuple[bool, str]:
        if not self._loop:
            return False, "Recorder not started"
        if not url:
            return False, "WebSocket URL is empty"
        if self._stream_task and not self._stream_task.done():
            return True, "Already connected"

        self._stream_url = url
        self._stop_requested = False
        self.events_received = 0
        self.last_event_utc = None
        self.last_error = None

        async def _spawn():
            self._stream_task = asyncio.create_task(self._stream_loop())

        try:
            asyncio.run_coroutine_threadsafe(_spawn(), self._loop).result(timeout=2)
        except Exception as exc:
            return False, str(exc)[:80]
        return True, "ok"

    def disconnect_stream(self):
        if not self._loop:
            return
        self._stop_requested = True

        async def _shutdown():
            if self._stream_task and not self._stream_task.done():
                self._stream_task.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await self._stream_task
            self._stream_task = None
            self.is_connected = False
            self._close_csv()
            self.is_recording = False

        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(
                _shutdown(), self._loop).result(timeout=5)

    async def _stream_loop(self):
        try:
            while not self._stop_requested:
                try:
                    async with websockets.connect(
                        self._stream_url, open_timeout=5,
                        ping_interval=20, ping_timeout=20,
                    ) as ws:
                        self.is_connected = True
                        self.last_error = None
                        async for raw in ws:
                            if self._stop_requested:
                                break
                            self._handle_message(raw)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.last_error = str(exc)[:120]
                self.is_connected = False
                if self._stop_requested:
                    break
                # backoff before reconnect
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.sleep(self.RECONNECT_DELAY)
        finally:
            self.is_connected = False

    # ── Recording (overlays the live stream) ───────────────────────
    def start_recording(self, csv_path: str) -> tuple[bool, str]:
        if not self.is_connected:
            return False, "Not connected — connect the stream first"
        if self.is_recording:
            return False, "Already recording"
        try:
            fh = open(csv_path, "w", newline="")
            writer = csv.writer(fh)
            writer.writerow(self.CSV_HEADER)
            fh.flush()
        except OSError as exc:
            return False, f"CSV open failed: {exc}"

        self._csv_fh = fh
        self._csv_writer = writer
        self.csv_path = csv_path
        self._record_start_count = self.events_received
        self.is_recording = True
        return True, csv_path

    def stop_recording(self) -> int:
        """Stop CSV writing. Returns the number of events recorded this session."""
        if not self.is_recording and not self._csv_fh:
            return 0
        self.is_recording = False
        session_count = self.events_received - self._record_start_count
        if not self._loop:
            self._close_csv()
            return session_count

        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(
                self._async_close_csv(), self._loop).result(timeout=2)
        return session_count

    async def _async_close_csv(self):
        self._close_csv()

    def _close_csv(self):
        if self._csv_fh:
            with contextlib.suppress(Exception):
                self._csv_fh.flush()
                self._csv_fh.close()
        self._csv_fh = None
        self._csv_writer = None

    def _handle_message(self, raw):
        try:
            msg = json.loads(raw)
        except Exception:
            return
        if not isinstance(msg, dict):
            return
        self.events_received += 1
        recv = _utc_now()
        self.last_event_utc = recv
        if self.is_recording and self._csv_writer:
            data = msg.get("data") if isinstance(msg.get("data"), dict) else {}
            try:
                self._csv_writer.writerow([
                    recv,
                    msg.get("timestamp", ""),
                    msg.get("type", ""),
                    data.get("type", ""),
                    data.get("paradigm", ""),
                    data.get("score", ""),
                    json.dumps(msg, separators=(",", ":")),
                ])
                if self.events_received % 50 == 0:
                    with contextlib.suppress(Exception):
                        self._csv_fh.flush()
            except Exception:
                pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Main Application
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class ChillsDemoApp(ctk.CTk):

    def __init__(self):
        super().__init__()
        self.title("Chills Demo Station")
        self.geometry("1000x800")
        self.minsize(850, 680)

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        # ── state ────────────────────────────────────────────────────
        self.mode = "frisson"  # "frisson" | "suuvi"
        # Device transport: serial (primary) or ble (stub for v2 firmware).
        self.connection_mode: str = "serial"   # "serial" | "ble"
        self.device: FrissonDevice = FrissonSerial()
        self.device_port: str | None = None    # e.g. /dev/cu.usbmodem1101
        self.arctop = ArcTopRecorder()
        self.session_active = False
        self.chills_reports: list[dict] = []
        self.current_song: str | None = None
        self.current_song_file: str | None = None
        self.song_duration = 0
        self.playback_start: float | None = None
        self.playback_start_utc: str | None = None
        self.playback_start_monotonic: float | None = None
        self.trigger_timers: list[threading.Timer] = []
        # Unified device-event log: scheduled triggers, manual triggers,
        # emergency stops, command failures. Replaces the old planned/fired
        # split; consumed by the session JSON saver.
        self.device_events: list[dict] = []
        self.session_trigger_times: list[float] = []
        self.session_intensity: str = "med"
        self.verify_results: list[dict] = []   # per-check pass/fail/skip
        self.verify_skipped: bool = False
        self.track_overrides: dict[str, list[float]] = load_track_overrides()
        self.participant_number = 1
        self.use_device = True
        self.arctop_confirmed = False
        self.countdown_seconds = 10
        # EEG recording (Suuvi mode)
        self.eeg_recording_enabled = False
        self.eeg_bt_ok = False
        self.eeg_app_ok = False
        self.eeg_stream_ok = False
        self.eeg_csv_filename: str | None = None
        self.eeg_session_event_count = 0
        self.arctop_ws_url = ARCTOP_WS_URL
        self._session_update_id: str | None = None
        self._countdown_id: str | None = None
        self._key_bind_id: str | None = None

        # ── clicker calibration ──────────────────────────────────
        self.clicker_enabled = False
        self.clicker_vol_up_key: str | None = None   # keysym for volume up
        self.clicker_vol_down_key: str | None = None  # keysym for volume down

        # ── audio ────────────────────────────────────────────────────
        pygame.mixer.init(frequency=44100)

        # ── directories ──────────────────────────────────────────────
        os.makedirs(STIMULI_DIR, exist_ok=True)
        os.makedirs(SUUVI_DIR, exist_ok=True)
        os.makedirs(DATA_DIR, exist_ok=True)

        # ── layout: mode toggle bar (top) ────────────────────────────
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        top_bar = ctk.CTkFrame(self, height=48, corner_radius=0)
        top_bar.grid(row=0, column=0, sticky="ew")
        top_bar.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            top_bar, text="Chills Demo Station",
            font=("Helvetica", 14, "bold"), text_color=C_MUTED,
        ).grid(row=0, column=0, padx=16, pady=10, sticky="w")

        self._mode_toggle = ctk.CTkSegmentedButton(
            top_bar, values=["Frisson", "Suuvi"],
            command=self._on_mode_change,
            font=("Helvetica", 13, "bold"), width=200,
        )
        self._mode_toggle.set("Frisson")
        self._mode_toggle.grid(row=0, column=1, padx=16, pady=10, sticky="e")
        # Hidden until clicker setup completes — keeps the first screen focused.
        self._mode_toggle.grid_remove()

        # ── layout: page area + status bar ───────────────────────────
        # Scrollable so taller pages (e.g. Suuvi setup with EEG card) can be
        # reached without resizing the window.
        self.page_frame = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.page_frame.grid(row=1, column=0, sticky="nsew", padx=30, pady=(10, 5))

        self.status_bar = ctk.CTkFrame(self, height=44, corner_radius=0)
        self.status_bar.grid(row=2, column=0, sticky="ew")
        self._build_status_bar()

        # ── services ─────────────────────────────────────────────────
        self.device.start()
        self.device.on_disconnect_callback = lambda: self.after(0, self._poll_status)
        self.arctop.start()
        atexit.register(self._safe_shutdown)

        # ── participant counter ──────────────────────────────────────
        self.participant_number = self._next_participant_number()

        # ── mousewheel scroll for the page area (macOS-friendly) ─────
        self.bind_all("<MouseWheel>", self._on_global_mousewheel)
        self.bind_all("<Button-4>", self._on_global_mousewheel)  # Linux up
        self.bind_all("<Button-5>", self._on_global_mousewheel)  # Linux down

        # ── global emergency stop (Frisson mode only) ────────────────
        # Spacebar is deliberate: it's the most likely accidental keystroke
        # if the participant fumbles the clicker. Keeps Suuvi flow untouched.
        self.bind_all("<space>", self._on_emergency_stop_key)

        # ── go ───────────────────────────────────────────────────────
        self._show_clicker_setup()
        self._poll_status()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_global_mousewheel(self, event):
        """Forward wheel events to the scrollable page canvas.

        Skips events whose target is a Text/Listbox widget so the URL
        textbox keeps its own internal scroll behavior.
        """
        canvas = getattr(self.page_frame, "_parent_canvas", None)
        if canvas is None:
            return
        w = event.widget
        while w is not None:
            try:
                if w.winfo_class() in ("Text", "Listbox"):
                    return
                w = w.master
            except Exception:
                break
        if getattr(event, "num", 0) == 4:
            delta = -1
        elif getattr(event, "num", 0) == 5:
            delta = 1
        elif abs(event.delta) >= 120:  # Windows
            delta = int(-event.delta / 120)
        else:                          # macOS (small magnitudes)
            delta = -int(event.delta) if event.delta else 0
        if delta:
            with contextlib.suppress(Exception):
                canvas.yview_scroll(delta, "units")

    # ── safety hooks ─────────────────────────────────────────────────

    def _on_emergency_stop_key(self, event):
        """Spacebar global emergency-stop handler. Frisson mode only."""
        if self.mode != "frisson":
            return
        # Don't hijack space inside text entry widgets.
        try:
            cls = event.widget.winfo_class()
        except Exception:
            cls = ""
        if cls in ("Entry", "Text", "TEntry", "TText"):
            return
        self._emergency_stop("space-key")

    def _emergency_stop(self, source: str = "manual"):
        """Send 'off' to the device, log, and flash a brief confirmation.

        Safe to call regardless of connection state; no-ops cleanly if the
        device isn't connected.
        """
        ok, msg = (False, "device not connected")
        if self.device.is_connected:
            ok, msg = self.device.emergency_stop()
        self._log_device_event(
            event="emergency_stop", command="off",
            success=ok, message=msg, source=source)
        # Brief visual confirmation (top-of-window banner).
        with contextlib.suppress(Exception):
            self._flash_status_banner("Emergency stop sent",
                                      C_DANGER if not ok else C_WARNING)

    def _flash_status_banner(self, text: str, color: str):
        """Show a 1.5 s banner overlaid on the status bar."""
        if not hasattr(self, "status_bar") or not self.status_bar.winfo_exists():
            return
        if hasattr(self, "_banner_lbl") and self._banner_lbl.winfo_exists():
            self._banner_lbl.destroy()
        self._banner_lbl = ctk.CTkLabel(
            self.status_bar, text=text, font=("Helvetica", 12, "bold"),
            text_color=color)
        self._banner_lbl.grid(row=0, column=2, padx=8, pady=10)
        self.after(1500, lambda: self._banner_lbl.destroy()
                   if self._banner_lbl.winfo_exists() else None)

    def _log_device_event(self, **fields):
        """Append a structured event to the device_events log.

        Always includes session-relative ms, UTC, and any provided fields.
        """
        if self.playback_start_monotonic is not None:
            t_ms = int((time.monotonic() - self.playback_start_monotonic) * 1000)
        else:
            t_ms = None
        entry = {"t_ms": t_ms, "utc": _utc_now(), **fields}
        self.device_events.append(entry)
        return entry

    def _safe_shutdown(self):
        """atexit + on-close hook: stop device cleanly. Idempotent."""
        with contextlib.suppress(Exception):
            if self.device and self.device.is_connected:
                self.device.emergency_stop()
                self.device.disconnect()

    def _apply_wheel_bindings(self):
        """Bind mousewheel on every descendant of the page so trackpad scroll
        works regardless of which child the cursor is over (macOS quirk —
        CTkScrollableFrame's own Enter/Leave handler doesn't fire reliably
        when the cursor moves directly between sibling children)."""
        if not hasattr(self, "page_frame") or not self.page_frame.winfo_exists():
            return
        handler = self._on_global_mousewheel

        def _bind(w):
            try:
                cls = w.winfo_class()
            except Exception:
                return
            if cls not in ("Text", "Listbox"):
                with contextlib.suppress(Exception):
                    w.bind("<MouseWheel>", handler, add="+")
                    w.bind("<Button-4>", handler, add="+")
                    w.bind("<Button-5>", handler, add="+")
            try:
                children = w.winfo_children()
            except Exception:
                return
            for c in children:
                _bind(c)

        _bind(self.page_frame)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  Clicker Calibration (first screen on launch)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _show_clicker_setup(self):
        self._clear_page()
        self._clicker_step = 0  # 0 = waiting for down, 1 = waiting for up, 2 = done

        ctk.CTkLabel(
            self.page_frame, text="Clicker Setup",
            font=("Helvetica", 30, "bold"),
        ).pack(pady=(40, 8))
        ctk.CTkLabel(
            self.page_frame,
            text="Calibrate the clicker so the app knows which buttons to use.\n"
                 "Skip this step if you're not using a clicker.",
            font=("Helvetica", 14), text_color=C_MUTED,
            wraplength=600, justify="center",
        ).pack(pady=(0, 30))

        # Step indicators
        self._clicker_card = ctk.CTkFrame(self.page_frame, corner_radius=12)
        self._clicker_card.pack(fill="x", padx=80, pady=10)

        # Step 1: volume down
        row1 = ctk.CTkFrame(self._clicker_card, fg_color="transparent")
        row1.pack(fill="x", padx=24, pady=(16, 8))
        self._ck_check1 = ctk.CTkLabel(
            row1, text="  ", font=("Helvetica", 18), width=30)
        self._ck_check1.pack(side="left")
        self._ck_label1 = ctk.CTkLabel(
            row1, text='Step 1:  Press the "Volume Down" button on the clicker',
            font=("Helvetica", 15, "bold"))
        self._ck_label1.pack(side="left", padx=(8, 0))

        # Step 2: volume up
        row2 = ctk.CTkFrame(self._clicker_card, fg_color="transparent")
        row2.pack(fill="x", padx=24, pady=(8, 16))
        self._ck_check2 = ctk.CTkLabel(
            row2, text="  ", font=("Helvetica", 18), width=30)
        self._ck_check2.pack(side="left")
        self._ck_label2 = ctk.CTkLabel(
            row2, text='Step 2:  Press the "Volume Up" button on the clicker',
            font=("Helvetica", 15), text_color=C_MUTED)
        self._ck_label2.pack(side="left", padx=(8, 0))

        # Detected key display
        self._ck_info = ctk.CTkLabel(
            self.page_frame, text="Waiting for clicker input...",
            font=("Courier", 13), text_color=C_WARNING,
        )
        self._ck_info.pack(pady=(16, 20))

        # Buttons
        btn_row = ctk.CTkFrame(self.page_frame, fg_color="transparent")
        btn_row.pack(pady=8)

        self._ck_continue_btn = ctk.CTkButton(
            btn_row, text="Continue",
            font=("Helvetica", 17, "bold"), width=200, height=52,
            fg_color=C_SUCCESS, hover_color="#3ba882", text_color="#000",
            command=self._clicker_done, state="disabled",
        )
        self._ck_continue_btn.pack(side="left", padx=10)

        ctk.CTkButton(
            btn_row, text="Skip — No Clicker",
            font=("Helvetica", 14, "bold"), width=200, height=52,
            fg_color="#2d3a4a", hover_color="#3d4a5a",
            command=self._clicker_skip,
        ).pack(side="left", padx=10)

        # Bind for key capture
        self._ck_bind = self.bind("<KeyPress>", self._on_clicker_calibrate)

    def _on_clicker_calibrate(self, event):
        """Capture keys one at a time: first down, then up."""
        # Ignore bare modifiers
        if event.keysym in self._IGNORE_KEYS:
            return

        if self._clicker_step == 0:
            # Captured "volume down" key
            self.clicker_vol_down_key = event.keysym
            self._ck_check1.configure(text="OK", text_color=C_SUCCESS)
            self._ck_label1.configure(
                text=f'Step 1:  Volume Down  =  [{event.keysym}]',
                text_color=C_SUCCESS)
            self._ck_label2.configure(
                text='Step 2:  Press the "Volume Up" button on the clicker',
                font=("Helvetica", 15, "bold"), text_color=("gray90", "gray90"))
            self._ck_info.configure(
                text=f"Got it: [{event.keysym}]  —  now press the Volume Up button",
                text_color=C_SUCCESS)
            self._clicker_step = 1

        elif self._clicker_step == 1:
            if event.keysym == self.clicker_vol_down_key:
                # Same key pressed again — ignore
                self._ck_info.configure(
                    text=f"That's the same key [{event.keysym}] — press the OTHER button",
                    text_color=C_WARNING)
                return
            # Captured "volume up" key
            self.clicker_vol_up_key = event.keysym
            self._ck_check2.configure(text="OK", text_color=C_SUCCESS)
            self._ck_label2.configure(
                text=f'Step 2:  Volume Up  =  [{event.keysym}]',
                text_color=C_SUCCESS)
            self._ck_info.configure(
                text=f"Clicker calibrated:  Down=[{self.clicker_vol_down_key}]  "
                     f"Up=[{self.clicker_vol_up_key}]",
                text_color=C_SUCCESS)
            self._clicker_step = 2
            self.clicker_enabled = True

            # Unbind and enable continue
            self.unbind("<KeyPress>", self._ck_bind)
            self._ck_bind = None
            self._ck_continue_btn.configure(state="normal")

    def _clicker_done(self):
        if self._ck_bind:
            self.unbind("<KeyPress>", self._ck_bind)
            self._ck_bind = None
        self._mode_toggle.grid()
        self._show_home()

    def _clicker_skip(self):
        if hasattr(self, "_ck_bind") and self._ck_bind:
            self.unbind("<KeyPress>", self._ck_bind)
            self._ck_bind = None
        self.clicker_enabled = False
        self.clicker_vol_down_key = None
        self.clicker_vol_up_key = None
        self._mode_toggle.grid()
        self._show_home()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  Mode toggle
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _on_mode_change(self, value: str):
        if self.session_active:
            # Don't allow mode switch during a session
            self._mode_toggle.set(self.mode.capitalize())
            return
        self.mode = value.lower()
        self._show_home()

    def _show_home(self):
        """Navigate to the first page for the current mode."""
        if self.mode == "suuvi":
            self.show_suuvi_setup_page()
        else:
            self.show_connection_page()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  Status Bar
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _build_status_bar(self):
        self.status_bar.grid_columnconfigure(2, weight=1)

        self.lbl_mode = ctk.CTkLabel(
            self.status_bar, text="Mode: Frisson",
            font=("Helvetica", 12), text_color=C_MUTED)
        self.lbl_mode.grid(row=0, column=0, padx=(16, 20), pady=10)

        self.lbl_ble = ctk.CTkLabel(
            self.status_bar, text="Device: --",
            font=("Helvetica", 12))
        self.lbl_ble.grid(row=0, column=1, padx=(0, 20), pady=10)

        self.lbl_sessions = ctk.CTkLabel(
            self.status_bar, text="Sessions saved: 0",
            font=("Helvetica", 12), text_color=C_MUTED)
        self.lbl_sessions.grid(row=0, column=3, padx=(0, 16), pady=10)

    def _poll_status(self):
        if self.mode == "suuvi":
            self.lbl_mode.configure(text="Mode: Suuvi", text_color=C_SUUVI)
            self.lbl_ble.configure(text="No device needed", text_color=C_MUTED)
        else:
            self.lbl_mode.configure(text="Mode: Frisson", text_color=C_PRIMARY)
            mode_label = "USB" if self.connection_mode == "serial" else "BLE"
            if self.device.is_connected:
                detail = self.device.device_name
                self.lbl_ble.configure(
                    text=f"Device ({mode_label}): {detail}", text_color=C_SUCCESS)
            else:
                self.lbl_ble.configure(
                    text=f"Device ({mode_label}): not connected", text_color=C_DANGER)

        try:
            n = len([f for f in os.listdir(DATA_DIR) if f.endswith(".json")])
        except OSError:
            n = 0
        self.lbl_sessions.configure(text=f"Sessions saved: {n}")

        self.after(2000, self._poll_status)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  Helpers
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _clear_page(self):
        for w in self.page_frame.winfo_children():
            w.destroy()
        # After the page builder finishes adding new children, bind wheel
        # events on each one so trackpad scroll works on macOS.
        self.after_idle(self._apply_wheel_bindings)

    def _next_participant_number(self) -> int:
        try:
            nums = []
            for f in os.listdir(DATA_DIR):
                if not f.endswith(".json"):
                    continue
                parts = f.split("_P")
                if len(parts) >= 2:
                    with contextlib.suppress(ValueError):
                        nums.append(int(parts[1].split("_")[0]))
            return max(nums) + 1 if nums else 1
        except OSError:
            return 1

    @staticmethod
    def _fmt(seconds: float) -> str:
        s = max(0, int(seconds))
        return f"{s // 60}:{s % 60:02d}"

    def _worker(self, fn, on_done):
        def _run():
            result = fn()
            self.after(0, lambda: on_done(result))
        threading.Thread(target=_run, daemon=True).start()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  FRISSON — PAGE 1: Device Connection
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def show_connection_page(self):
        self._clear_page()

        ctk.CTkLabel(self.page_frame, text="Frisson Device Connection",
                     font=("Helvetica", 30, "bold")).pack(pady=(20, 14))

        # ── Mode toggle ─────────────────────────────────────────────
        mode_card = ctk.CTkFrame(self.page_frame, corner_radius=12)
        mode_card.pack(fill="x", padx=50, pady=(0, 10))
        ctk.CTkLabel(mode_card, text="Connection mode",
                     font=("Helvetica", 13, "bold"),
                     text_color=C_MUTED).pack(pady=(12, 4))
        self._conn_mode_btn = ctk.CTkSegmentedButton(
            mode_card, values=["USB Serial (new device)", "Bluetooth (coming soon)"],
            command=self._on_conn_mode_change,
            font=("Helvetica", 13, "bold"), width=440)
        self._conn_mode_btn.set(
            "USB Serial (new device)" if self.connection_mode == "serial"
            else "Bluetooth (coming soon)")
        self._conn_mode_btn.pack(pady=(2, 14))

        # ── Serial port row (visible only in serial mode) ───────────
        self._serial_card = ctk.CTkFrame(self.page_frame, corner_radius=12)
        self._serial_card.pack(fill="x", padx=50, pady=(0, 10))
        ctk.CTkLabel(self._serial_card, text="Serial port",
                     font=("Helvetica", 13, "bold"),
                     text_color=C_MUTED).pack(pady=(12, 4))
        port_row = ctk.CTkFrame(self._serial_card, fg_color="transparent")
        port_row.pack(fill="x", padx=14, pady=(0, 12))
        self._port_var = ctk.StringVar(value="")
        self._port_dropdown = ctk.CTkOptionMenu(
            port_row, variable=self._port_var, values=[""], width=460,
            font=("Helvetica", 12))
        self._port_dropdown.pack(side="left", padx=(0, 8))
        ctk.CTkButton(port_row, text="Refresh", width=90, height=28,
                      font=("Helvetica", 12),
                      command=self._refresh_serial_ports).pack(side="left")

        # ── BLE info card (visible only in BLE mode) ────────────────
        self._ble_info_card = ctk.CTkFrame(self.page_frame, corner_radius=12)
        ctk.CTkLabel(
            self._ble_info_card,
            text="BLE not yet supported on the new device firmware — use USB Serial for now.",
            font=("Helvetica", 13), text_color=C_WARNING,
            wraplength=600, justify="center"
        ).pack(padx=20, pady=18)

        # ── status + buttons ────────────────────────────────────────
        self._conn_status = ctk.CTkLabel(
            self.page_frame, text="Not connected",
            font=("Courier", 12), text_color=C_MUTED,
            wraplength=700, justify="left", anchor="w")
        self._conn_status.pack(pady=(8, 8), fill="x", padx=50)

        btn_row = ctk.CTkFrame(self.page_frame, fg_color="transparent")
        btn_row.pack(pady=8)
        self._connect_btn = ctk.CTkButton(
            btn_row, text="Connect", font=("Helvetica", 15, "bold"),
            width=160, height=44, fg_color=C_ACCENT, hover_color="#1a4a7a",
            command=self._on_connect)
        self._connect_btn.pack(side="left", padx=8)
        ctk.CTkButton(
            btn_row, text="Disconnect", font=("Helvetica", 15, "bold"),
            width=140, height=44, fg_color="#2d3a4a", hover_color="#3d4a5a",
            command=self._on_disconnect).pack(side="left", padx=8)

        self._skip_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(self.page_frame, text="Run without Frisson device",
                        variable=self._skip_var, font=("Helvetica", 13),
                        text_color=C_MUTED).pack(pady=(12, 16))

        ctk.CTkButton(
            self.page_frame, text="Continue to Verify Device",
            font=("Helvetica", 17, "bold"), width=300, height=52,
            fg_color=C_SUCCESS, hover_color="#3ba882", text_color="#000",
            command=self._go_to_verify).pack(pady=5)

        # initial port enumeration + UI sync
        self._refresh_serial_ports()
        self._on_conn_mode_change(self._conn_mode_btn.get())

        if self.device.is_connected:
            self._conn_status.configure(
                text=f"Connected to {self.device.device_name}", text_color=C_SUCCESS)

    @staticmethod
    def _port_label(p: dict) -> str:
        desc = p["description"] or "?"
        if p.get("is_debug"):
            desc += "  [debug — usually NOT what you want]"
        return f"{p['device']} — {desc}"

    def _refresh_serial_ports(self):
        ports = list_serial_ports()
        if not ports:
            label = "(no serial ports detected)"
            self._port_dropdown.configure(values=[label])
            self._port_var.set(label)
            self._available_ports = []
            return
        labels = [self._port_label(p) for p in ports]
        self._available_ports = ports
        self._port_dropdown.configure(values=labels)
        # Auto-pick first Arduino/ESP32 port if any (skipping the debug CDC).
        auto = auto_pick_arduino_port(ports)
        idx = 0
        if auto:
            for i, p in enumerate(ports):
                if p["device"] == auto:
                    idx = i
                    break
        self._port_var.set(labels[idx])

    def _selected_port_device(self) -> str | None:
        if not getattr(self, "_available_ports", None):
            return None
        label = self._port_var.get()
        for p in self._available_ports:
            if self._port_label(p) == label:
                return p["device"]
        return None

    def _on_conn_mode_change(self, value: str):
        is_serial = value.startswith("USB")
        self.connection_mode = "serial" if is_serial else "ble"
        # Swap the right card in/out.
        if is_serial:
            self._serial_card.pack(fill="x", padx=50, pady=(0, 10),
                                   before=self._conn_status)
            self._ble_info_card.pack_forget()
            self._connect_btn.configure(state="normal")
        else:
            self._ble_info_card.pack(fill="x", padx=50, pady=(0, 10),
                                     before=self._conn_status)
            self._serial_card.pack_forget()
            self._connect_btn.configure(state="disabled")
        self._poll_status()

    def _on_connect(self):
        if self.connection_mode != "serial":
            return  # button is disabled, defensive
        port = self._selected_port_device()
        if not port:
            self._conn_status.configure(
                text="Pick a serial port first.", text_color=C_DANGER)
            return
        # Recreate the device if the port changed (cheap; loop is reused).
        if not isinstance(self.device, FrissonSerial) or self.device.port != port:
            with contextlib.suppress(Exception):
                self.device.stop()
            self.device = FrissonSerial(port=port)
            self.device.on_disconnect_callback = lambda: self.after(0, self._poll_status)
            self.device.start()
        self.device_port = port
        self._connect_btn.configure(text="Connecting...", fg_color=C_WARNING)
        self._conn_status.configure(
            text=f"Opening {port} (Arduino reset takes ~{ARDUINO_BOOT_DELAY:.0f}s)…",
            text_color=C_WARNING)
        self._worker(self.device.connect, self._connect_done)

    def _connect_done(self, result):
        ok, msg = result
        if ok:
            self._connect_btn.configure(text="Connected", fg_color=C_SUCCESS)
            self._conn_status.configure(
                text=f"Connected to {msg}", text_color=C_SUCCESS)
        else:
            self._connect_btn.configure(text="Retry", fg_color=C_DANGER)
            self._conn_status.configure(text=msg, text_color=C_DANGER)
        self.after(2000, lambda: self._connect_btn.configure(
            text="Connect", fg_color=C_ACCENT))

    def _on_disconnect(self):
        self._worker(self.device.disconnect, lambda _: self._conn_status.configure(
            text="Disconnected", text_color=C_MUTED))

    def _go_to_verify(self):
        self.use_device = not self._skip_var.get()
        if not self.use_device:
            # Skip both verify and the device entirely.
            self.verify_skipped = True
            self.verify_results = []
            self.show_frisson_setup_page()
            return
        self.show_verify_device_page()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  FRISSON — PAGE 1b: Verify Device (replaces legacy Test Trigger)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    # Cooldowns (seconds) per check type. These are SAFETY values — we
    # damaged Peltiers earlier in this project by firing too rapidly.
    # Don't reduce without a hardware reason.
    _COOLDOWN_SINGLE = 30
    _COOLDOWN_WAVE = 60

    # Each check that has fireable buttons exposes a Med / Max intensity
    # picker. (Verification doesn't bother with low/high — the demo only
    # needs to confirm the two production-relevant levels work.)
    _VERIFY_CHECKS = [
        {"id": "channels", "title": "1. Single channels",
         "kind": "single", "intensities": ("med", "max"), "default": "med",
         "instr": "Pick an intensity, then fire each Peltier and touch its "
                  "labeled (cold) side. Confirm each feels distinctly cool "
                  "and the level is uniform across channels. 30 s cooldown "
                  "between fires for heatsink recovery."},
        {"id": "wave",     "title": "2. Wave pattern",
         "kind": "wave", "intensities": ("med", "max"), "default": "med",
         "instr": "Pick an intensity, then place three fingers across all "
                  "three Peltiers and confirm the rolling sensation "
                  "propagates correctly. 60 s cooldown after firing."},
        {"id": "estop",    "title": "3. Emergency stop test",
         "kind": "estop", "intensities": ("med",), "default": "med",
         "instr": "Fires wave-med, then sends 'off' after 500 ms. "
                  "Confirm the wave is interrupted before completing."},
    ]

    def show_verify_device_page(self):
        self._clear_page()
        # Reset state for a fresh run.
        self._verify_state = {c["id"]: "pending" for c in self._VERIFY_CHECKS}
        self._verify_cooldown_until = 0.0
        self._verify_buttons: list = []  # buttons disabled during cooldown
        self._verify_intensity_vars = {}  # rebuilt by _build_verify_check_card

        ctk.CTkLabel(self.page_frame, text="Verify Device",
                     font=("Helvetica", 30, "bold")).pack(pady=(20, 6))
        ctk.CTkLabel(self.page_frame,
                     text="Run each check, mark Pass/Fail/Skip, then proceed.",
                     font=("Helvetica", 13), text_color=C_MUTED
                     ).pack(pady=(0, 8))

        # Live cooldown banner (always present, hidden when not cooling).
        self._verify_cd_lbl = ctk.CTkLabel(
            self.page_frame, text="", font=("Helvetica", 13, "bold"),
            text_color=C_WARNING)
        self._verify_cd_lbl.pack(pady=(0, 8))

        # Build a card per check.
        for check in self._VERIFY_CHECKS:
            self._build_verify_check_card(check)

        # Cumulative status + proceed.
        self._verify_summary_lbl = ctk.CTkLabel(
            self.page_frame, text="", font=("Helvetica", 13, "bold"))
        self._verify_summary_lbl.pack(pady=(8, 4))

        proceed = ctk.CTkButton(
            self.page_frame, text="Proceed to Session Setup",
            font=("Helvetica", 16, "bold"), width=300, height=48,
            fg_color=C_SUCCESS, hover_color="#3ba882", text_color="#000",
            command=self._verify_proceed)
        proceed.pack(pady=(8, 4))

        skip = ctk.CTkButton(
            self.page_frame, text="Skip Verification",
            font=("Helvetica", 12), width=160, height=28,
            fg_color="transparent", hover_color="#2a2a3e", text_color=C_MUTED,
            command=self._verify_skip)
        skip.pack(pady=(2, 16))

        self._tick_verify_cooldown()
        self._update_verify_summary()

    def _build_verify_check_card(self, check: dict):
        card = ctk.CTkFrame(self.page_frame, corner_radius=10)
        card.pack(fill="x", padx=30, pady=6)
        ctk.CTkLabel(card, text=check["title"], font=("Helvetica", 15, "bold"),
                     anchor="w").pack(fill="x", padx=14, pady=(10, 2))
        ctk.CTkLabel(card, text=check["instr"], font=("Helvetica", 12),
                     text_color=C_MUTED, wraplength=720, justify="left",
                     anchor="w").pack(fill="x", padx=14, pady=(0, 6))

        # Per-card intensity picker — only when the check has more than one
        # option. Stored on self._verify_intensity_vars[check_id].
        intensities = check.get("intensities", (check.get("default", "med"),))
        var = ctk.StringVar(value=check.get("default", intensities[0]))
        if not hasattr(self, "_verify_intensity_vars"):
            self._verify_intensity_vars = {}
        self._verify_intensity_vars[check["id"]] = var
        if len(intensities) > 1:
            int_row = ctk.CTkFrame(card, fg_color="transparent")
            int_row.pack(fill="x", padx=14, pady=(0, 6))
            ctk.CTkLabel(int_row, text="Intensity:",
                         font=("Helvetica", 12), text_color=C_MUTED
                         ).pack(side="left", padx=(0, 8))
            ctk.CTkSegmentedButton(
                int_row, values=list(intensities), variable=var,
                font=("Helvetica", 12), width=160
            ).pack(side="left")

        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=(2, 10))

        # Fire buttons depend on the kind.
        if check["kind"] == "single":
            for ch in (1, 2, 3):
                btn = ctk.CTkButton(
                    row, text=f"Fire P{ch}", width=110, height=32,
                    font=("Helvetica", 13, "bold"),
                    fg_color=C_ACCENT, hover_color="#1a4a7a")
                btn.configure(command=lambda c=ch, ck=check, b=btn:
                              self._fire_verify(ck, b, channel=c))
                btn.pack(side="left", padx=4)
                self._verify_buttons.append(btn)
        elif check["kind"] == "wave":
            btn = ctk.CTkButton(
                row, text="Fire Wave", width=160, height=32,
                font=("Helvetica", 13, "bold"),
                fg_color=C_ACCENT, hover_color="#1a4a7a")
            btn.configure(command=lambda ck=check, b=btn: self._fire_verify(ck, b))
            btn.pack(side="left", padx=4)
            self._verify_buttons.append(btn)
        elif check["kind"] == "estop":
            btn = ctk.CTkButton(
                row, text="Test Emergency Stop", width=200, height=32,
                font=("Helvetica", 13, "bold"),
                fg_color=C_DANGER, hover_color="#c0392b")
            btn.configure(command=lambda ck=check, b=btn:
                          self._fire_verify_estop(ck, b))
            btn.pack(side="left", padx=4)
            self._verify_buttons.append(btn)

        # Pass / Fail / Skip toggles.
        result_var = ctk.StringVar(value="pending")
        seg = ctk.CTkSegmentedButton(
            row, values=["pending", "pass", "fail", "skip"],
            variable=result_var, width=300, font=("Helvetica", 11),
            command=lambda v, cid=check["id"]: self._on_verify_mark(cid, v))
        seg.pack(side="right", padx=4)

    def _verify_intensity_for(self, check: dict) -> str:
        var = self._verify_intensity_vars.get(check["id"])
        if var is None:
            return check.get("default", "med")
        v = var.get()
        return v if v in INTENSITY_LEVELS else check.get("default", "med")

    def _fire_verify(self, check: dict, btn, channel: int | None = None):
        if not self._verify_can_fire():
            return
        intensity = self._verify_intensity_for(check)
        if channel is not None:
            ok, msg = self._do_fire(
                channel=channel, intensity=intensity, source="verify")
        else:
            ok, msg = self._do_fire(
                pattern="wave", intensity=intensity, source="verify")
        cooldown = (self._COOLDOWN_WAVE if check["kind"] == "wave"
                    else self._COOLDOWN_SINGLE)
        self._begin_verify_cooldown(cooldown)
        if not ok:
            self._verify_cd_lbl.configure(
                text=f"Command failed: {msg}", text_color=C_DANGER)

    def _fire_verify_estop(self, check: dict, btn):
        if not self._verify_can_fire():
            return
        intensity = self._verify_intensity_for(check)
        ok, _ = self._do_fire(
            pattern="wave", intensity=intensity, source="verify")
        if not ok:
            return
        self.after(500, lambda: self._emergency_stop(source="verify-test"))
        # Short cooldown — wave was interrupted, but still let things settle.
        self._begin_verify_cooldown(self._COOLDOWN_SINGLE)

    def _verify_can_fire(self) -> bool:
        if time.monotonic() < self._verify_cooldown_until:
            return False
        if self.connection_mode != "serial" or not self.device.is_connected:
            self._verify_cd_lbl.configure(
                text="Device not connected.", text_color=C_DANGER)
            return False
        return True

    def _begin_verify_cooldown(self, seconds: float):
        self._verify_cooldown_until = time.monotonic() + seconds
        for b in self._verify_buttons:
            b.configure(state="disabled")
        self._tick_verify_cooldown()

    def _tick_verify_cooldown(self):
        remaining = max(0.0, self._verify_cooldown_until - time.monotonic())
        if remaining > 0:
            self._verify_cd_lbl.configure(
                text=f"Cooldown — {int(remaining)+1} s before next fire",
                text_color=C_WARNING)
            self.after(250, self._tick_verify_cooldown)
        else:
            self._verify_cd_lbl.configure(text="", text_color=C_WARNING)
            for b in self._verify_buttons:
                with contextlib.suppress(Exception):
                    b.configure(state="normal")

    def _on_verify_mark(self, check_id: str, value: str):
        self._verify_state[check_id] = value
        self._update_verify_summary()

    def _update_verify_summary(self):
        states = list(self._verify_state.values())
        n_total = len(states)
        n_pass = states.count("pass")
        n_fail = states.count("fail")
        n_skip = states.count("skip")
        n_pending = states.count("pending")
        if n_pass == n_total:
            text = "All checks passed — ready for session"
            color = C_SUCCESS
        elif n_fail or (n_skip and n_pending == 0):
            text = (f"{n_pass} pass / {n_fail} fail / {n_skip} skip — "
                    "not all checks passed")
            color = C_WARNING
        else:
            text = (f"{n_pass} pass / {n_fail} fail / {n_skip} skip / "
                    f"{n_pending} pending")
            color = C_MUTED
        self._verify_summary_lbl.configure(text=text, text_color=color)

    def _verify_proceed(self):
        # Snapshot results for the session JSON.
        self.verify_results = [
            {"check": cid, "result": self._verify_state.get(cid, "pending"),
             "utc": _utc_now()}
            for cid in (c["id"] for c in self._VERIFY_CHECKS)]
        self.verify_skipped = False
        self.show_frisson_setup_page()

    def _verify_skip(self):
        self.verify_skipped = True
        self.verify_results = []
        self.show_frisson_setup_page()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  FRISSON — Device fire helpers (used by verify + session)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _do_fire(self, *, channel: int | None = None,
                 pattern: str = "single", intensity: str = "med",
                 source: str = "manual") -> tuple[bool, str]:
        """Fire a command on the device. Logs into device_events.

        - channel: 1/2/3 for single, ignored otherwise
        - pattern: "single" | "wave" | "seq" | "sim"
        - intensity: low/med/high/max
        - source: "scheduled" | "manual" | "verify" | etc. (logged)
        """
        if intensity not in INTENSITY_LEVELS:
            return False, f"bad intensity {intensity}"
        if not self.device.is_connected:
            self._log_device_event(
                event=("scheduled_trigger" if source == "scheduled"
                       else "manual_trigger"),
                command=None, channel=channel, pattern=pattern,
                intensity=intensity, success=False,
                message="device not connected", source=source)
            return False, "device not connected"
        if pattern == "single":
            if channel not in (1, 2, 3):
                return False, "bad channel"
            cmd = f"p{channel}-{intensity}"
            ok, msg = self.device.fire_channel(channel, intensity)
        elif pattern == "wave":
            cmd = f"wave-{intensity}"
            ok, msg = self.device.fire_wave(intensity)
        elif pattern == "seq":
            cmd = f"seq-{intensity}"
            ok, msg = self.device.fire_seq(intensity)
        elif pattern == "sim":
            cmd = f"sim-{intensity}"
            ok, msg = self.device.fire_sim(intensity)
        else:
            return False, f"unknown pattern {pattern}"
        self._log_device_event(
            event=("scheduled_trigger" if source == "scheduled"
                   else "manual_trigger"),
            command=cmd, channel=channel if pattern == "single" else None,
            pattern=pattern, intensity=intensity, success=ok,
            message=msg, source=source)
        return ok, msg

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  FRISSON — PAGE 2: Session Setup
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def show_frisson_setup_page(self):
        self._clear_page()

        ctk.CTkLabel(self.page_frame, text="Frisson Session Setup",
                     font=("Helvetica", 30, "bold")).pack(pady=(20, 18))

        p_row = ctk.CTkFrame(self.page_frame, fg_color="transparent")
        p_row.pack(pady=4)
        ctk.CTkLabel(p_row, text="Participant #:",
                     font=("Helvetica", 16)).pack(side="left", padx=(0, 10))
        self._p_entry = ctk.CTkEntry(p_row, width=80, font=("Helvetica", 16),
                                     justify="center")
        self._p_entry.insert(0, str(self.participant_number))
        self._p_entry.pack(side="left")

        # ── stimulus picker ─────────────────────────────────────────
        ctk.CTkLabel(self.page_frame, text="Select Stimulus:",
                     font=("Helvetica", 16), text_color=C_MUTED).pack(pady=(20, 6))
        self._song_var = ctk.StringVar(value="Random")
        sf = ctk.CTkFrame(self.page_frame, fg_color="transparent")
        sf.pack()
        for opt in ["Random"] + list(SONGS.keys()):
            ctk.CTkRadioButton(
                sf, text=opt, variable=self._song_var, value=opt,
                font=("Helvetica", 14),
                command=self._on_song_change).pack(anchor="w", padx=80, pady=3)

        # ── trigger times (editable) ────────────────────────────────
        trig_card = ctk.CTkFrame(self.page_frame, corner_radius=10)
        trig_card.pack(fill="x", padx=40, pady=(14, 6))
        ctk.CTkLabel(trig_card, text="Trigger times (seconds, comma-separated)",
                     font=("Helvetica", 13, "bold")
                     ).pack(pady=(10, 2), padx=14, anchor="w")
        ctk.CTkLabel(trig_card,
                     text="Auto-filled with the song's defaults. Edit to override; "
                          "edits persist per-track across launches.",
                     font=("Helvetica", 11), text_color=C_MUTED,
                     wraplength=680, justify="left",
                     anchor="w").pack(padx=14, pady=(0, 4), fill="x")
        trig_row = ctk.CTkFrame(trig_card, fg_color="transparent")
        trig_row.pack(fill="x", padx=14, pady=(2, 4))
        self._triggers_entry = ctk.CTkEntry(
            trig_row, font=("Courier", 14), width=420, justify="left")
        self._triggers_entry.pack(side="left", padx=(0, 8))
        ctk.CTkButton(trig_row, text="Reset to defaults", width=140, height=28,
                      font=("Helvetica", 12),
                      command=self._reset_triggers).pack(side="left")
        self._triggers_err = ctk.CTkLabel(
            trig_card, text="", font=("Helvetica", 11), text_color=C_DANGER,
            anchor="w", justify="left")
        self._triggers_err.pack(pady=(0, 10), padx=14, fill="x")

        # ── intensity selector ──────────────────────────────────────
        int_row = ctk.CTkFrame(self.page_frame, fg_color="transparent")
        int_row.pack(pady=(8, 4))
        ctk.CTkLabel(int_row, text="Trigger intensity:",
                     font=("Helvetica", 14)).pack(side="left", padx=(0, 8))
        self._intensity_session_var = ctk.StringVar(value=self.session_intensity)
        ctk.CTkOptionMenu(int_row, variable=self._intensity_session_var,
                          values=list(INTENSITY_LEVELS), width=110,
                          font=("Helvetica", 13)).pack(side="left")
        ctk.CTkLabel(int_row,
                     text="  (used for scheduled waves and manual fires)",
                     font=("Helvetica", 11), text_color=C_MUTED
                     ).pack(side="left", padx=(8, 0))

        # ── audio file presence ─────────────────────────────────────
        self._audio_lbl = ctk.CTkLabel(self.page_frame, text="",
                                       font=("Helvetica", 12))
        self._audio_lbl.pack(pady=(10, 0))
        missing = [c["file"] for c in SONGS.values()
                   if not os.path.exists(os.path.join(STIMULI_DIR, c["file"]))]
        if missing:
            self._audio_lbl.configure(
                text=f"Missing: {', '.join(missing)}", text_color=C_WARNING)
        else:
            self._audio_lbl.configure(
                text="All audio files found", text_color=C_SUCCESS)

        if self.use_device and not self.device.is_connected:
            ctk.CTkLabel(self.page_frame,
                         text="Device not connected — go back or check 'run without'",
                         font=("Helvetica", 12), text_color=C_WARNING
                         ).pack(pady=(6, 0))

        ctk.CTkButton(self.page_frame, text="Start Session",
                      font=("Helvetica", 18, "bold"), width=260, height=55,
                      fg_color=C_PRIMARY, hover_color="#c93a52",
                      command=self._prepare_frisson_session).pack(pady=22)
        ctk.CTkButton(self.page_frame, text="Back to Device Setup",
                      font=("Helvetica", 13), width=200, height=34,
                      fg_color="transparent", hover_color="#2a2a3e",
                      text_color=C_MUTED,
                      command=self.show_connection_page).pack()

        # initial fill of triggers
        self._on_song_change()

    def _current_song_name(self) -> str:
        choice = self._song_var.get()
        if choice == "Random":
            return random.choice(list(SONGS.keys()))
        return choice

    def _on_song_change(self):
        # On Random we don't lock the song until Start, but we do show
        # something meaningful: defaults of the first song so the user
        # can still edit without surprise.
        choice = self._song_var.get()
        song = (choice if choice != "Random"
                else next(iter(SONGS.keys())))
        self._fill_triggers_for(song)

    def _fill_triggers_for(self, song: str):
        triggers = self.track_overrides.get(song)
        if triggers is None:
            triggers = list(SONGS.get(song, {}).get("triggers", []))
        text = ", ".join(_fmt_num(t) for t in triggers)
        self._triggers_entry.delete(0, "end")
        self._triggers_entry.insert(0, text)
        self._triggers_err.configure(text="")

    def _reset_triggers(self):
        choice = self._song_var.get()
        song = (choice if choice != "Random"
                else next(iter(SONGS.keys())))
        # Drop the override and refill.
        self.track_overrides.pop(song, None)
        save_track_overrides(self.track_overrides)
        defaults = list(SONGS.get(song, {}).get("triggers", []))
        text = ", ".join(_fmt_num(t) for t in defaults)
        self._triggers_entry.delete(0, "end")
        self._triggers_entry.insert(0, text)
        self._triggers_err.configure(text="")

    def _prepare_frisson_session(self):
        try:
            self.participant_number = int(self._p_entry.get())
        except ValueError:
            self._p_entry.configure(border_color=C_DANGER)
            return
        choice = self._song_var.get()
        self.current_song = (random.choice(list(SONGS.keys()))
                             if choice == "Random" else choice)
        cfg = SONGS[self.current_song]
        self.current_song_file = cfg["file"]
        path = os.path.join(STIMULI_DIR, cfg["file"])
        if not os.path.exists(path):
            self._audio_lbl.configure(
                text=f"Not found: {cfg['file']}", text_color=C_DANGER)
            return
        self.song_duration = cfg["duration_est"]
        # Parse + validate trigger times against the track duration.
        times, err = parse_trigger_times(
            self._triggers_entry.get(), max_duration=cfg["duration_est"])
        if times is None:
            self._triggers_err.configure(text=err)
            return
        self.session_trigger_times = times
        # Persist only if they differ from defaults.
        defaults = list(cfg.get("triggers", []))
        if times != defaults:
            self.track_overrides[self.current_song] = times
            save_track_overrides(self.track_overrides)
        self.session_intensity = self._intensity_session_var.get()
        if self.session_intensity not in INTENSITY_LEVELS:
            self.session_intensity = "med"
        self._run_volume_check()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  SUUVI — Setup Page (replaces connection + session setup)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def show_suuvi_setup_page(self):
        self._clear_page()

        ctk.CTkLabel(self.page_frame, text="Suuvi Session Setup",
                     font=("Helvetica", 30, "bold"),
                     text_color=C_SUUVI).pack(pady=(25, 20))

        # ── ArcTop EEG card ──────────────────────────────────────────
        arctop_card = ctk.CTkFrame(self.page_frame, corner_radius=12)
        arctop_card.pack(fill="x", padx=50, pady=(5, 10))

        ctk.CTkLabel(arctop_card, text="ArcTop EEG Recording",
                     font=("Helvetica", 16, "bold")).pack(pady=(14, 2))
        ctk.CTkLabel(arctop_card,
                     text=f"When enabled, every event from the ArcTop WebSocket "
                          f"is streamed to a CSV alongside the session JSON.",
                     font=("Helvetica", 12), text_color=C_MUTED,
                     wraplength=620, justify="center").pack(padx=20, pady=(0, 6))

        self._eeg_enable_var = ctk.BooleanVar(value=self.eeg_recording_enabled)
        ctk.CTkSwitch(arctop_card,
                      text="Collect EEG data during this session",
                      variable=self._eeg_enable_var,
                      font=("Helvetica", 14, "bold"),
                      command=self._on_eeg_toggle,
                      ).pack(pady=(4, 8))

        # verification rows + URL entry (only populated when toggle is on)
        self._eeg_check_frame = ctk.CTkFrame(arctop_card, fg_color="transparent")
        self._eeg_check_frame.pack(fill="x", padx=18, pady=(0, 14))
        self._build_eeg_check_rows()

        # WebSocket URL entry — sits inside the same expandable section
        self._eeg_url_frame = ctk.CTkFrame(arctop_card, fg_color="transparent")
        ctk.CTkLabel(self._eeg_url_frame, text="WebSocket URL:",
                     font=("Helvetica", 12), text_color=C_MUTED,
                     anchor="w").pack(fill="x", padx=8, pady=(2, 2))
        self._eeg_url_entry = ctk.CTkTextbox(
            self._eeg_url_frame, height=60, font=("Helvetica", 11),
            wrap="char")
        self._eeg_url_entry.pack(fill="x", padx=8, pady=(0, 4))
        self._eeg_url_entry.insert("1.0", self.arctop_ws_url)
        # invalidate stream test when URL is edited
        self._eeg_url_entry.bind("<KeyRelease>", self._on_url_edited)
        ctk.CTkLabel(self._eeg_url_frame,
                     text="Edit before testing if Arctop's URL/token has changed.",
                     font=("Helvetica", 10), text_color=C_MUTED,
                     anchor="w").pack(fill="x", padx=8, pady=(0, 4))

        self._refresh_eeg_check_visibility()

        # ── Participant # ────────────────────────────────────────────
        p_row = ctk.CTkFrame(self.page_frame, fg_color="transparent")
        p_row.pack(pady=(14, 8))
        ctk.CTkLabel(p_row, text="Participant #:",
                     font=("Helvetica", 16)).pack(side="left", padx=(0, 10))
        self._p_entry = ctk.CTkEntry(p_row, width=80, font=("Helvetica", 16), justify="center")
        self._p_entry.insert(0, str(self.participant_number))
        self._p_entry.pack(side="left")

        # ── Track selection ──────────────────────────────────────────
        ctk.CTkLabel(self.page_frame, text="Select Track:",
                     font=("Helvetica", 16), text_color=C_MUTED).pack(pady=(18, 8))

        tracks = _scan_suuvi_tracks()
        self._suuvi_track_var = ctk.StringVar(value=tracks[0] if tracks else "")

        if not tracks:
            ctk.CTkLabel(self.page_frame,
                         text="No audio files found in Stimuli/Suuvi/\n"
                              "Drop your tracks (.mp3, .wav, .ogg, .flac) into that folder.",
                         font=("Helvetica", 14), text_color=C_DANGER,
                         justify="center").pack(pady=10)
        else:
            track_frame = ctk.CTkFrame(self.page_frame, fg_color="transparent")
            track_frame.pack()
            for t in tracks:
                ctk.CTkRadioButton(track_frame, text=t,
                                   variable=self._suuvi_track_var, value=t,
                                   font=("Helvetica", 14)).pack(anchor="w", padx=50, pady=4)

        # ── Countdown delay ──────────────────────────────────────────
        delay_row = ctk.CTkFrame(self.page_frame, fg_color="transparent")
        delay_row.pack(pady=(18, 4))
        ctk.CTkLabel(delay_row, text="Pre-play countdown (seconds):",
                     font=("Helvetica", 14)).pack(side="left", padx=(0, 10))
        self._delay_entry = ctk.CTkEntry(delay_row, width=60, font=("Helvetica", 14), justify="center")
        self._delay_entry.insert(0, str(self.countdown_seconds))
        self._delay_entry.pack(side="left")

        ctk.CTkLabel(self.page_frame,
                     text="The track will start automatically after the countdown. "
                          "Tell the participant to get comfortable and put on headphones.",
                     font=("Helvetica", 12), text_color=C_MUTED,
                     wraplength=550, justify="center").pack(pady=(4, 16))

        # ── Start button ─────────────────────────────────────────────
        self._suuvi_status = ctk.CTkLabel(self.page_frame, text="",
                                          font=("Helvetica", 12))
        self._suuvi_status.pack(pady=(0, 6))

        ctk.CTkButton(self.page_frame, text="Start Session",
                      font=("Helvetica", 18, "bold"), width=260, height=55,
                      fg_color=C_SUUVI, hover_color="#5a4bd6",
                      command=self._prepare_suuvi_session).pack(pady=5)

    def _prepare_suuvi_session(self):
        # validate participant + countdown + track first
        try:
            self.participant_number = int(self._p_entry.get())
        except ValueError:
            self._p_entry.configure(border_color=C_DANGER)
            return
        try:
            self.countdown_seconds = max(0, int(self._delay_entry.get()))
        except ValueError:
            self._delay_entry.configure(border_color=C_DANGER)
            return

        track = self._suuvi_track_var.get()
        if not track or not os.path.exists(os.path.join(SUUVI_DIR, track)):
            self._suuvi_status.configure(
                text="No valid track selected.", text_color=C_DANGER)
            return

        # EEG gating: if recording is requested, require all 3 checks green
        self.eeg_recording_enabled = bool(self._eeg_enable_var.get())
        if self.eeg_recording_enabled:
            url = self._get_url_from_entry()
            if not url:
                self._suuvi_status.configure(
                    text="Enter a WebSocket URL for the ArcTop stream.",
                    text_color=C_DANGER)
                return
            self.arctop_ws_url = url
            if not (self.eeg_bt_ok and self.eeg_app_ok and self.eeg_stream_ok):
                self._suuvi_status.configure(
                    text="Run all three EEG checks before starting "
                         "(or turn off EEG recording).",
                    text_color=C_DANGER)
                return

        self.arctop_confirmed = self.eeg_recording_enabled
        self.use_device = False
        self.current_song = os.path.splitext(track)[0]
        self.current_song_file = track
        self.song_duration = 0  # unknown, will end when music stops
        self._run_volume_check()

    # ── EEG check rows ───────────────────────────────────────────────

    def _build_eeg_check_rows(self):
        for w in self._eeg_check_frame.winfo_children():
            w.destroy()

        # ── Row 1: Bluetooth ────────────────────────────
        row1 = ctk.CTkFrame(self._eeg_check_frame, fg_color="transparent")
        row1.pack(fill="x", pady=4)
        self._eeg_bt_icon = ctk.CTkLabel(
            row1, text="•", width=24, font=("Helvetica", 18, "bold"),
            text_color=C_MUTED)
        self._eeg_bt_icon.pack(side="left", padx=(8, 6))
        self._eeg_bt_label = ctk.CTkLabel(
            row1, text=f"Bluetooth: looking for {ARCTOP_HEADPHONES_NAME}…",
            anchor="w", font=("Helvetica", 13))
        self._eeg_bt_label.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(row1, text="Check", width=70, height=26,
                      font=("Helvetica", 12),
                      command=self._refresh_bt_check).pack(side="right", padx=8)

        # ── Row 2: ArcTop app ───────────────────────────
        row2 = ctk.CTkFrame(self._eeg_check_frame, fg_color="transparent")
        row2.pack(fill="x", pady=4)
        self._eeg_app_icon = ctk.CTkLabel(
            row2, text="•", width=24, font=("Helvetica", 18, "bold"),
            text_color=C_MUTED)
        self._eeg_app_icon.pack(side="left", padx=(8, 6))
        self._eeg_app_label = ctk.CTkLabel(
            row2, text="ArcTop app running on this Mac…",
            anchor="w", font=("Helvetica", 13))
        self._eeg_app_label.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(row2, text="Check", width=70, height=26,
                      font=("Helvetica", 12),
                      command=self._refresh_app_check).pack(side="right", padx=8)

        # ── Row 3: Data stream (persistent connection) ──
        row3 = ctk.CTkFrame(self._eeg_check_frame, fg_color="transparent")
        row3.pack(fill="x", pady=4)
        self._eeg_stream_icon = ctk.CTkLabel(
            row3, text="•", width=24, font=("Helvetica", 18, "bold"),
            text_color=C_MUTED)
        self._eeg_stream_icon.pack(side="left", padx=(8, 6))
        self._eeg_stream_label = ctk.CTkLabel(
            row3, text="Data stream: not connected",
            anchor="w", font=("Helvetica", 13))
        self._eeg_stream_label.pack(side="left", fill="x", expand=True)
        self._eeg_stream_btn = ctk.CTkButton(
            row3, text="Connect", width=100, height=26,
            font=("Helvetica", 12),
            command=self._toggle_eeg_connection)
        self._eeg_stream_btn.pack(side="right", padx=8)

    def _refresh_eeg_check_visibility(self):
        on = bool(self._eeg_enable_var.get())
        if on:
            self._eeg_check_frame.pack(fill="x", padx=18, pady=(0, 4))
            self._eeg_url_frame.pack(fill="x", padx=10, pady=(0, 12))
            # auto-run the lightweight checks immediately
            self._refresh_bt_check()
            self._refresh_app_check()
            # restore any existing connection state in the UI
            self._update_stream_row_from_state()
            self._start_stream_poll()
        else:
            self._eeg_check_frame.pack_forget()
            self._eeg_url_frame.pack_forget()
            self._stop_stream_poll()

    def _get_url_from_entry(self) -> str:
        return self._eeg_url_entry.get("1.0", "end").strip()

    def _on_url_edited(self, _event=None):
        # if the URL changes while connected, drop the old connection so the
        # next "Connect" picks up the new URL
        if self.arctop.is_connected or (
                self.arctop._stream_task and not self.arctop._stream_task.done()):
            self._worker(self.arctop.disconnect_stream, lambda _: None)
        self.eeg_stream_ok = False
        if hasattr(self, "_eeg_stream_btn"):
            self._eeg_stream_btn.configure(text="Connect")
        if hasattr(self, "_eeg_stream_icon"):
            self._set_check(self._eeg_stream_icon, self._eeg_stream_label,
                            None, "Data stream: URL changed — reconnect")

    def _on_eeg_toggle(self):
        self._refresh_eeg_check_visibility()

    @staticmethod
    def _set_check(icon_lbl, text_lbl, ok: bool | None, text: str):
        if ok is True:
            icon_lbl.configure(text="✓", text_color=C_SUCCESS)
        elif ok is False:
            icon_lbl.configure(text="✗", text_color=C_DANGER)
        else:
            icon_lbl.configure(text="…", text_color=C_WARNING)
        text_lbl.configure(text=text)

    def _refresh_bt_check(self):
        self._set_check(self._eeg_bt_icon, self._eeg_bt_label,
                        None, "Bluetooth: scanning…")
        self._worker(_check_mw75_bluetooth, self._on_bt_check_done)

    def _on_bt_check_done(self, result):
        paired, connected, text = result
        ok = paired and connected
        self.eeg_bt_ok = ok
        self._set_check(self._eeg_bt_icon, self._eeg_bt_label, ok,
                        f"Bluetooth: {text}")

    def _refresh_app_check(self):
        self._set_check(self._eeg_app_icon, self._eeg_app_label,
                        None, "Checking for ArcTop app…")
        self._worker(_check_arctop_app_running, self._on_app_check_done)

    def _on_app_check_done(self, running: bool):
        self.eeg_app_ok = running
        text = "ArcTop app is running" if running else "ArcTop app not detected"
        self._set_check(self._eeg_app_icon, self._eeg_app_label, running, text)

    def _toggle_eeg_connection(self):
        # If already connected (or trying), disconnect.
        if self.arctop.is_connected or (
                self.arctop._stream_task and not self.arctop._stream_task.done()):
            self._eeg_stream_btn.configure(state="disabled", text="Disconnecting…")
            self._worker(self.arctop.disconnect_stream, self._on_disconnect_done)
            return

        url = self._get_url_from_entry()
        if not url:
            self._set_check(self._eeg_stream_icon, self._eeg_stream_label,
                            False, "Data stream: URL is empty")
            return
        self.arctop_ws_url = url
        self._set_check(self._eeg_stream_icon, self._eeg_stream_label,
                        None, "Data stream: connecting…")
        self._eeg_stream_btn.configure(state="disabled", text="Connecting…")
        self._worker(lambda: self.arctop.connect_stream(url),
                     self._on_connect_done)

    def _on_connect_done(self, result):
        ok, msg = result
        self._eeg_stream_btn.configure(state="normal")
        if not ok:
            self.eeg_stream_ok = False
            self._eeg_stream_btn.configure(text="Connect")
            self._set_check(self._eeg_stream_icon, self._eeg_stream_label,
                            False, f"Data stream: {msg}")
        else:
            self._eeg_stream_btn.configure(text="Disconnect")
            # waiting for first event to confirm streaming
            self._set_check(self._eeg_stream_icon, self._eeg_stream_label,
                            None, "Data stream: connected, waiting for events…")

    def _on_disconnect_done(self, _result):
        self.eeg_stream_ok = False
        self._eeg_stream_btn.configure(state="normal", text="Connect")
        self._set_check(self._eeg_stream_icon, self._eeg_stream_label,
                        None, "Data stream: disconnected")

    # ── live stream poll (drives the row 3 indicator) ───────────────
    def _start_stream_poll(self):
        if getattr(self, "_stream_poll_id", None):
            return
        self._stream_poll_id = self.after(500, self._poll_stream_status)

    def _stop_stream_poll(self):
        if getattr(self, "_stream_poll_id", None):
            self.after_cancel(self._stream_poll_id)
            self._stream_poll_id = None

    def _poll_stream_status(self):
        self._stream_poll_id = None
        # If the EEG section is hidden or the page changed, stop polling.
        if (not hasattr(self, "_eeg_stream_icon")
                or not self._eeg_stream_icon.winfo_exists()):
            return
        self._update_stream_row_from_state()
        self._stream_poll_id = self.after(750, self._poll_stream_status)

    def _update_stream_row_from_state(self):
        if not hasattr(self, "_eeg_stream_icon"):
            return
        if not self._eeg_stream_icon.winfo_exists():
            return
        a = self.arctop
        if a.is_connected:
            self._eeg_stream_btn.configure(text="Disconnect")
            # Connection alone is enough to start — Arctop only pushes events
            # while a paradigm is actively running on the headphones, which
            # may not be the case yet at setup time.
            self.eeg_stream_ok = True
            if a.events_received > 0:
                self._set_check(
                    self._eeg_stream_icon, self._eeg_stream_label, True,
                    f"Data stream: live — {a.events_received} events received")
            else:
                self._set_check(
                    self._eeg_stream_icon, self._eeg_stream_label, True,
                    "Data stream: connected (waiting for Arctop to push events)")
        else:
            self.eeg_stream_ok = False
            if a.last_error:
                self._eeg_stream_btn.configure(text="Connect")
                self._set_check(
                    self._eeg_stream_icon, self._eeg_stream_label, False,
                    f"Data stream: {a.last_error}")
            elif a._stream_task and not a._stream_task.done():
                # task running but not yet open (initial connect or reconnect backoff)
                self._eeg_stream_btn.configure(text="Disconnect")
                self._set_check(
                    self._eeg_stream_icon, self._eeg_stream_label, None,
                    "Data stream: connecting…")
            else:
                self._eeg_stream_btn.configure(text="Connect")
                self._set_check(
                    self._eeg_stream_icon, self._eeg_stream_label, None,
                    "Data stream: not connected")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  SHARED — Volume Check (before every session)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    _VOL_STEP = 5  # system volume change per clicker press (0–100 scale)

    def _run_volume_check(self):
        """Show an 'analyzing' splash, then hand off to the volume-check page."""
        self._clear_page()
        ctk.CTkLabel(
            self.page_frame, text="Analyzing track volume...",
            font=("Helvetica", 20), text_color=C_MUTED,
        ).pack(pady=(120, 0))

        if self.mode == "suuvi":
            path = os.path.join(SUUVI_DIR, self.current_song_file)
        else:
            path = os.path.join(STIMULI_DIR, self.current_song_file)

        self._worker(
            lambda: _generate_warm_tone(_analyze_peak(path)),
            self._show_volume_check,
        )

    # ── system volume helpers (macOS) ────────────────────────────────

    @staticmethod
    def _get_system_volume() -> int:
        """Return macOS system output volume (0–100), or -1 on error."""
        try:
            r = subprocess.run(
                ["osascript", "-e", "output volume of (get volume settings)"],
                capture_output=True, text=True, timeout=2)
            return int(r.stdout.strip())
        except Exception:
            return -1

    @staticmethod
    def _set_system_volume(level: int):
        """Set macOS system output volume (clamped 0–100)."""
        level = max(0, min(100, level))
        with contextlib.suppress(Exception):
            subprocess.run(
                ["osascript", "-e", f"set volume output volume {level}"],
                capture_output=True, timeout=2)

    # ── volume check page ────────────────────────────────────────────

    def _show_volume_check(self, tone_sound):
        self._clear_page()
        self._volume_tone = tone_sound
        accent = C_SUUVI if self.mode == "suuvi" else C_PRIMARY

        ctk.CTkLabel(
            self.page_frame, text="Volume Check",
            font=("Helvetica", 30, "bold"),
        ).pack(pady=(30, 16))

        card = ctk.CTkFrame(self.page_frame, corner_radius=12)
        card.pack(fill="x", padx=60, pady=10)

        vol_method = ("the clicker" if self.clicker_enabled
                      else "the keyboard volume keys")
        for text in [
            "A reference tone is playing at this track's peak loudness.",
            f"The participant can adjust volume with {vol_method}.",
            "Once they're comfortable, give a thumbs-up and press 'Volume OK'.",
            "This is the loudest the track will get during playback.",
        ]:
            ctk.CTkLabel(
                card, text=text, font=("Helvetica", 14),
                wraplength=580, justify="left",
            ).pack(padx=24, pady=(8, 4))
        ctk.CTkFrame(card, height=8, fg_color="transparent").pack()

        # Volume level display
        vol_frame = ctk.CTkFrame(self.page_frame, fg_color="transparent")
        vol_frame.pack(pady=(16, 4))

        self._vol_level_lbl = ctk.CTkLabel(
            vol_frame, text="",
            font=("Helvetica", 22, "bold"), text_color=accent,
        )
        self._vol_level_lbl.pack()

        self._vol_bar = ctk.CTkProgressBar(
            self.page_frame, width=400, height=12, progress_color=accent,
        )
        self._vol_bar.pack(pady=(4, 4))

        if self.clicker_enabled:
            hint = (f"Clicker: [{self.clicker_vol_up_key}] = louder  |  "
                    f"[{self.clicker_vol_down_key}] = softer")
        else:
            hint = "No clicker configured — use keyboard volume keys"
        self._vol_hint = ctk.CTkLabel(
            self.page_frame, text=hint,
            font=("Helvetica", 12), text_color=C_MUTED,
        )
        self._vol_hint.pack(pady=(2, 14))

        # Buttons
        btn_row = ctk.CTkFrame(self.page_frame, fg_color="transparent")
        btn_row.pack(pady=8)

        ctk.CTkButton(
            btn_row, text="Volume OK — Start Session",
            font=("Helvetica", 17, "bold"), width=320, height=52,
            fg_color=C_SUCCESS, hover_color="#3ba882", text_color="#000",
            command=self._volume_ok,
        ).pack(side="left", padx=10)

        ctk.CTkButton(
            btn_row, text="Cancel",
            font=("Helvetica", 14, "bold"), width=120, height=52,
            fg_color="#2d3a4a", hover_color="#3d4a5a",
            command=self._volume_cancel,
        ).pack(side="left", padx=10)

        # Show initial volume level and start polling
        self._vol_check_active = True
        self._refresh_vol_display()
        self._poll_vol()

        # Bind clicker keys for volume control
        self._vol_key_bind = self.bind("<KeyPress>", self._on_volume_key)

        # Start looping the tone with a gentle fade-in
        self._volume_tone.play(loops=-1, fade_ms=1500)

    def _on_volume_key(self, event):
        """Handle clicker volume keys (calibrated) during volume check."""
        if self.clicker_enabled and event.keysym == self.clicker_vol_up_key:
            vol = self._get_system_volume()
            if vol >= 0:
                self._set_system_volume(vol + self._VOL_STEP)
                self._refresh_vol_display()
        elif self.clicker_enabled and event.keysym == self.clicker_vol_down_key:
            vol = self._get_system_volume()
            if vol >= 0:
                self._set_system_volume(vol - self._VOL_STEP)
                self._refresh_vol_display()

    def _refresh_vol_display(self):
        """Update the volume level label and bar."""
        vol = self._get_system_volume()
        if vol >= 0:
            self._vol_level_lbl.configure(text=f"System Volume: {vol}%")
            self._vol_bar.set(vol / 100.0)
        else:
            self._vol_level_lbl.configure(text="System Volume: unknown")
            self._vol_bar.set(0)

    def _poll_vol(self):
        """Periodically refresh the volume display so it tracks changes
        from any source (keyboard volume keys, menu bar, clicker)."""
        if not self._vol_check_active:
            return
        self._refresh_vol_display()
        self.after(500, self._poll_vol)

    def _volume_ok(self):
        """Unbind clicker, stop polling, fade out tone, proceed to session."""
        self._vol_check_active = False
        if hasattr(self, "_vol_key_bind") and self._vol_key_bind:
            self.unbind("<KeyPress>", self._vol_key_bind)
            self._vol_key_bind = None
        if hasattr(self, "_volume_tone") and self._volume_tone:
            self._volume_tone.fadeout(500)
        self.after(600, self._start_session_page)

    def _volume_cancel(self):
        """Unbind clicker, stop polling, stop tone, go back to setup."""
        self._vol_check_active = False
        if hasattr(self, "_vol_key_bind") and self._vol_key_bind:
            self.unbind("<KeyPress>", self._vol_key_bind)
            self._vol_key_bind = None
        if hasattr(self, "_volume_tone") and self._volume_tone:
            self._volume_tone.fadeout(300)
        self.after(400, self._show_home)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  SHARED — Session Page (both modes)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _start_session_page(self):
        """Shared session page. In Suuvi mode, runs a countdown first."""
        self._clear_page()

        self.chills_reports = []
        self.device_events = []
        self.session_active = True
        self.trigger_timers = []
        self.playback_start = None
        self.playback_start_utc = None
        self.playback_start_monotonic = None

        is_suuvi = (self.mode == "suuvi")
        accent = C_SUUVI if is_suuvi else C_PRIMARY

        # header
        title = f"Now Playing: {self.current_song}"
        ctk.CTkLabel(self.page_frame, text=title,
                     font=("Helvetica", 28, "bold")).pack(pady=(30, 4))
        ctk.CTkLabel(self.page_frame, text=f"Participant #{self.participant_number}",
                     font=("Helvetica", 14), text_color=C_MUTED).pack(pady=(0, 20))

        # BLE status (Frisson only)
        if not is_suuvi and self.use_device:
            self._device_dot = ctk.CTkLabel(self.page_frame, text="",
                                            font=("Helvetica", 12))
            self._device_dot.pack(pady=(0, 6))
            self._refresh_device_dot()

        # EEG status (Suuvi + recording)
        if is_suuvi and self.eeg_recording_enabled:
            self._eeg_session_dot = ctk.CTkLabel(self.page_frame, text="",
                                                 font=("Helvetica", 12))
            self._eeg_session_dot.pack(pady=(0, 6))
            self._refresh_eeg_session_dot()

        # countdown / timer label
        self._timer_lbl = ctk.CTkLabel(self.page_frame, text="",
                                       font=("Helvetica", 26, "bold"))
        self._timer_lbl.pack(pady=(0, 8))

        # progress bar
        self._progress = ctk.CTkProgressBar(
            self.page_frame, width=500, height=14, progress_color=accent)
        self._progress.set(0)
        self._progress.pack(pady=(0, 30))

        # chills counter
        ctk.CTkLabel(self.page_frame, text="Chills Reported",
                     font=("Helvetica", 14), text_color=C_MUTED).pack()
        self._chills_lbl = ctk.CTkLabel(self.page_frame, text="0",
                                        font=("Helvetica", 80, "bold"))
        self._chills_lbl.pack(pady=(2, 8))

        self._instr_lbl = ctk.CTkLabel(
            self.page_frame,
            text="Press the clicker (or any key) when you experience chills!",
            font=("Helvetica", 15), text_color=C_MUTED)
        self._instr_lbl.pack(pady=(0, 25))

        # Manual trigger panel (Frisson only) — gives the operator ad-hoc
        # control during a session. All fires are logged as manual_trigger.
        if not is_suuvi:
            mt_card = ctk.CTkFrame(self.page_frame, corner_radius=10)
            mt_card.pack(fill="x", padx=80, pady=(0, 14))
            ctk.CTkLabel(mt_card,
                         text=f"Manual triggers — intensity: {self.session_intensity}",
                         font=("Helvetica", 12, "bold"), text_color=C_MUTED
                         ).pack(pady=(8, 4))
            mt_row = ctk.CTkFrame(mt_card, fg_color="transparent")
            mt_row.pack(pady=(0, 10))
            for ch in (1, 2, 3):
                ctk.CTkButton(
                    mt_row, text=f"P{ch}", width=70, height=32,
                    font=("Helvetica", 13, "bold"),
                    fg_color=C_ACCENT, hover_color="#1a4a7a",
                    command=lambda c=ch: self._do_fire(
                        channel=c, pattern="single",
                        intensity=self.session_intensity, source="manual")
                ).pack(side="left", padx=4)
            ctk.CTkButton(
                mt_row, text="Wave", width=90, height=32,
                font=("Helvetica", 13, "bold"),
                fg_color=C_ACCENT, hover_color="#1a4a7a",
                command=lambda: self._do_fire(
                    pattern="wave", intensity=self.session_intensity,
                    source="manual")
            ).pack(side="left", padx=8)
            ctk.CTkButton(
                mt_row, text="STOP", width=90, height=32,
                font=("Helvetica", 13, "bold"),
                fg_color=C_DANGER, hover_color="#c0392b",
                command=lambda: self._emergency_stop(source="session-button")
            ).pack(side="left", padx=8)

        ctk.CTkButton(self.page_frame, text="Stop Session",
                      font=("Helvetica", 14, "bold"), width=160, height=40,
                      fg_color=C_DANGER, hover_color="#c0392b",
                      command=self._abort_session).pack()

        # key capture — bind AFTER UI is built
        self._key_bind_id = self.bind("<KeyPress>", self._on_key)

        # Suuvi: countdown then play.  Frisson: play immediately.
        if is_suuvi and self.countdown_seconds > 0:
            self._remaining = self.countdown_seconds
            self._timer_lbl.configure(text=f"Starting in {self._remaining}...")
            self._instr_lbl.configure(
                text="Get ready... track will start after the countdown.")
            self._run_countdown()
        else:
            self._begin_playback()

    # ── countdown (Suuvi) ────────────────────────────────────────────

    def _run_countdown(self):
        if not self.session_active:
            return
        if self._remaining <= 0:
            self._begin_playback()
            return
        self._timer_lbl.configure(text=f"Starting in {self._remaining}...")
        self._remaining -= 1
        self._countdown_id = self.after(1000, self._run_countdown)

    # ── playback (shared) ────────────────────────────────────────────

    def _begin_playback(self):
        if self.mode == "suuvi":
            path = os.path.join(SUUVI_DIR, self.current_song_file)
        else:
            path = os.path.join(STIMULI_DIR, self.current_song_file)

        try:
            pygame.mixer.music.load(path)
            pygame.mixer.music.play()
        except Exception as exc:
            self._instr_lbl.configure(text=f"Audio error: {exc}", text_color=C_DANGER)
            return

        self.playback_start = time.time()
        self.playback_start_utc = _utc_now()
        # Monotonic clock for trigger scheduling — wall-clock isn't safe.
        # TODO (v2): replace with pygame.mixer.music.get_pos() polling for
        # research-grade timing precision. Current approach has ~10–50 ms
        # drift from audio backend startup latency.
        self.playback_start_monotonic = time.monotonic()

        # Suuvi: open a CSV on the existing live stream. The WebSocket itself
        # was opened earlier on the setup page (Connect button) and stays up
        # across sessions, so Arctop's portal sees one continuous client.
        self.eeg_csv_filename = None
        self.eeg_session_event_count = 0
        if self.mode == "suuvi" and self.eeg_recording_enabled:
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
            fname = f"eeg_{now_str}_P{self.participant_number:03d}_{self.current_song}.csv"
            ok, msg = self.arctop.start_recording(os.path.join(DATA_DIR, fname))
            if ok:
                self.eeg_csv_filename = fname
            else:
                print(f"[App] EEG recorder failed to start: {msg}")

        self._instr_lbl.configure(
            text="Press the clicker (or any key) when you experience chills!",
            text_color=C_MUTED)

        # Frisson: schedule wave triggers at the parsed times.
        if self.mode == "frisson" and self.use_device and self.device.is_connected:
            for t in self.session_trigger_times:
                timer = threading.Timer(
                    max(0.0, float(t)),
                    self._fire_scheduled_trigger,
                    args=[float(t)])
                timer.daemon = True
                timer.start()
                self.trigger_timers.append(timer)

        self._tick_session()

    # ── device dot (Frisson) ─────────────────────────────────────────

    def _refresh_device_dot(self):
        if not self.session_active:
            return
        if not hasattr(self, "_device_dot") or not self._device_dot.winfo_exists():
            return
        if self.device.is_connected:
            self._device_dot.configure(
                text=f"Device: {self.device.device_name}", text_color=C_SUCCESS)
        else:
            self._device_dot.configure(
                text="Device: DISCONNECTED", text_color=C_DANGER)
        self.after(3000, self._refresh_device_dot)

    def _refresh_eeg_session_dot(self):
        if not self.session_active:
            return
        if not hasattr(self, "_eeg_session_dot"):
            return
        if not self._eeg_session_dot.winfo_exists():
            return
        a = self.arctop
        session_count = max(0, a.events_received - a._record_start_count)
        if a.is_connected and a.is_recording:
            self._eeg_session_dot.configure(
                text=f"EEG: recording — {session_count} events this session "
                     f"(total {a.events_received})",
                text_color=C_SUCCESS)
        elif a.is_connected:
            self._eeg_session_dot.configure(
                text=f"EEG: connected, not recording (total {a.events_received})",
                text_color=C_WARNING)
        else:
            self._eeg_session_dot.configure(
                text="EEG: DISCONNECTED", text_color=C_DANGER)
        self.after(750, self._refresh_eeg_session_dot)

    # ── key handler ──────────────────────────────────────────────────

    _IGNORE_KEYS = frozenset({
        "Shift_L", "Shift_R", "Control_L", "Control_R",
        "Alt_L", "Alt_R", "Meta_L", "Meta_R",
        "Caps_Lock", "Tab", "Escape",
    })

    def _on_key(self, event):
        if not self.session_active or self.playback_start is None:
            return
        if event.keysym == "Escape":
            self._abort_session()
            return
        if event.keysym in self._IGNORE_KEYS:
            return

        elapsed = time.time() - self.playback_start
        self.chills_reports.append({
            "elapsed_seconds": round(elapsed, 3),
            "utc": _utc_now(),
            "key": event.keysym,
        })

        n = len(self.chills_reports)
        self._chills_lbl.configure(text=str(n), text_color=C_SUUVI if self.mode == "suuvi" else C_PRIMARY)
        self.after(180, lambda: self._chills_lbl.configure(text_color=("gray90", "gray90")))

    # ── scheduled trigger (Frisson only, runs on threading.Timer thread) ──

    def _fire_scheduled_trigger(self, planned: float):
        if not self.session_active:
            return
        # Marshal the actual command back onto the main thread so logging
        # and any UI side effects happen on the main loop.
        self.after(0, lambda p=planned: self._do_scheduled_fire(p))

    def _do_scheduled_fire(self, planned: float):
        if not self.session_active:
            return
        ok, msg = self._do_fire(
            pattern="wave", intensity=self.session_intensity,
            source="scheduled")
        # Augment the just-logged event with planned/actual seconds.
        if self.device_events:
            actual = ((time.monotonic() - self.playback_start_monotonic)
                      if self.playback_start_monotonic else planned)
            self.device_events[-1]["planned_sec"] = round(planned, 3)
            self.device_events[-1]["actual_sec"] = round(actual, 3)

    # ── session timer / progress ─────────────────────────────────────

    def _tick_session(self):
        if not self.session_active:
            return
        if not pygame.mixer.music.get_busy() and self.playback_start is not None:
            elapsed = time.time() - self.playback_start
            if elapsed > 5:
                self._end_session()
                return

        elapsed = time.time() - self.playback_start if self.playback_start else 0
        if self.song_duration > 0:
            frac = min(elapsed / self.song_duration, 1.0)
            self._timer_lbl.configure(
                text=f"{self._fmt(elapsed)} / {self._fmt(self.song_duration)}")
        else:
            frac = 0
            self._timer_lbl.configure(text=f"{self._fmt(elapsed)}")
        self._progress.set(frac)

        self._session_update_id = self.after(200, self._tick_session)

    # ── end / abort ──────────────────────────────────────────────────

    def _end_session(self):
        if not self.session_active:
            return
        self.session_active = False

        if self.arctop.is_recording:
            self.eeg_session_event_count = self.arctop.stop_recording()

        if self._key_bind_id:
            self.unbind("<KeyPress>", self._key_bind_id)
            self._key_bind_id = None
        if self._countdown_id:
            self.after_cancel(self._countdown_id)
            self._countdown_id = None
        for t in self.trigger_timers:
            t.cancel()
        self.trigger_timers.clear()
        if self._session_update_id:
            self.after_cancel(self._session_update_id)
            self._session_update_id = None

        with contextlib.suppress(Exception):
            pygame.mixer.music.stop()

        # Audio stopped → make sure no Peltiers are still firing.
        if self.mode == "frisson" and self.device.is_connected:
            with contextlib.suppress(Exception):
                self.device.emergency_stop()

        self.show_post_session_page()

    def _abort_session(self):
        self._end_session()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  SHARED — Post-Session Survey
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def show_post_session_page(self):
        self._clear_page()
        elapsed = (time.time() - self.playback_start) if self.playback_start else 0
        accent = C_SUUVI if self.mode == "suuvi" else C_PRIMARY

        ctk.CTkLabel(self.page_frame, text="Session Complete",
                     font=("Helvetica", 30, "bold")).pack(pady=(30, 20))

        card = ctk.CTkFrame(self.page_frame, corner_radius=10)
        card.pack(fill="x", padx=80, pady=8)
        for lbl, val in [
            ("Mode", self.mode.capitalize()),
            ("Track", self.current_song),
            ("Duration", self._fmt(elapsed)),
            ("Chills reported", str(len(self.chills_reports))),
        ]:
            row = ctk.CTkFrame(card, fg_color="transparent")
            row.pack(fill="x", padx=24, pady=6)
            ctk.CTkLabel(row, text=f"{lbl}:", font=("Helvetica", 14),
                         text_color=C_MUTED, width=150, anchor="e").pack(side="left")
            ctk.CTkLabel(row, text=val, font=("Helvetica", 14, "bold"),
                         anchor="w").pack(side="left", padx=(10, 0))

        q = ctk.CTkFrame(self.page_frame, fg_color="transparent")
        q.pack(fill="x", padx=80, pady=(16, 0))

        ctk.CTkLabel(q, text="Did you experience chills?",
                     font=("Helvetica", 16)).pack(pady=(8, 8))
        self._yn_var = ctk.StringVar(value="Yes")
        yn = ctk.CTkFrame(q, fg_color="transparent")
        yn.pack()
        ctk.CTkRadioButton(yn, text="Yes", variable=self._yn_var, value="Yes",
                           font=("Helvetica", 14)).pack(side="left", padx=16)
        ctk.CTkRadioButton(yn, text="No", variable=self._yn_var, value="No",
                           font=("Helvetica", 14)).pack(side="left", padx=16)

        ctk.CTkLabel(q, text="How intense were the chills?  (1 = mild, 10 = very intense)",
                     font=("Helvetica", 16)).pack(pady=(20, 8))
        sl_row = ctk.CTkFrame(q, fg_color="transparent")
        sl_row.pack()
        ctk.CTkLabel(sl_row, text="1", font=("Helvetica", 12),
                     text_color=C_MUTED).pack(side="left", padx=(0, 6))
        self._intensity_var = ctk.DoubleVar(value=5)
        ctk.CTkSlider(sl_row, from_=1, to=10, number_of_steps=9,
                      variable=self._intensity_var, width=320,
                      command=self._update_intensity_lbl).pack(side="left")
        ctk.CTkLabel(sl_row, text="10", font=("Helvetica", 12),
                     text_color=C_MUTED).pack(side="left", padx=(6, 0))
        self._int_lbl = ctk.CTkLabel(q, text="5", font=("Helvetica", 24, "bold"),
                                     text_color=accent)
        self._int_lbl.pack(pady=4)

        ctk.CTkButton(self.page_frame, text="Save & Next Participant",
                      font=("Helvetica", 17, "bold"), width=300, height=52,
                      fg_color=C_SUCCESS, hover_color="#3ba882", text_color="#000",
                      command=self._save_and_next).pack(pady=(20, 10))

    def _update_intensity_lbl(self, val):
        self._int_lbl.configure(text=str(int(float(val))))

    def _save_and_next(self):
        elapsed = (time.time() - self.playback_start) if self.playback_start else 0
        end_utc = _utc_now()

        data = {
            "mode": self.mode,
            "session_id": f"{datetime.now(timezone.utc).strftime('%Y-%m-%d_%H%M%S')}_P{self.participant_number:03d}",
            "participant_id": self.participant_number,
            "timestamp_utc": end_utc,
            "track_name": self.current_song,
            "track_file": self.current_song_file,
            "duration_seconds": round(elapsed, 2),
            "play_pressed_utc": self.playback_start_utc,
            "track_start_utc": self.playback_start_utc,
            "track_end_utc": end_utc,
            "chills_reports": self.chills_reports,
            "total_chills_count": len(self.chills_reports),
            "post_survey": {
                "experienced_chills": self._yn_var.get() == "Yes",
                "chills_intensity": int(self._intensity_var.get()),
            },
        }

        # mode-specific fields
        if self.mode == "suuvi":
            data["arctop_confirmed"] = self.arctop_confirmed
            data["countdown_seconds"] = self.countdown_seconds
            data["eeg_recording_enabled"] = self.eeg_recording_enabled
            data["eeg_csv_file"] = self.eeg_csv_filename
            data["eeg_event_count"] = self.eeg_session_event_count
        else:
            data["device_used"] = self.use_device
            data["connection_mode"] = self.connection_mode
            data["device_port"] = self.device_port
            data["intensity_setting"] = self.session_intensity
            data["scheduled_trigger_times"] = list(self.session_trigger_times)
            data["device_events"] = list(self.device_events)
            data["verify_skipped"] = self.verify_skipped
            data["verify_results"] = list(self.verify_results)

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
        prefix = "suuvi" if self.mode == "suuvi" else "session"
        fname = f"{prefix}_{now}_P{self.participant_number:03d}_{self.current_song}.json"

        try:
            with open(os.path.join(DATA_DIR, fname), "w") as f:
                json.dump(data, f, indent=2)
        except OSError as exc:
            print(f"[App] Save error: {exc}")

        self.participant_number += 1
        self._show_home() if self.mode == "suuvi" else self.show_frisson_setup_page()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  Cleanup
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _on_close(self):
        if self.session_active and self.playback_start:
            self.session_active = False
            with contextlib.suppress(Exception):
                pygame.mixer.music.stop()
            self._emergency_save()
        for t in self.trigger_timers:
            t.cancel()
        # Make absolutely sure no Peltiers are still on before we tear down.
        with contextlib.suppress(Exception):
            if self.device.is_connected:
                self.device.emergency_stop()
        with contextlib.suppress(Exception):
            self.arctop.stop()
        with contextlib.suppress(Exception):
            self.device.stop()
        with contextlib.suppress(Exception):
            pygame.mixer.quit()
        self.destroy()

    def _emergency_save(self):
        if self.arctop.is_recording:
            with contextlib.suppress(Exception):
                self.eeg_session_event_count = self.arctop.stop_recording()
        # Stop any in-progress Peltier action and log it.
        if self.mode == "frisson" and self.device.is_connected:
            with contextlib.suppress(Exception):
                self.device.emergency_stop()
                self._log_device_event(
                    event="emergency_stop", command="off", success=True,
                    source="window-close")
        elapsed = (time.time() - self.playback_start) if self.playback_start else 0
        data = {
            "mode": self.mode,
            "session_id": f"PARTIAL_{datetime.now(timezone.utc).strftime('%Y-%m-%d_%H%M%S')}_P{self.participant_number:03d}",
            "participant_id": self.participant_number,
            "timestamp_utc": _utc_now(),
            "track_name": self.current_song,
            "track_file": self.current_song_file or "unknown",
            "duration_seconds": round(elapsed, 2),
            "play_pressed_utc": self.playback_start_utc,
            "track_start_utc": self.playback_start_utc,
            "chills_reports": self.chills_reports,
            "total_chills_count": len(self.chills_reports),
            "post_survey": None,
            "eeg_recording_enabled": self.eeg_recording_enabled,
            "eeg_csv_file": self.eeg_csv_filename,
            "eeg_event_count": self.eeg_session_event_count,
            "note": "Session interrupted — partial data",
        }
        if self.mode == "frisson":
            data["connection_mode"] = self.connection_mode
            data["device_port"] = self.device_port
            data["intensity_setting"] = self.session_intensity
            data["scheduled_trigger_times"] = list(self.session_trigger_times)
            data["device_events"] = list(self.device_events)
            data["verify_skipped"] = self.verify_skipped
            data["verify_results"] = list(self.verify_results)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
        fname = f"PARTIAL_{now}_P{self.participant_number:03d}_{self.current_song}.json"
        with contextlib.suppress(Exception):
            with open(os.path.join(DATA_DIR, fname), "w") as f:
                json.dump(data, f, indent=2)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
if __name__ == "__main__":
    app = ChillsDemoApp()
    app.mainloop()
