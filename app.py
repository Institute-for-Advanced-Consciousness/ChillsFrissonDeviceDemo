#!/usr/bin/env python3
"""
ChillsDemo — GUI station for running chills / ASMR experiments
with direct Frisson haptic-device BLE control.

Based on E4002 by the Institute for Advanced Consciousness.

Launch:
    source venv/bin/activate && python app.py
"""

import asyncio
import contextlib
import json
import os
import random
import subprocess
import sys
import threading
import time
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
DATA_DIR = os.path.join(BASE_DIR, "Data")

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
#
# Service UUID for the RFduino-based Frisson device.
FRISSON_SERVICE_UUID = "00002220-0000-1000-8000-00805f9b34fb"
#
# BLE packet format (13 bytes):
#   [cmd, P1_str, P2_str, P3_str, M1_str,
#    P1_start, P2_start, P3_start, M1_start,
#    P1_stop,  P2_stop,  P3_stop,  M1_stop]
# Times are in 0.1 s units (e.g., 30 = 3.0 seconds).
#
# Test packet: all 3 peltiers at max, simultaneous, 3 seconds.
BLE_TEST_PACKET = bytes([20, 255, 255, 255, 0, 0, 0, 0, 0, 30, 30, 30, 0])
#
# Session trigger packet — cascading wave P3 → P2 → P1:
#   P3 fires immediately  (start=0,  stop=30  → 0.0–3.0 s)
#   P2 fires at +0.3 s    (start=3,  stop=33  → 0.3–3.3 s)
#   P1 fires at +0.5 s    (start=5,  stop=35  → 0.5–3.5 s)
# The BLE write is scheduled 0.25 s before the nominal trigger time,
# so relative to the music: P3 @ T−0.25, P2 ≈ T, P1 @ T+0.25.
BLE_SESSION_PACKET = bytes([20, 255, 255, 255, 0, 5, 3, 0, 0, 35, 33, 30, 0])
TRIGGER_LEAD_TIME = 0.25  # send the packet this many seconds early

# ── Colours ──────────────────────────────────────────────────────────────

C_PRIMARY = "#e94560"
C_SUCCESS = "#4ecca3"
C_WARNING = "#f39c12"
C_DANGER = "#e74c3c"
C_MUTED = "#7f8c9b"
C_ACCENT = "#0f3460"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  FrissonBLE — persistent direct BLE connection
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class FrissonBLE:
    """Manages a persistent BLE connection to the Frisson device.

    All public methods are **blocking** and must be called from a
    worker thread, never the main/tkinter thread.  The class runs
    its own asyncio event loop in a background thread so that the
    BleakClient's connection stays alive between calls.
    """

    def __init__(self):
        self.connected = False
        self.device_name = ""
        self.device_rssi: int | None = None
        self.services_info: list[dict] = []   # populated on connect
        self.notifications: list[dict] = []   # data received from device
        self._client: BleakClient | None = None
        self._write_uuid: str | None = None
        self._write_response: bool = True  # use write-with-response by default
        self._notify_uuid: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self.on_disconnect_callback = None    # set by the app

    # ── lifecycle ─────────────────────────────────────────────────────

    def start(self):
        """Spin up the background asyncio event loop."""
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True,
        )
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

    # ── blocking public API (call from worker threads) ────────────────

    def connect(self) -> tuple[bool, str]:
        return self._submit(self._do_connect())

    def disconnect(self) -> tuple[bool, str]:
        return self._submit(self._safe_disconnect())

    def send(self, packet: bytes) -> tuple[bool, str]:
        return self._submit(self._do_write(packet))

    def send_and_listen(self, packet: bytes, wait: float = 4.0) -> tuple[bool, str, list[bytes]]:
        """Send a packet, then collect any notifications for *wait* seconds."""
        return self._submit(self._do_write_and_listen(packet, wait), timeout=wait + 5)

    def _submit(self, coro, timeout=15):
        if not self._loop:
            return False, "BLE not started"
        try:
            return asyncio.run_coroutine_threadsafe(
                coro, self._loop,
            ).result(timeout=timeout)
        except Exception as exc:
            return False, str(exc)[:80]

    # ── async internals ──────────────────────────────────────────────

    async def _do_connect(self) -> tuple[bool, str]:
        await self._safe_disconnect()
        self.services_info = []
        self.notifications = []

        device = await BleakScanner.find_device_by_filter(
            lambda _d, ad: FRISSON_SERVICE_UUID.lower()
            in [s.lower() for s in (ad.service_uuids or [])],
            timeout=6.0,
        )
        if not device:
            return False, "Device not found — is it powered on?"

        self.device_rssi = getattr(device, "rssi", None)

        self._client = BleakClient(
            device, timeout=8.0,
            disconnected_callback=self._on_disconnect,
        )
        await self._client.connect()

        # ── enumerate every service & characteristic ────────────
        frisson_chars = []  # ordered list for the target service
        for svc in self._client.services:
            svc_info = {
                "uuid": svc.uuid,
                "characteristics": [],
            }
            for ch in svc.characteristics:
                ch_info = {
                    "uuid": ch.uuid,
                    "properties": list(ch.properties),
                    "descriptors": [d.uuid for d in ch.descriptors],
                }
                svc_info["characteristics"].append(ch_info)

                if svc.uuid.lower() == FRISSON_SERVICE_UUID.lower():
                    frisson_chars.append(ch)
                    if "notify" in ch.properties or "indicate" in ch.properties:
                        self._notify_uuid = ch.uuid

            self.services_info.append(svc_info)

        # The webapp (sketch_rfduino.js) uses characteristics[1] as the
        # write target.  Match that index so we hit the correct one.
        if len(frisson_chars) >= 2:
            target = frisson_chars[1]
            self._write_uuid = target.uuid
            self._write_response = "write" in target.properties
        else:
            # Fallback: pick any writable characteristic
            for ch in frisson_chars:
                if "write" in ch.properties or "write-without-response" in ch.properties:
                    self._write_uuid = ch.uuid
                    self._write_response = "write" in ch.properties
                    break

        if not self._write_uuid:
            await self._client.disconnect()
            return False, "Write characteristic not found"

        # ── subscribe to ALL notify/indicate chars across all services
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
            self._notify_uuid = self._notify_uuids[0]  # for status display

        self.connected = True
        self.device_name = device.name or "Frisson"
        return True, self.device_name

    def _on_notify(self, _sender, data: bytearray):
        """Called by bleak whenever the device pushes a notification."""
        self.notifications.append({
            "time": time.time(),
            "data": bytes(data),
            "hex": data.hex(),
        })

    async def _do_write(self, packet: bytes) -> tuple[bool, str]:
        if not self._client or not self._client.is_connected:
            self.connected = False
            return False, "Not connected"
        try:
            await self._client.write_gatt_char(
                self._write_uuid, packet,
                response=self._write_response,
            )
            return True, "OK"
        except Exception as exc:
            self.connected = False
            return False, str(exc)[:80]

    async def _read_all_chars(self) -> list[dict]:
        """Read every readable characteristic and return their values."""
        reads = []
        for svc in self._client.services:
            for ch in svc.characteristics:
                if "read" in ch.properties:
                    try:
                        val = await self._client.read_gatt_char(ch.uuid)
                        reads.append({
                            "service": svc.uuid,
                            "char": ch.uuid,
                            "properties": list(ch.properties),
                            "value": bytes(val),
                            "hex": val.hex(" "),
                            "text": val.decode("utf-8", errors="replace"),
                        })
                    except Exception as exc:
                        reads.append({
                            "service": svc.uuid,
                            "char": ch.uuid,
                            "properties": list(ch.properties),
                            "error": str(exc)[:60],
                        })
        return reads

    async def _do_write_and_listen(
        self, packet: bytes, wait: float,
    ) -> tuple[bool, str, list]:
        """Write a packet, then read everything and collect notifications."""
        if not self._client or not self._client.is_connected:
            self.connected = False
            return False, "Not connected", []
        before = len(self.notifications)

        # Read all chars BEFORE trigger
        reads_before = await self._read_all_chars()

        try:
            await self._client.write_gatt_char(
                self._write_uuid, packet,
                response=self._write_response,
            )
        except Exception as exc:
            self.connected = False
            return False, str(exc)[:80], []

        await asyncio.sleep(wait)

        # Read all chars AFTER trigger
        reads_after = await self._read_all_chars()

        new_notifs = self.notifications[before:]
        report = {
            "notifications": [n["data"] for n in new_notifs],
            "reads_before": reads_before,
            "reads_after": reads_after,
        }
        return True, "OK", report

    async def _safe_disconnect(self) -> tuple[bool, str]:
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
        """Called by bleak when the device disconnects unexpectedly."""
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
        self.geometry("1000x780")
        self.minsize(850, 650)

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        # ── state ────────────────────────────────────────────────────
        self.ble = FrissonBLE()
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
        self.ble.start()
        self.ble.on_disconnect_callback = lambda: self.after(0, self._poll_status)

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
        self.status_bar.grid_columnconfigure(2, weight=1)

        self.lbl_ble = ctk.CTkLabel(
            self.status_bar, text="Device: not connected",
            font=("Helvetica", 12),
        )
        self.lbl_ble.grid(row=0, column=0, padx=(16, 20), pady=10)

        self.lbl_sessions = ctk.CTkLabel(
            self.status_bar, text="Sessions saved: 0",
            font=("Helvetica", 12), text_color=C_MUTED,
        )
        self.lbl_sessions.grid(row=0, column=3, padx=(0, 16), pady=10)

    def _poll_status(self):
        # BLE connection
        if self.ble.connected:
            self.lbl_ble.configure(
                text=f"Device: {self.ble.device_name}", text_color=C_SUCCESS)
        else:
            self.lbl_ble.configure(
                text="Device: not connected", text_color=C_DANGER)

        # session count
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
        """Run *fn* in a background thread; call on_done(result) on
        the main thread when it finishes."""
        def _run():
            result = fn()
            self.after(0, lambda: on_done(result))
        threading.Thread(target=_run, daemon=True).start()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  PAGE 1 — Device Connection
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def show_connection_page(self):
        self._clear_page()

        ctk.CTkLabel(
            self.page_frame, text="Chills Demo Station",
            font=("Helvetica", 34, "bold"),
        ).pack(pady=(30, 2))
        ctk.CTkLabel(
            self.page_frame, text="Device Connection",
            font=("Helvetica", 16), text_color=C_MUTED,
        ).pack(pady=(0, 30))

        # instructions card
        card = ctk.CTkFrame(self.page_frame, corner_radius=12)
        card.pack(fill="x", padx=50, pady=10)
        steps = [
            "Power on the Frisson device.",
            "Click 'Scan & Connect' — the app pairs directly over Bluetooth.",
            "Click 'Test Trigger' to fire all 3 peltiers at full strength for 3 s.",
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

        # connection status / diagnostic output
        self._conn_status = ctk.CTkLabel(
            self.page_frame, text="Not connected",
            font=("Courier", 12), text_color=C_MUTED,
            wraplength=700, justify="left", anchor="w",
        )
        self._conn_status.pack(pady=(20, 12), fill="x", padx=50)

        # buttons
        btn_row = ctk.CTkFrame(self.page_frame, fg_color="transparent")
        btn_row.pack(pady=8)

        self._connect_btn = ctk.CTkButton(
            btn_row, text="Scan & Connect",
            font=("Helvetica", 15, "bold"), width=200, height=44,
            fg_color=C_ACCENT, hover_color="#1a4a7a",
            command=self._on_connect,
        )
        self._connect_btn.pack(side="left", padx=8)

        self._disconnect_btn = ctk.CTkButton(
            btn_row, text="Disconnect",
            font=("Helvetica", 15, "bold"), width=140, height=44,
            fg_color="#2d3a4a", hover_color="#3d4a5a",
            command=self._on_disconnect,
        )
        self._disconnect_btn.pack(side="left", padx=8)

        self._test_btn = ctk.CTkButton(
            btn_row, text="Test Trigger",
            font=("Helvetica", 15, "bold"), width=160, height=44,
            fg_color="#2d3a4a", hover_color="#3d4a5a",
            command=self._on_test,
        )
        self._test_btn.pack(side="left", padx=8)

        # skip device
        self._skip_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            self.page_frame, text="Run without Frisson device",
            variable=self._skip_var, font=("Helvetica", 13),
            text_color=C_MUTED,
        ).pack(pady=(14, 18))

        # continue
        ctk.CTkButton(
            self.page_frame, text="Continue to Session Setup",
            font=("Helvetica", 17, "bold"), width=300, height=52,
            fg_color=C_SUCCESS, hover_color="#3ba882", text_color="#000",
            command=self._go_to_setup,
        ).pack(pady=5)

        # sync status label with current state
        if self.ble.connected:
            self._conn_status.configure(
                text=f"Connected to {self.ble.device_name}",
                text_color=C_SUCCESS)

    # ── connection actions ───────────────────────────────────────────

    def _on_connect(self):
        self._connect_btn.configure(text="Scanning...", fg_color=C_WARNING)
        self._conn_status.configure(text="Scanning for Frisson device...", text_color=C_WARNING)
        self._worker(self.ble.connect, self._connect_done)

    def _connect_done(self, result):
        ok, msg = result
        if ok:
            self._connect_btn.configure(text="Connected", fg_color=C_SUCCESS)
            # Build a rich status string
            parts = [f"Connected to {msg}"]
            if self.ble.device_rssi is not None:
                parts.append(f"RSSI: {self.ble.device_rssi} dBm")
            wr_mode = "w/ response" if self.ble._write_response else "no response"
            parts.append(f"Write: {wr_mode}")
            notify_status = "yes" if self.ble._notify_uuid else "no"
            parts.append(f"Notify: {notify_status}")
            n_svc = len(self.ble.services_info)
            n_chars = sum(len(s["characteristics"]) for s in self.ble.services_info)
            parts.append(f"Services: {n_svc}, Chars: {n_chars}")
            self._conn_status.configure(
                text="  |  ".join(parts), text_color=C_SUCCESS)
            self.after(2000, lambda: self._connect_btn.configure(
                text="Scan & Connect", fg_color=C_ACCENT))
        else:
            self._connect_btn.configure(text="Retry", fg_color=C_DANGER)
            self._conn_status.configure(text=msg, text_color=C_DANGER)
            self.after(2000, lambda: self._connect_btn.configure(
                text="Scan & Connect", fg_color=C_ACCENT))

    def _on_disconnect(self):
        self._worker(self.ble.disconnect, self._disconnect_done)

    def _disconnect_done(self, _result):
        self._conn_status.configure(text="Disconnected", text_color=C_MUTED)

    def _on_test(self):
        if not self.ble.connected:
            self._test_btn.configure(text="Not connected", fg_color=C_DANGER)
            self.after(1500, lambda: self._test_btn.configure(
                text="Test Trigger", fg_color="#2d3a4a"))
            return
        self._test_btn.configure(text="Firing (listening 4 s)...", fg_color=C_WARNING)
        self._worker(
            lambda: self.ble.send_and_listen(BLE_TEST_PACKET, wait=4.0),
            self._test_done,
        )

    def _test_done(self, result):
        ok, msg, report = result
        if ok:
            self._test_btn.configure(text="Device OK!", fg_color=C_SUCCESS)
            lines = ["Write succeeded."]

            notifs = report.get("notifications", [])
            if notifs:
                lines.append(f"\nNotifications ({len(notifs)}):")
                for raw in notifs:
                    lines.append(f"  [{len(raw)}B] {raw.hex(' ')}")
            else:
                n_sub = len(getattr(self.ble, "_notify_uuids", []))
                lines.append(f"No notifications (subscribed to {n_sub} char(s))")

            reads_after = report.get("reads_after", [])
            if reads_after:
                lines.append(f"\nReadable characteristics ({len(reads_after)}):")
                for r in reads_after:
                    if "error" in r:
                        lines.append(f"  {r['char'][:8]}.. err: {r['error']}")
                    else:
                        val_repr = r["text"] if r["value"].isascii() and len(r["value"]) < 30 else r["hex"]
                        lines.append(f"  {r['char'][:8]}.. [{len(r['value'])}B] {val_repr}")

            # Check if any readable values changed after the trigger
            reads_before = report.get("reads_before", [])
            changed = []
            before_map = {r["char"]: r.get("hex", "") for r in reads_before}
            for r in reads_after:
                if r.get("hex", "") != before_map.get(r["char"], ""):
                    changed.append(r["char"][:8])
            if changed:
                lines.append(f"\nChanged after trigger: {', '.join(changed)}")

            self._conn_status.configure(
                text="\n".join(lines), text_color=C_SUCCESS)
        else:
            self._test_btn.configure(text="Failed", fg_color=C_DANGER)
            self._conn_status.configure(
                text=f"Trigger failed: {msg}", text_color=C_DANGER)
        self.after(4000, lambda: self._test_btn.configure(
            text="Test Trigger", fg_color="#2d3a4a"))

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
        self._p_entry = ctk.CTkEntry(
            p_row, width=80, font=("Helvetica", 16), justify="center",
        )
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

        # device warning if not connected
        if self.use_device and not self.ble.connected:
            ctk.CTkLabel(
                self.page_frame, text="Device not connected — go back to connect or check 'run without'",
                font=("Helvetica", 12), text_color=C_WARNING,
            ).pack(pady=(6, 0))

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
                text_color=C_WARNING)
        else:
            self._audio_lbl.configure(
                text="All audio files found", text_color=C_SUCCESS)

    def _prepare_session(self):
        try:
            self.participant_number = int(self._p_entry.get())
        except ValueError:
            self._p_entry.configure(border_color=C_DANGER)
            return

        choice = self._song_var.get()
        self.current_song = (
            random.choice(list(SONGS.keys())) if choice == "Random" else choice
        )

        cfg = SONGS[self.current_song]
        path = os.path.join(STIMULI_DIR, cfg["file"])
        if not os.path.exists(path):
            self._audio_lbl.configure(
                text=f"File not found: Stimuli/{cfg['file']}",
                text_color=C_DANGER)
            return

        self.song_duration = cfg["duration_est"]
        self.show_session_page()

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    #  PAGE 3 — Running Session
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def show_session_page(self):
        self._clear_page()

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

        # BLE status dot (during session)
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

        ctk.CTkButton(
            self.page_frame, text="Stop Session",
            font=("Helvetica", 14, "bold"), width=160, height=40,
            fg_color=C_DANGER, hover_color="#c0392b",
            command=self._abort_session,
        ).pack()

        self._key_bind_id = self.bind("<KeyPress>", self._on_key)
        self._start_playback()

    # ── device dot ───────────────────────────────────────────────────

    def _refresh_device_dot(self):
        if not self.session_active:
            return
        if self.ble.connected:
            self._device_dot.configure(
                text=f"Device: {self.ble.device_name}",
                text_color=C_SUCCESS)
        else:
            self._device_dot.configure(
                text="Device: DISCONNECTED — reconnect after session",
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

        # Schedule BLE triggers with the cascade lead-time.
        if self.use_device and self.ble.connected:
            for t in cfg["triggers"]:
                fire_at = max(0.0, t - TRIGGER_LEAD_TIME)
                timer = threading.Timer(fire_at, self._fire_trigger, args=[t])
                timer.daemon = True
                timer.start()
                self.trigger_timers.append(timer)

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

        n = len(self.chills_reports)
        self._chills_lbl.configure(text=str(n), text_color=C_PRIMARY)
        self.after(180, lambda: self._chills_lbl.configure(
            text_color=("gray90", "gray90")))

    # ── trigger via direct BLE ───────────────────────────────────────

    def _fire_trigger(self, planned: float):
        """Called from a Timer thread at each trigger point."""
        if not self.session_active:
            return
        ok, _msg = self.ble.send(BLE_SESSION_PACKET)
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

        if not pygame.mixer.music.get_busy() and self.playback_start is not None:
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

        if self._key_bind_id:
            self.unbind("<KeyPress>", self._key_bind_id)
            self._key_bind_id = None

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

        ctk.CTkLabel(
            q, text="How intense were the chills?  (1 = mild, 10 = very intense)",
            font=("Helvetica", 16),
        ).pack(pady=(22, 8))

        sl_row = ctk.CTkFrame(q, fg_color="transparent")
        sl_row.pack()
        ctk.CTkLabel(sl_row, text="1", font=("Helvetica", 12),
                     text_color=C_MUTED).pack(side="left", padx=(0, 6))
        self._intensity_var = ctk.DoubleVar(value=5)
        ctk.CTkSlider(
            sl_row, from_=1, to=10, number_of_steps=9,
            variable=self._intensity_var, width=320,
            command=self._update_intensity_lbl,
        ).pack(side="left")
        ctk.CTkLabel(sl_row, text="10", font=("Helvetica", 12),
                     text_color=C_MUTED).pack(side="left", padx=(6, 0))

        self._int_lbl = ctk.CTkLabel(
            q, text="5", font=("Helvetica", 24, "bold"), text_color=C_PRIMARY,
        )
        self._int_lbl.pack(pady=4)

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

        self.participant_number += 1
        self.show_setup_page()

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
