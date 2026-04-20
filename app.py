#!/usr/bin/env python3
"""
ChillsDemo — GUI station for running chills / ASMR experiments
with Frisson haptic-device integration.

Based on E4002 by the Institute for Advanced Consciousness.

Launch:
    python app.py

The app automatically starts the WebSocket relay server (fr_server.py),
connects to it, and guides you through device pairing, stimulus playback,
chills capture, and data saving.
"""

import contextlib
import json
import os
import queue
import random
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime

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
    import websocket  # websocket-client
except ImportError:
    _missing.append("websocket-client")

if _missing:
    print("Missing dependencies: " + ", ".join(_missing))
    print("Install them with:  pip install -r requirements.txt")
    sys.exit(1)

# ── Configuration ────────────────────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STIMULI_DIR = os.path.join(BASE_DIR, "Stimuli")
DATA_DIR = os.path.join(BASE_DIR, "Data")
SERVER_SCRIPT = os.path.join(BASE_DIR, "fr_server.py")
WS_URL = "ws://localhost:8766"
FRISSON_WEBAPP = "https://frissoniacs.github.io/"

# Song definitions from E4002_RunExperiment.py
# triggers = seconds into the track where the Frisson device fires
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

# ── Colours ──────────────────────────────────────────────────────────────

C_PRIMARY = "#e94560"
C_SUCCESS = "#4ecca3"
C_WARNING = "#f39c12"
C_DANGER = "#e74c3c"
C_MUTED = "#7f8c9b"
C_ACCENT = "#0f3460"
C_CARD = "#16213e"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Frisson WebSocket Client
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class FrissonClient:
    """Persistent, auto-reconnecting WebSocket connection to fr_server.py.

    Tracks three independent statuses by listening to messages relayed
    through the server:

    * **server_connected** – our WS link to fr_server.py is alive.
    * **webapp_connected** – the Frisson browser page has sent
      ``FW_Frisson_Hello`` through the server (page loaded + WS open).
    * **device_verified** / **device_last_verified** – the webapp echoed
      ``FW_Frisson_Trigger`` after a successful BLE write, proving the
      physical device is paired and responsive.  Resets on WS disconnect.
    """

    def __init__(self):
        self.ws: websocket.WebSocketApp | None = None
        self.server_connected = False
        self.webapp_connected = False      # got FW_Frisson_Hello
        self.device_verified = False       # got FW_Frisson_Trigger
        self.device_last_verified: float | None = None  # time.time()
        self._client_count = 0
        self._running = True
        self._thread: threading.Thread | None = None

    # ── lifecycle ─────────────────────────────────────────────────────

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self.ws:
            with contextlib.suppress(Exception):
                self.ws.close()

    # ── internal reconnect loop ──────────────────────────────────────

    def _loop(self):
        while self._running:
            try:
                self.ws = websocket.WebSocketApp(
                    WS_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_close=self._on_close,
                    on_error=self._on_error,
                )
                self.ws.run_forever(ping_interval=10, ping_timeout=5)
            except Exception:
                pass
            self.server_connected = False
            self.webapp_connected = False
            self.device_verified = False
            self._client_count = 0
            if self._running:
                time.sleep(2)

    # ── callbacks ────────────────────────────────────────────────────

    def _on_open(self, ws):
        self.server_connected = True
        with contextlib.suppress(Exception):
            ws.send("status")

    def _on_message(self, ws, message):
        # ── plain-string messages relayed from the Frisson webapp ────
        if message == "FW_Frisson_Hello":
            self.webapp_connected = True
            return
        if message == "FW_Frisson_Trigger":
            # The webapp confirmed a successful BLE write to the device.
            self.webapp_connected = True  # also confirms webapp is alive
            self.device_verified = True
            self.device_last_verified = time.time()
            return

        # ── JSON status messages from our server ─────────────────────
        try:
            data = json.loads(message)
            if data.get("type") == "status":
                count = data.get("clients", 1)
                # 2+ clients = at least one other client besides us (the webapp)
                if count >= 2:
                    self.webapp_connected = True
                elif self._client_count >= 2:
                    # Count dropped — webapp disconnected
                    self.webapp_connected = False
                    self.device_verified = False
                self._client_count = count
        except (json.JSONDecodeError, TypeError):
            pass

    def _on_close(self, ws, status_code=None, msg=None):
        self.server_connected = False
        self.webapp_connected = False
        self.device_verified = False
        self._client_count = 0

    def _on_error(self, ws, error):
        pass  # reconnect loop handles it

    # ── public API ───────────────────────────────────────────────────

    def trigger_device(self) -> bool:
        if self.ws and self.server_connected:
            with contextlib.suppress(Exception):
                self.ws.send("trigger_device")
                return True
        return False

    def request_status(self):
        if self.ws and self.server_connected:
            with contextlib.suppress(Exception):
                self.ws.send("status")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Main Application
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class ChillsDemoApp(ctk.CTk):

    def __init__(self):
        super().__init__()

        self.title("Chills Demo Station")
        self.geometry("1000x780")
        self.minsize(850, 650)

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        # ── state ────────────────────────────────────────────────────
        self.server_process: subprocess.Popen | None = None
        self.frisson = FrissonClient()
        self.session_active = False
        self.chills_reports: list[dict] = []
        self.current_song: str | None = None
        self.song_duration = 0
        self.playback_start: float | None = None
        self.trigger_timers: list[threading.Timer] = []
        self.device_triggers_fired: list[dict] = []
        self.participant_number = 1
        self.use_device = True
        self._session_update_id: str | None = None
        self._key_bind_id: str | None = None

        # ── audio ────────────────────────────────────────────────────
        pygame.mixer.init(frequency=44100)

        # ── directories ──────────────────────────────────────────────
        os.makedirs(STIMULI_DIR, exist_ok=True)
        os.makedirs(DATA_DIR, exist_ok=True)

        # ── layout ───────────────────────────────────────────────────
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self.page_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.page_frame.grid(row=0, column=0, sticky="nsew", padx=30, pady=(20, 5))

        self.status_bar = ctk.CTkFrame(self, height=44, corner_radius=0)
        self.status_bar.grid(row=1, column=0, sticky="ew")
        self._build_status_bar()

        # ── services ─────────────────────────────────────────────────
        self._start_server()
        self.after(800, self.frisson.start)

        # ── participant counter ──────────────────────────────────────
        self.participant_number = self._next_participant_number()

        # ── go ───────────────────────────────────────────────────────
        self.show_connection_page()
        self._poll_status()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  Status Bar
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _build_status_bar(self):
        self.status_bar.grid_columnconfigure(4, weight=1)
        pad = {"padx": (16, 18), "pady": 10}

        self.lbl_server = ctk.CTkLabel(
            self.status_bar, text="Server: --", font=("Helvetica", 12)
        )
        self.lbl_server.grid(row=0, column=0, **pad)

        self.lbl_webapp = ctk.CTkLabel(
            self.status_bar, text="Webapp: --", font=("Helvetica", 12)
        )
        self.lbl_webapp.grid(row=0, column=1, **pad)

        self.lbl_device = ctk.CTkLabel(
            self.status_bar, text="Device: --", font=("Helvetica", 12)
        )
        self.lbl_device.grid(row=0, column=2, **pad)

        # right-aligned session counter
        self.lbl_sessions = ctk.CTkLabel(
            self.status_bar, text="Sessions saved: 0", font=("Helvetica", 12),
            text_color=C_MUTED,
        )
        self.lbl_sessions.grid(row=0, column=5, padx=(0, 16), pady=10)

    def _poll_status(self):
        """Called every 2 s to refresh the three status-bar indicators."""

        # ── 1. Server (our WebSocket link to fr_server.py) ───────────
        if self.frisson.server_connected:
            self.lbl_server.configure(text="Server: Running", text_color=C_SUCCESS)
        else:
            self.lbl_server.configure(text="Server: Waiting...", text_color=C_WARNING)

        # ── 2. Webapp (Frisson browser page connected via WS) ────────
        if self.frisson.webapp_connected:
            self.lbl_webapp.configure(text="Webapp: Connected", text_color=C_SUCCESS)
        elif self.frisson.server_connected:
            self.lbl_webapp.configure(text="Webapp: Not connected", text_color=C_WARNING)
        else:
            self.lbl_webapp.configure(text="Webapp: --", text_color=C_MUTED)

        # ── 3. Device (BLE-verified via FW_Frisson_Trigger echo) ─────
        if self.frisson.device_verified:
            age = ""
            if self.frisson.device_last_verified:
                secs = int(time.time() - self.frisson.device_last_verified)
                if secs < 60:
                    age = f" ({secs}s ago)"
                else:
                    age = f" ({secs // 60}m ago)"
            self.lbl_device.configure(
                text=f"Device: Verified{age}", text_color=C_SUCCESS)
        elif self.frisson.webapp_connected:
            self.lbl_device.configure(
                text="Device: Not tested — press Test Trigger",
                text_color=C_WARNING)
        else:
            self.lbl_device.configure(text="Device: --", text_color=C_MUTED)

        # ── session file count ───────────────────────────────────────
        try:
            n = len([f for f in os.listdir(DATA_DIR) if f.endswith(".json")])
        except OSError:
            n = 0
        self.lbl_sessions.configure(text=f"Sessions saved: {n}")

        # ask server for fresh client counts
        self.frisson.request_status()

        self.after(2000, self._poll_status)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  Server Management
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _start_server(self):
        if self.server_process and self.server_process.poll() is None:
            return
        # Kill any orphaned server still holding the port from a prior run
        with contextlib.suppress(Exception):
            subprocess.run(
                ["lsof", "-ti", "tcp:8766"],
                capture_output=True, text=True, timeout=3,
            )
            result = subprocess.run(
                ["lsof", "-ti", "tcp:8766"],
                capture_output=True, text=True, timeout=3,
            )
            for pid in result.stdout.strip().split("\n"):
                if pid.strip():
                    subprocess.run(["kill", pid.strip()], timeout=3)
            time.sleep(0.3)
        try:
            self.server_process = subprocess.Popen(
                [sys.executable, SERVER_SCRIPT],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except Exception as exc:
            print(f"[App] Failed to start server: {exc}")

    def _stop_server(self):
        if self.server_process:
            self.server_process.terminate()
            try:
                self.server_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.server_process.kill()

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

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  PAGE 1 — Connection Setup
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def show_connection_page(self):
        self._clear_page()

        # title
        ctk.CTkLabel(
            self.page_frame, text="Chills Demo Station",
            font=("Helvetica", 34, "bold"),
        ).pack(pady=(30, 2))
        ctk.CTkLabel(
            self.page_frame, text="Device Connection Setup",
            font=("Helvetica", 16), text_color=C_MUTED,
        ).pack(pady=(0, 25))

        # instructions card
        card = ctk.CTkFrame(self.page_frame, corner_radius=12)
        card.pack(fill="x", padx=50, pady=10)

        steps = [
            "The WebSocket relay server starts automatically in the background.",
            "Click 'Open Frisson Webapp' to launch it in your browser.",
            "In the webapp: tap Connect, select your Frisson device, and pair it.",
            "Set all intensity sliders (P1, P2, P3) to maximum (255).",
            "Click 'Test Trigger' — Device status turns green when the hardware responds.",
        ]
        for i, text in enumerate(steps):
            row = ctk.CTkFrame(card, fg_color="transparent")
            row.pack(fill="x", padx=20, pady=(12 if i == 0 else 3, 12 if i == len(steps) - 1 else 3))
            ctk.CTkLabel(
                row, text=f"{i + 1}.", font=("Helvetica", 14, "bold"),
                text_color=C_PRIMARY, width=28,
            ).pack(side="left")
            ctk.CTkLabel(
                row, text=text, font=("Helvetica", 14),
                wraplength=620, anchor="w", justify="left",
            ).pack(side="left", padx=(6, 0))

        # action buttons
        btn_row = ctk.CTkFrame(self.page_frame, fg_color="transparent")
        btn_row.pack(pady=20)

        ctk.CTkButton(
            btn_row, text="Open Frisson Webapp",
            font=("Helvetica", 15, "bold"), width=230, height=44,
            fg_color=C_ACCENT, hover_color="#1a4a7a",
            command=lambda: webbrowser.open(FRISSON_WEBAPP),
        ).pack(side="left", padx=10)

        self._test_btn = ctk.CTkButton(
            btn_row, text="Test Trigger",
            font=("Helvetica", 15, "bold"), width=160, height=44,
            fg_color="#2d3a4a", hover_color="#3d4a5a",
            command=self._test_trigger,
        )
        self._test_btn.pack(side="left", padx=10)

        # skip-device option
        self._skip_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            self.page_frame, text="Run without Frisson device",
            variable=self._skip_var, font=("Helvetica", 13),
            text_color=C_MUTED,
        ).pack(pady=(8, 18))

        # continue
        ctk.CTkButton(
            self.page_frame, text="Continue to Session Setup",
            font=("Helvetica", 17, "bold"), width=300, height=52,
            fg_color=C_SUCCESS, hover_color="#3ba882", text_color="#000",
            command=self._go_to_setup,
        ).pack(pady=5)

    def _test_trigger(self):
        # Clear previous verification so we can detect a fresh echo
        prev_verified = self.frisson.device_verified
        self.frisson.device_verified = False
        sent = self.frisson.trigger_device()
        if not sent:
            self.frisson.device_verified = prev_verified  # restore
            self._test_btn.configure(text="Send failed", fg_color=C_DANGER)
            self.after(1500, lambda: self._test_btn.configure(
                text="Test Trigger", fg_color="#2d3a4a"))
            return
        self._test_btn.configure(text="Sent...", fg_color=C_WARNING)
        # Check for FW_Frisson_Trigger echo over the next 3 seconds
        self._test_trigger_check(attempts=6)

    def _test_trigger_check(self, attempts: int):
        if self.frisson.device_verified:
            self._test_btn.configure(text="Device OK!", fg_color=C_SUCCESS)
            self.after(2000, lambda: self._test_btn.configure(
                text="Test Trigger", fg_color="#2d3a4a"))
            return
        if attempts <= 0:
            # Trigger was sent to server but no BLE confirmation came back.
            # Device may not be paired, or webapp didn't relay the echo.
            self._test_btn.configure(
                text="Sent (no device echo)", fg_color=C_WARNING)
            self.after(2500, lambda: self._test_btn.configure(
                text="Test Trigger", fg_color="#2d3a4a"))
            return
        self.after(500, lambda: self._test_trigger_check(attempts - 1))

    def _go_to_setup(self):
        self.use_device = not self._skip_var.get()
        self.show_setup_page()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  PAGE 2 — Session Setup
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def show_setup_page(self):
        self._clear_page()

        ctk.CTkLabel(
            self.page_frame, text="Session Setup",
            font=("Helvetica", 30, "bold"),
        ).pack(pady=(35, 28))

        # participant #
        p_row = ctk.CTkFrame(self.page_frame, fg_color="transparent")
        p_row.pack(pady=8)
        ctk.CTkLabel(
            p_row, text="Participant #:", font=("Helvetica", 16),
        ).pack(side="left", padx=(0, 10))
        self._p_entry = ctk.CTkEntry(p_row, width=80, font=("Helvetica", 16), justify="center")
        self._p_entry.insert(0, str(self.participant_number))
        self._p_entry.pack(side="left")

        # song selection
        ctk.CTkLabel(
            self.page_frame, text="Select Stimulus:",
            font=("Helvetica", 16), text_color=C_MUTED,
        ).pack(pady=(28, 10))

        self._song_var = ctk.StringVar(value="Random")
        song_frame = ctk.CTkFrame(self.page_frame, fg_color="transparent")
        song_frame.pack()
        for opt in ["Random"] + list(SONGS.keys()):
            ctk.CTkRadioButton(
                song_frame, text=opt, variable=self._song_var, value=opt,
                font=("Helvetica", 15),
            ).pack(anchor="w", padx=50, pady=5)

        # audio-file check
        self._audio_lbl = ctk.CTkLabel(
            self.page_frame, text="", font=("Helvetica", 12),
        )
        self._audio_lbl.pack(pady=(18, 0))
        self._check_audio()

        # start
        ctk.CTkButton(
            self.page_frame, text="Start Session",
            font=("Helvetica", 18, "bold"), width=260, height=55,
            fg_color=C_PRIMARY, hover_color="#c93a52",
            command=self._prepare_session,
        ).pack(pady=28)

        # nav
        ctk.CTkButton(
            self.page_frame, text="Back to Device Setup",
            font=("Helvetica", 13), width=200, height=34,
            fg_color="transparent", hover_color="#2a2a3e", text_color=C_MUTED,
            command=self.show_connection_page,
        ).pack()

    def _check_audio(self):
        missing = [
            cfg["file"] for cfg in SONGS.values()
            if not os.path.exists(os.path.join(STIMULI_DIR, cfg["file"]))
        ]
        if missing:
            self._audio_lbl.configure(
                text=f"Missing in Stimuli/: {', '.join(missing)}",
                text_color=C_WARNING,
            )
        else:
            self._audio_lbl.configure(text="All audio files found", text_color=C_SUCCESS)

    def _prepare_session(self):
        # validate participant #
        try:
            self.participant_number = int(self._p_entry.get())
        except ValueError:
            self._p_entry.configure(border_color=C_DANGER)
            return

        # pick song
        choice = self._song_var.get()
        self.current_song = (
            random.choice(list(SONGS.keys())) if choice == "Random" else choice
        )

        # check file exists
        cfg = SONGS[self.current_song]
        path = os.path.join(STIMULI_DIR, cfg["file"])
        if not os.path.exists(path):
            self._audio_lbl.configure(
                text=f"File not found: Stimuli/{cfg['file']}", text_color=C_DANGER,
            )
            return

        self.song_duration = cfg["duration_est"]
        self.show_session_page()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  PAGE 3 — Running Session
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def show_session_page(self):
        self._clear_page()

        # reset per-session state
        self.chills_reports = []
        self.device_triggers_fired = []
        self.session_active = True
        self.trigger_timers = []

        # header
        ctk.CTkLabel(
            self.page_frame, text=f"Now Playing: {self.current_song}",
            font=("Helvetica", 28, "bold"),
        ).pack(pady=(35, 4))
        ctk.CTkLabel(
            self.page_frame, text=f"Participant #{self.participant_number}",
            font=("Helvetica", 14), text_color=C_MUTED,
        ).pack(pady=(0, 28))

        # device indicator (only when using device)
        if self.use_device:
            self._device_dot = ctk.CTkLabel(
                self.page_frame, text="", font=("Helvetica", 12),
            )
            self._device_dot.pack(pady=(0, 6))
            self._refresh_device_dot()

        # timer + progress
        self._timer_lbl = ctk.CTkLabel(
            self.page_frame,
            text=f"0:00 / {self._fmt(self.song_duration)}",
            font=("Helvetica", 22),
        )
        self._timer_lbl.pack(pady=(0, 8))

        self._progress = ctk.CTkProgressBar(
            self.page_frame, width=500, height=14,
            progress_color=C_PRIMARY,
        )
        self._progress.set(0)
        self._progress.pack(pady=(0, 35))

        # chills counter
        ctk.CTkLabel(
            self.page_frame, text="Chills Reported",
            font=("Helvetica", 14), text_color=C_MUTED,
        ).pack()
        self._chills_lbl = ctk.CTkLabel(
            self.page_frame, text="0",
            font=("Helvetica", 80, "bold"),
        )
        self._chills_lbl.pack(pady=(2, 8))

        self._instr_lbl = ctk.CTkLabel(
            self.page_frame,
            text="Press the clicker (or any key) when you experience chills!",
            font=("Helvetica", 15), text_color=C_MUTED,
        )
        self._instr_lbl.pack(pady=(0, 30))

        # stop
        ctk.CTkButton(
            self.page_frame, text="Stop Session",
            font=("Helvetica", 14, "bold"), width=160, height=40,
            fg_color=C_DANGER, hover_color="#c0392b",
            command=self._abort_session,
        ).pack()

        # key capture
        self._key_bind_id = self.bind("<KeyPress>", self._on_key)

        # start audio
        self._start_playback()

    # ── device dot (on session page) ─────────────────────────────────

    def _refresh_device_dot(self):
        if not self.session_active:
            return
        if self.frisson.device_verified:
            self._device_dot.configure(
                text="Device: verified", text_color=C_SUCCESS)
        elif self.frisson.webapp_connected:
            self._device_dot.configure(
                text="Device: webapp connected, BLE not yet verified",
                text_color=C_WARNING)
        else:
            self._device_dot.configure(
                text="Device: NOT connected — check Frisson webapp & re-pair",
                text_color=C_DANGER)
        self.after(3000, self._refresh_device_dot)

    # ── playback ─────────────────────────────────────────────────────

    def _start_playback(self):
        cfg = SONGS[self.current_song]
        path = os.path.join(STIMULI_DIR, cfg["file"])
        try:
            pygame.mixer.music.load(path)
            pygame.mixer.music.play()
        except Exception as exc:
            self._instr_lbl.configure(
                text=f"Audio error: {exc}", text_color=C_DANGER)
            return

        self.playback_start = time.time()

        # schedule device triggers
        if self.use_device:
            for t in cfg["triggers"]:
                timer = threading.Timer(t, self._fire_trigger, args=[t])
                timer.daemon = True
                timer.start()
                self.trigger_timers.append(timer)

        # start progress loop
        self._tick_session()

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
            "time_seconds": round(elapsed, 3),
            "key": event.keysym,
        })

        # flash counter
        n = len(self.chills_reports)
        self._chills_lbl.configure(text=str(n), text_color=C_PRIMARY)
        self.after(180, lambda: self._chills_lbl.configure(text_color=("gray90", "gray90")))

    # ── trigger ──────────────────────────────────────────────────────

    def _fire_trigger(self, planned: float):
        if not self.session_active:
            return
        ok = self.frisson.trigger_device()
        actual = (time.time() - self.playback_start) if self.playback_start else planned
        self.device_triggers_fired.append({
            "planned_sec": planned,
            "actual_sec": round(actual, 3),
            "success": ok,
        })

    # ── session timer / progress ─────────────────────────────────────

    def _tick_session(self):
        if not self.session_active:
            return

        # check if music finished
        if not pygame.mixer.music.get_busy() and self.playback_start is not None:
            # small grace period so final notes aren't cut off
            elapsed = time.time() - self.playback_start
            if elapsed > 5:
                self._end_session()
                return

        elapsed = time.time() - self.playback_start if self.playback_start else 0
        frac = min(elapsed / self.song_duration, 1.0) if self.song_duration else 0
        self._timer_lbl.configure(
            text=f"{self._fmt(elapsed)} / {self._fmt(self.song_duration)}")
        self._progress.set(frac)

        self._session_update_id = self.after(200, self._tick_session)

    # ── end / abort ──────────────────────────────────────────────────

    def _end_session(self):
        if not self.session_active:
            return
        self.session_active = False

        # unbind keys
        if self._key_bind_id:
            self.unbind("<KeyPress>", self._key_bind_id)
            self._key_bind_id = None

        # cancel timers
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
    #  PAGE 4 — Post-Session Survey
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def show_post_session_page(self):
        self._clear_page()

        elapsed = (time.time() - self.playback_start) if self.playback_start else 0

        ctk.CTkLabel(
            self.page_frame, text="Session Complete",
            font=("Helvetica", 30, "bold"),
        ).pack(pady=(35, 22))

        # summary card
        card = ctk.CTkFrame(self.page_frame, corner_radius=10)
        card.pack(fill="x", padx=80, pady=8)

        for lbl, val in [
            ("Song", self.current_song),
            ("Duration", self._fmt(elapsed)),
            ("Chills reported", str(len(self.chills_reports))),
        ]:
            row = ctk.CTkFrame(card, fg_color="transparent")
            row.pack(fill="x", padx=24, pady=7)
            ctk.CTkLabel(
                row, text=f"{lbl}:", font=("Helvetica", 14),
                text_color=C_MUTED, width=150, anchor="e",
            ).pack(side="left")
            ctk.CTkLabel(
                row, text=val, font=("Helvetica", 14, "bold"), anchor="w",
            ).pack(side="left", padx=(10, 0))

        # questions
        q = ctk.CTkFrame(self.page_frame, fg_color="transparent")
        q.pack(fill="x", padx=80, pady=(18, 0))

        # Q1 — experienced chills?
        ctk.CTkLabel(
            q, text="Did you experience chills?", font=("Helvetica", 16),
        ).pack(pady=(8, 8))
        self._yn_var = ctk.StringVar(value="Yes")
        yn = ctk.CTkFrame(q, fg_color="transparent")
        yn.pack()
        ctk.CTkRadioButton(
            yn, text="Yes", variable=self._yn_var, value="Yes",
            font=("Helvetica", 14),
        ).pack(side="left", padx=16)
        ctk.CTkRadioButton(
            yn, text="No", variable=self._yn_var, value="No",
            font=("Helvetica", 14),
        ).pack(side="left", padx=16)

        # Q2 — intensity
        ctk.CTkLabel(
            q, text="How intense were the chills?  (1 = mild, 10 = very intense)",
            font=("Helvetica", 16),
        ).pack(pady=(22, 8))

        sl_row = ctk.CTkFrame(q, fg_color="transparent")
        sl_row.pack()
        ctk.CTkLabel(sl_row, text="1", font=("Helvetica", 12), text_color=C_MUTED).pack(side="left", padx=(0, 6))
        self._intensity_var = ctk.DoubleVar(value=5)
        ctk.CTkSlider(
            sl_row, from_=1, to=10, number_of_steps=9,
            variable=self._intensity_var, width=320,
            command=self._update_intensity_lbl,
        ).pack(side="left")
        ctk.CTkLabel(sl_row, text="10", font=("Helvetica", 12), text_color=C_MUTED).pack(side="left", padx=(6, 0))

        self._int_lbl = ctk.CTkLabel(
            q, text="5", font=("Helvetica", 24, "bold"), text_color=C_PRIMARY,
        )
        self._int_lbl.pack(pady=4)

        # save
        ctk.CTkButton(
            self.page_frame, text="Save & Next Participant",
            font=("Helvetica", 17, "bold"), width=300, height=52,
            fg_color=C_SUCCESS, hover_color="#3ba882", text_color="#000",
            command=self._save_and_next,
        ).pack(pady=(22, 10))

    def _update_intensity_lbl(self, val):
        self._int_lbl.configure(text=str(int(float(val))))

    def _save_and_next(self):
        elapsed = (time.time() - self.playback_start) if self.playback_start else 0

        data = {
            "session_id": (
                f"{datetime.now().strftime('%Y-%m-%d_%H%M%S')}"
                f"_P{self.participant_number:03d}"
            ),
            "participant_id": self.participant_number,
            "timestamp": datetime.now().isoformat(),
            "song": self.current_song,
            "song_file": SONGS[self.current_song]["file"],
            "duration_seconds": round(elapsed, 2),
            "device_used": self.use_device,
            "device_triggers_planned": SONGS[self.current_song]["triggers"],
            "device_triggers_fired": self.device_triggers_fired,
            "chills_reports": self.chills_reports,
            "total_chills_count": len(self.chills_reports),
            "post_survey": {
                "experienced_chills": self._yn_var.get() == "Yes",
                "chills_intensity": int(self._intensity_var.get()),
            },
        }

        now = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        fname = f"session_{now}_P{self.participant_number:03d}_{self.current_song}.json"
        fpath = os.path.join(DATA_DIR, fname)

        try:
            with open(fpath, "w") as f:
                json.dump(data, f, indent=2)
        except OSError as exc:
            print(f"[App] Save error: {exc}")

        # advance
        self.participant_number += 1
        self.show_setup_page()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  Cleanup
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def _on_close(self):
        # if a session is in-flight, auto-save what we have
        if self.session_active and self.playback_start:
            self.session_active = False
            with contextlib.suppress(Exception):
                pygame.mixer.music.stop()
            self._emergency_save()

        for t in self.trigger_timers:
            t.cancel()
        self.frisson.stop()
        self._stop_server()
        with contextlib.suppress(Exception):
            pygame.mixer.quit()
        self.destroy()

    def _emergency_save(self):
        """Best-effort save of partial session data on unexpected close."""
        elapsed = (time.time() - self.playback_start) if self.playback_start else 0
        data = {
            "session_id": f"PARTIAL_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}_P{self.participant_number:03d}",
            "participant_id": self.participant_number,
            "timestamp": datetime.now().isoformat(),
            "song": self.current_song,
            "song_file": SONGS[self.current_song]["file"] if self.current_song else "unknown",
            "duration_seconds": round(elapsed, 2),
            "device_used": self.use_device,
            "device_triggers_planned": SONGS[self.current_song]["triggers"] if self.current_song else [],
            "device_triggers_fired": self.device_triggers_fired,
            "chills_reports": self.chills_reports,
            "total_chills_count": len(self.chills_reports),
            "post_survey": None,
            "note": "Session was interrupted — partial data",
        }
        now = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        fname = f"PARTIAL_{now}_P{self.participant_number:03d}_{self.current_song}.json"
        with contextlib.suppress(Exception):
            with open(os.path.join(DATA_DIR, fname), "w") as f:
                json.dump(data, f, indent=2)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Entry point
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

if __name__ == "__main__":
    app = ChillsDemoApp()
    app.mainloop()
