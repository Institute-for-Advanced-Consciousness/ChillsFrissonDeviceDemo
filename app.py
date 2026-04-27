#!/usr/bin/env python3
"""
ChillsDemo — GUI station for running chills / ASMR experiments.

Two modes:
  * **Frisson** — direct BLE control of the Frisson haptic device with
    predefined trigger timings per song.
  * **Suuvi** — audio-only playback from Stimuli/Suuvi/ with ArcTop EEG
    headphone integration (configurable pre-play countdown, UTC timestamps
    for post-hoc alignment).

Launch:
    source venv/bin/activate && python app.py
"""

import array
import asyncio
import contextlib
import json
import math
import os
import random
import struct
import subprocess
import sys
import threading
import time
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
    from bleak import BleakClient, BleakScanner
except ImportError:
    _missing.append("bleak")

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

# ── Frisson BLE protocol ────────────────────────────────────────────────

FRISSON_SERVICE_UUID = "00002220-0000-1000-8000-00805f9b34fb"
BLE_TEST_PACKET = bytes([20, 255, 255, 255, 0, 0, 0, 0, 0, 30, 30, 30, 0])
BLE_SESSION_PACKET = bytes([20, 255, 255, 255, 0, 5, 3, 0, 0, 35, 33, 30, 0])
TRIGGER_LEAD_TIME = 0.25

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
#  FrissonBLE — persistent direct BLE connection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class FrissonBLE:
    """Manages a persistent BLE connection to the Frisson device."""

    def __init__(self):
        self.connected = False
        self.device_name = ""
        self.device_rssi: int | None = None
        self.services_info: list[dict] = []
        self.notifications: list[dict] = []
        self._client: BleakClient | None = None
        self._write_uuid: str | None = None
        self._write_response: bool = True
        self._notify_uuid: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self.on_disconnect_callback = None

    def start(self):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True)
        self._thread.start()

    def stop(self):
        if self._loop and self._client:
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(
                    self._safe_disconnect(), self._loop,
                ).result(timeout=4)
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        self.connected = False

    # ── blocking public API ──────────────────────────────────────────

    def connect(self):
        return self._submit(self._do_connect())

    def disconnect(self):
        return self._submit(self._safe_disconnect())

    def send(self, packet):
        return self._submit(self._do_write(packet))

    def send_and_listen(self, packet, wait=4.0):
        return self._submit(self._do_write_and_listen(packet, wait), timeout=wait + 5)

    def _submit(self, coro, timeout=15):
        if not self._loop:
            return False, "BLE not started"
        try:
            return asyncio.run_coroutine_threadsafe(
                coro, self._loop).result(timeout=timeout)
        except Exception as exc:
            return False, str(exc)[:80]

    # ── async internals ──────────────────────────────────────────────

    async def _do_connect(self):
        await self._safe_disconnect()
        self.services_info = []
        self.notifications = []

        device = await BleakScanner.find_device_by_filter(
            lambda _d, ad: FRISSON_SERVICE_UUID.lower()
            in [s.lower() for s in (ad.service_uuids or [])],
            timeout=6.0)
        if not device:
            return False, "Device not found — is it powered on?"

        self.device_rssi = getattr(device, "rssi", None)
        self._client = BleakClient(
            device, timeout=8.0, disconnected_callback=self._on_disconnect)
        await self._client.connect()

        frisson_chars = []
        for svc in self._client.services:
            svc_info = {"uuid": svc.uuid, "characteristics": []}
            for ch in svc.characteristics:
                svc_info["characteristics"].append({
                    "uuid": ch.uuid,
                    "properties": list(ch.properties),
                    "descriptors": [d.uuid for d in ch.descriptors],
                })
                if svc.uuid.lower() == FRISSON_SERVICE_UUID.lower():
                    frisson_chars.append(ch)
                    if "notify" in ch.properties or "indicate" in ch.properties:
                        self._notify_uuid = ch.uuid
            self.services_info.append(svc_info)

        if len(frisson_chars) >= 2:
            target = frisson_chars[1]
            self._write_uuid = target.uuid
            self._write_response = "write" in target.properties
        else:
            for ch in frisson_chars:
                if "write" in ch.properties or "write-without-response" in ch.properties:
                    self._write_uuid = ch.uuid
                    self._write_response = "write" in ch.properties
                    break

        if not self._write_uuid:
            await self._client.disconnect()
            return False, "Write characteristic not found"

        self._notify_uuids = []
        for svc in self._client.services:
            for ch in svc.characteristics:
                if "notify" in ch.properties or "indicate" in ch.properties:
                    try:
                        await self._client.start_notify(ch.uuid, self._on_notify)
                        self._notify_uuids.append(ch.uuid)
                    except Exception:
                        pass
        if self._notify_uuids:
            self._notify_uuid = self._notify_uuids[0]

        self.connected = True
        self.device_name = device.name or "Frisson"
        return True, self.device_name

    def _on_notify(self, _sender, data: bytearray):
        self.notifications.append({
            "time": time.time(), "data": bytes(data), "hex": data.hex()})

    async def _do_write(self, packet):
        if not self._client or not self._client.is_connected:
            self.connected = False
            return False, "Not connected"
        try:
            await self._client.write_gatt_char(
                self._write_uuid, packet, response=self._write_response)
            return True, "OK"
        except Exception as exc:
            self.connected = False
            return False, str(exc)[:80]

    async def _read_all_chars(self):
        reads = []
        for svc in self._client.services:
            for ch in svc.characteristics:
                if "read" in ch.properties:
                    try:
                        val = await self._client.read_gatt_char(ch.uuid)
                        reads.append({"service": svc.uuid, "char": ch.uuid,
                                      "properties": list(ch.properties),
                                      "value": bytes(val), "hex": val.hex(" "),
                                      "text": val.decode("utf-8", errors="replace")})
                    except Exception as exc:
                        reads.append({"service": svc.uuid, "char": ch.uuid,
                                      "properties": list(ch.properties),
                                      "error": str(exc)[:60]})
        return reads

    async def _do_write_and_listen(self, packet, wait):
        if not self._client or not self._client.is_connected:
            self.connected = False
            return False, "Not connected", []
        before = len(self.notifications)
        reads_before = await self._read_all_chars()
        try:
            await self._client.write_gatt_char(
                self._write_uuid, packet, response=self._write_response)
        except Exception as exc:
            self.connected = False
            return False, str(exc)[:80], []
        await asyncio.sleep(wait)
        reads_after = await self._read_all_chars()
        new_notifs = self.notifications[before:]
        return True, "OK", {
            "notifications": [n["data"] for n in new_notifs],
            "reads_before": reads_before, "reads_after": reads_after}

    async def _safe_disconnect(self):
        if self._client:
            for uuid in getattr(self, "_notify_uuids", []):
                with contextlib.suppress(Exception):
                    await self._client.stop_notify(uuid)
            with contextlib.suppress(Exception):
                if self._client.is_connected:
                    await self._client.disconnect()
        self.connected = False
        self._write_uuid = None
        self._write_response = True
        self._notify_uuid = None
        return True, "Disconnected"

    def _on_disconnect(self, _client):
        self.connected = False
        self._write_uuid = None
        self._notify_uuid = None
        if self.on_disconnect_callback:
            self.on_disconnect_callback()


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
        self.ble = FrissonBLE()
        self.session_active = False
        self.chills_reports: list[dict] = []
        self.current_song: str | None = None
        self.current_song_file: str | None = None
        self.song_duration = 0
        self.playback_start: float | None = None
        self.playback_start_utc: str | None = None
        self.trigger_timers: list[threading.Timer] = []
        self.device_triggers_fired: list[dict] = []
        self.participant_number = 1
        self.use_device = True
        self.arctop_confirmed = False
        self.countdown_seconds = 10
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

        # ── layout: page area + status bar ───────────────────────────
        self.page_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.page_frame.grid(row=1, column=0, sticky="nsew", padx=30, pady=(10, 5))

        self.status_bar = ctk.CTkFrame(self, height=44, corner_radius=0)
        self.status_bar.grid(row=2, column=0, sticky="ew")
        self._build_status_bar()

        # ── services ─────────────────────────────────────────────────
        self.ble.start()
        self.ble.on_disconnect_callback = lambda: self.after(0, self._poll_status)

        # ── participant counter ──────────────────────────────────────
        self.participant_number = self._next_participant_number()

        # ── go ───────────────────────────────────────────────────────
        self._show_clicker_setup()
        self._poll_status()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

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
        self._show_home()

    def _clicker_skip(self):
        if hasattr(self, "_ck_bind") and self._ck_bind:
            self.unbind("<KeyPress>", self._ck_bind)
            self._ck_bind = None
        self.clicker_enabled = False
        self.clicker_vol_down_key = None
        self.clicker_vol_up_key = None
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
        # mode
        if self.mode == "suuvi":
            self.lbl_mode.configure(text="Mode: Suuvi", text_color=C_SUUVI)
            self.lbl_ble.configure(text="No device needed", text_color=C_MUTED)
        else:
            self.lbl_mode.configure(text="Mode: Frisson", text_color=C_PRIMARY)
            if self.ble.connected:
                self.lbl_ble.configure(
                    text=f"Device: {self.ble.device_name}", text_color=C_SUCCESS)
            else:
                self.lbl_ble.configure(
                    text="Device: not connected", text_color=C_DANGER)

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
                     font=("Helvetica", 30, "bold")).pack(pady=(25, 20))

        card = ctk.CTkFrame(self.page_frame, corner_radius=12)
        card.pack(fill="x", padx=50, pady=10)
        for i, text in enumerate([
            "Power on the Frisson device.",
            "Click 'Scan & Connect' — the app pairs directly over Bluetooth.",
            "Click 'Test Trigger' to fire all 3 peltiers at full strength for 3 s.",
        ]):
            row = ctk.CTkFrame(card, fg_color="transparent")
            row.pack(fill="x", padx=20, pady=(12 if i == 0 else 3, 12 if i == 2 else 3))
            ctk.CTkLabel(row, text=f"{i+1}.", font=("Helvetica", 14, "bold"),
                         text_color=C_PRIMARY, width=28).pack(side="left")
            ctk.CTkLabel(row, text=text, font=("Helvetica", 14),
                         wraplength=620, anchor="w", justify="left"
                         ).pack(side="left", padx=(6, 0))

        self._conn_status = ctk.CTkLabel(
            self.page_frame, text="Not connected",
            font=("Courier", 12), text_color=C_MUTED,
            wraplength=700, justify="left", anchor="w")
        self._conn_status.pack(pady=(16, 10), fill="x", padx=50)

        btn_row = ctk.CTkFrame(self.page_frame, fg_color="transparent")
        btn_row.pack(pady=8)
        self._connect_btn = ctk.CTkButton(
            btn_row, text="Scan & Connect", font=("Helvetica", 15, "bold"),
            width=200, height=44, fg_color=C_ACCENT, hover_color="#1a4a7a",
            command=self._on_connect)
        self._connect_btn.pack(side="left", padx=8)
        ctk.CTkButton(
            btn_row, text="Disconnect", font=("Helvetica", 15, "bold"),
            width=140, height=44, fg_color="#2d3a4a", hover_color="#3d4a5a",
            command=self._on_disconnect).pack(side="left", padx=8)
        self._test_btn = ctk.CTkButton(
            btn_row, text="Test Trigger", font=("Helvetica", 15, "bold"),
            width=160, height=44, fg_color="#2d3a4a", hover_color="#3d4a5a",
            command=self._on_test)
        self._test_btn.pack(side="left", padx=8)

        self._skip_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(self.page_frame, text="Run without Frisson device",
                        variable=self._skip_var, font=("Helvetica", 13),
                        text_color=C_MUTED).pack(pady=(12, 16))

        ctk.CTkButton(
            self.page_frame, text="Continue to Session Setup",
            font=("Helvetica", 17, "bold"), width=300, height=52,
            fg_color=C_SUCCESS, hover_color="#3ba882", text_color="#000",
            command=self._go_to_frisson_setup).pack(pady=5)

        if self.ble.connected:
            self._conn_status.configure(
                text=f"Connected to {self.ble.device_name}", text_color=C_SUCCESS)

    def _on_connect(self):
        self._connect_btn.configure(text="Scanning...", fg_color=C_WARNING)
        self._conn_status.configure(text="Scanning...", text_color=C_WARNING)
        self._worker(self.ble.connect, self._connect_done)

    def _connect_done(self, result):
        ok, msg = result
        if ok:
            self._connect_btn.configure(text="Connected", fg_color=C_SUCCESS)
            parts = [f"Connected to {msg}"]
            if self.ble.device_rssi is not None:
                parts.append(f"RSSI: {self.ble.device_rssi} dBm")
            wr_mode = "w/ response" if self.ble._write_response else "no response"
            parts.append(f"Write: {wr_mode}")
            parts.append(f"Notify: {'yes' if self.ble._notify_uuid else 'no'}")
            self._conn_status.configure(text="  |  ".join(parts), text_color=C_SUCCESS)
        else:
            self._connect_btn.configure(text="Retry", fg_color=C_DANGER)
            self._conn_status.configure(text=msg, text_color=C_DANGER)
        self.after(2000, lambda: self._connect_btn.configure(
            text="Scan & Connect", fg_color=C_ACCENT))

    def _on_disconnect(self):
        self._worker(self.ble.disconnect, lambda _: self._conn_status.configure(
            text="Disconnected", text_color=C_MUTED))

    def _on_test(self):
        if not self.ble.connected:
            self._test_btn.configure(text="Not connected", fg_color=C_DANGER)
            self.after(1500, lambda: self._test_btn.configure(
                text="Test Trigger", fg_color="#2d3a4a"))
            return
        self._test_btn.configure(text="Firing (4 s)...", fg_color=C_WARNING)
        self._worker(
            lambda: self.ble.send_and_listen(BLE_TEST_PACKET, wait=4.0),
            self._test_done)

    def _test_done(self, result):
        ok, msg, report = result
        if ok:
            self._test_btn.configure(text="Device OK!", fg_color=C_SUCCESS)
            notifs = report.get("notifications", [])
            if notifs:
                info = f"Write OK. {len(notifs)} notification(s) received."
            else:
                n_sub = len(getattr(self.ble, "_notify_uuids", []))
                info = f"Write OK. No notifications (subscribed to {n_sub} char(s))."
            self._conn_status.configure(text=info, text_color=C_SUCCESS)
        else:
            self._test_btn.configure(text="Failed", fg_color=C_DANGER)
            self._conn_status.configure(text=f"Trigger failed: {msg}", text_color=C_DANGER)
        self.after(3000, lambda: self._test_btn.configure(
            text="Test Trigger", fg_color="#2d3a4a"))

    def _go_to_frisson_setup(self):
        self.use_device = not self._skip_var.get()
        self.show_frisson_setup_page()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  FRISSON — PAGE 2: Session Setup
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def show_frisson_setup_page(self):
        self._clear_page()

        ctk.CTkLabel(self.page_frame, text="Frisson Session Setup",
                     font=("Helvetica", 30, "bold")).pack(pady=(30, 25))

        p_row = ctk.CTkFrame(self.page_frame, fg_color="transparent")
        p_row.pack(pady=8)
        ctk.CTkLabel(p_row, text="Participant #:",
                     font=("Helvetica", 16)).pack(side="left", padx=(0, 10))
        self._p_entry = ctk.CTkEntry(p_row, width=80, font=("Helvetica", 16), justify="center")
        self._p_entry.insert(0, str(self.participant_number))
        self._p_entry.pack(side="left")

        ctk.CTkLabel(self.page_frame, text="Select Stimulus:",
                     font=("Helvetica", 16), text_color=C_MUTED).pack(pady=(25, 10))
        self._song_var = ctk.StringVar(value="Random")
        sf = ctk.CTkFrame(self.page_frame, fg_color="transparent")
        sf.pack()
        for opt in ["Random"] + list(SONGS.keys()):
            ctk.CTkRadioButton(sf, text=opt, variable=self._song_var, value=opt,
                               font=("Helvetica", 15)).pack(anchor="w", padx=50, pady=5)

        self._audio_lbl = ctk.CTkLabel(self.page_frame, text="", font=("Helvetica", 12))
        self._audio_lbl.pack(pady=(16, 0))
        missing = [c["file"] for c in SONGS.values()
                   if not os.path.exists(os.path.join(STIMULI_DIR, c["file"]))]
        if missing:
            self._audio_lbl.configure(text=f"Missing: {', '.join(missing)}", text_color=C_WARNING)
        else:
            self._audio_lbl.configure(text="All audio files found", text_color=C_SUCCESS)

        if self.use_device and not self.ble.connected:
            ctk.CTkLabel(self.page_frame,
                         text="Device not connected — go back or check 'run without'",
                         font=("Helvetica", 12), text_color=C_WARNING).pack(pady=(6, 0))

        ctk.CTkButton(self.page_frame, text="Start Session",
                      font=("Helvetica", 18, "bold"), width=260, height=55,
                      fg_color=C_PRIMARY, hover_color="#c93a52",
                      command=self._prepare_frisson_session).pack(pady=25)
        ctk.CTkButton(self.page_frame, text="Back to Device Setup",
                      font=("Helvetica", 13), width=200, height=34,
                      fg_color="transparent", hover_color="#2a2a3e", text_color=C_MUTED,
                      command=self.show_connection_page).pack()

    def _prepare_frisson_session(self):
        try:
            self.participant_number = int(self._p_entry.get())
        except ValueError:
            self._p_entry.configure(border_color=C_DANGER)
            return
        choice = self._song_var.get()
        self.current_song = random.choice(list(SONGS.keys())) if choice == "Random" else choice
        cfg = SONGS[self.current_song]
        self.current_song_file = cfg["file"]
        path = os.path.join(STIMULI_DIR, cfg["file"])
        if not os.path.exists(path):
            self._audio_lbl.configure(text=f"Not found: {cfg['file']}", text_color=C_DANGER)
            return
        self.song_duration = cfg["duration_est"]
        self._run_volume_check()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  SUUVI — Setup Page (replaces connection + session setup)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def show_suuvi_setup_page(self):
        self._clear_page()

        ctk.CTkLabel(self.page_frame, text="Suuvi Session Setup",
                     font=("Helvetica", 30, "bold"),
                     text_color=C_SUUVI).pack(pady=(25, 20))

        # ── ArcTop confirmation ──────────────────────────────────────
        arctop_card = ctk.CTkFrame(self.page_frame, corner_radius=12)
        arctop_card.pack(fill="x", padx=50, pady=(5, 10))

        ctk.CTkLabel(arctop_card, text="ArcTop EEG Headphones",
                     font=("Helvetica", 16, "bold")).pack(pady=(14, 4))
        ctk.CTkLabel(arctop_card,
                     text="Confirm the ArcTop headphones are connected and streaming "
                          "before starting. The app will record UTC timestamps so "
                          "button presses can be aligned with EEG data post-hoc.",
                     font=("Helvetica", 13), text_color=C_MUTED,
                     wraplength=600, justify="left").pack(padx=20, pady=(0, 8))

        self._arctop_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(arctop_card,
                        text="ArcTop headphones are connected and streaming",
                        variable=self._arctop_var, font=("Helvetica", 14, "bold"),
                        text_color=C_SUCCESS,
                        ).pack(pady=(4, 14))

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
        # validate
        if not self._arctop_var.get():
            self._suuvi_status.configure(
                text="Please confirm ArcTop headphones are connected.",
                text_color=C_DANGER)
            return
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

        self.arctop_confirmed = True
        self.use_device = False
        self.current_song = os.path.splitext(track)[0]
        self.current_song_file = track
        self.song_duration = 0  # unknown, will end when music stops
        self._run_volume_check()

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
        self.device_triggers_fired = []
        self.session_active = True
        self.trigger_timers = []
        self.playback_start = None
        self.playback_start_utc = None

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

        self._instr_lbl.configure(
            text="Press the clicker (or any key) when you experience chills!",
            text_color=C_MUTED)

        # Frisson: schedule BLE triggers
        if self.mode == "frisson" and self.use_device and self.ble.connected:
            cfg = SONGS.get(self.current_song, {})
            for t in cfg.get("triggers", []):
                fire_at = max(0.0, t - TRIGGER_LEAD_TIME)
                timer = threading.Timer(fire_at, self._fire_trigger, args=[t])
                timer.daemon = True
                timer.start()
                self.trigger_timers.append(timer)

        self._tick_session()

    # ── device dot (Frisson) ─────────────────────────────────────────

    def _refresh_device_dot(self):
        if not self.session_active:
            return
        if self.ble.connected:
            self._device_dot.configure(
                text=f"Device: {self.ble.device_name}", text_color=C_SUCCESS)
        else:
            self._device_dot.configure(
                text="Device: DISCONNECTED", text_color=C_DANGER)
        self.after(3000, self._refresh_device_dot)

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

    # ── trigger via BLE (Frisson only) ───────────────────────────────

    def _fire_trigger(self, planned):
        if not self.session_active:
            return
        ok, _msg = self.ble.send(BLE_SESSION_PACKET)
        actual = (time.time() - self.playback_start) if self.playback_start else planned
        self.device_triggers_fired.append({
            "planned_sec": planned, "actual_sec": round(actual, 3),
            "utc": _utc_now(), "success": ok})

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
        else:
            data["device_used"] = self.use_device
            data["device_triggers_planned"] = SONGS.get(self.current_song, {}).get("triggers", [])
            data["device_triggers_fired"] = self.device_triggers_fired

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
        self.ble.stop()
        with contextlib.suppress(Exception):
            pygame.mixer.quit()
        self.destroy()

    def _emergency_save(self):
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
            "note": "Session interrupted — partial data",
        }
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
        fname = f"PARTIAL_{now}_P{self.participant_number:03d}_{self.current_song}.json"
        with contextlib.suppress(Exception):
            with open(os.path.join(DATA_DIR, fname), "w") as f:
                json.dump(data, f, indent=2)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
if __name__ == "__main__":
    app = ChillsDemoApp()
    app.mainloop()
