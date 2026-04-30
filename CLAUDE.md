# ChillsDemo — Development Context

## What this is

A GUI desktop app (Python + customtkinter) for running chills/ASMR
experiments with the new Arduino-based Frisson haptic device. Built for
demo day — designed to be operated by a research assistant with many
participants cycling through.

Based on experiment code from
[E4002](https://github.com/Institute-for-Advanced-Consciousness/E4002)
by the Institute for Advanced Consciousness.

The legacy RFduino-based device, the Frisson webapp, and the WebSocket
relay (`fr_server.py`) have been removed. The app talks **directly** to
the new Arduino Nano ESP32 device over USB serial.

## Architecture

### Single-file app (`app.py`)

- **`FrissonDevice`** — abstract base class.
- **`FrissonSerial`** — USB serial implementation. Owns an asyncio loop
  in a daemon thread (mirroring the legacy persistent-loop pattern); all
  blocking pyserial I/O is wrapped in `loop.run_in_executor`.
- **`FrissonBLENew`** — stub. Always returns `False, "BLE not yet
  supported on the new device firmware — use USB Serial"`. The
  Connection screen exposes the Bluetooth toggle but disables Connect
  while it's selected. To wake this up later, implement BLE in v2 of
  the device firmware and have it speak the same ASCII command set.
- **`ArcTopRecorder`** — Suuvi-mode WebSocket EEG recorder; unchanged.
- **`ChillsDemoApp`** — customtkinter GUI.

### Pages (Frisson mode)

1. **Clicker Setup** — calibrate the clicker buttons (always first).
2. **Connection** — radio toggle: USB Serial (default) / Bluetooth
   (disabled, "coming soon"). Serial port dropdown, auto-pick of
   Arduino/ESP32 ports, Refresh button, Connect, Disconnect.
3. **Verify Device** — four checks with mandatory cooldown timers
   between fires. Per-check Pass/Fail/Skip. Skip Verification link
   bypasses straight to setup.
4. **Session Setup** — participant #, song, **editable trigger times**
   (defaults auto-populate; edits persist per-track to
   `track_overrides.json`), trigger **intensity** dropdown (low/med/
   high/max).
5. **Volume Check** — unchanged.
6. **Running Session** — audio playback, scheduled `wave-<intensity>`
   triggers at the configured times, manual P1/P2/P3/Wave/STOP panel,
   chills key capture, **Spacebar = global emergency stop** (Frisson
   mode only).
7. **Post-Session** — survey, save JSON, increment participant.

### Pages (Suuvi mode)

Untouched by the migration: setup card with ArcTop EEG verification +
URL textbox + Connect/Disconnect, volume check, running session, post.

### Threading model

Tk runs on the main thread. Long-running I/O runs in worker threads via
`_worker(fn, on_done)`. Two persistent asyncio loops live in daemon
threads: one inside `FrissonSerial`, one inside `ArcTopRecorder`. Sync
public methods submit coroutines via `run_coroutine_threadsafe(...)`.

Trigger scheduling uses `threading.Timer`, scheduled at the moment audio
playback begins (using `time.monotonic()` as the reference clock); the
timer thread marshals back to the tk main thread via `self.after(0, ...)`
before calling `_do_fire`.

> **TODO (timing v2)**: wall-clock-from-playback-start has ~10–50 ms drift
> from audio backend startup latency. For research-grade timing, switch
> to polling `pygame.mixer.music.get_pos()` and firing when the position
> crosses each trigger time.

## New Frisson Device — ASCII command protocol

Each command is a literal ASCII string + `\n`, sent over USB serial at
**115200 baud**. The device queues commands while busy, so don't overlap
commands shorter than the active pattern's runtime.

| Command family   | Variants                           | Behavior                                    | Duration |
|------------------|------------------------------------|---------------------------------------------|----------|
| `p1-…`/`p2-…`/`p3-…` | `low` `med` `high` `max`        | Fire that Peltier alone                     | ~2 s     |
| `wave-…`         | `low` `med` `high` `max`           | Rolling P3 → P2 → P1, 0.25 s offsets       | ~3.5 s   |
| `seq-…`          | `low` `med` `high` `max`           | Sequential P1 → P2 → P3, 1 s offsets       | ~3 s     |
| `sim-…`          | `low` `med` `high` `max`           | All three simultaneously                    | ~2 s     |
| `off`            | —                                  | Emergency stop — all channels off NOW       | instant  |

Intensity legend: `low` = 20% duty, `med` = 50%, `high` = 80%, `max` = 100%.

The device responds with diagnostic text on serial. The app drains it
into a rolling 200-line buffer and prints to stdout as `[Frisson] ...`,
but it never blocks on or parses these for control flow — protocol is
fire-and-forget.

### DTR reset gotcha

Opening the serial port pulses DTR which resets most Arduino boards
(including the Nano ESP32). After `serial.Serial(...)` succeeds, the
app sleeps `ARDUINO_BOOT_DELAY` (1.5 s) before allowing real commands,
so the Arduino's `setup()` completes first. If you change to a board
that doesn't reset on DTR, the delay is harmless.

## Trigger Timings (defaults)

These are the seeds; the GUI lets the operator edit them and the edits
persist per-track in `track_overrides.json` at the project root.

| Song       | Default triggers (seconds) | Est. duration |
|------------|----------------------------|---------------|
| Arameic    | 44, 79, 172                | ~4:24         |
| Hallelujah | 74, 93, 145                | ~5:12         |
| Misere    | 30, 81, 100                | ~4:30         |

## Data Output

Files saved to `Data/`:
- `session_<UTC>_P###_<Song>.json` — completed Frisson sessions
- `suuvi_<UTC>_P###_<Song>.json` — completed Suuvi sessions
- `PARTIAL_<UTC>_P###_<Song>.json` — interrupted sessions
- `eeg_<UTC>_P###_<Song>.csv` — Suuvi-mode EEG stream (when enabled)

### Frisson session JSON (key fields)

```jsonc
{
  "mode": "frisson",
  "connection_mode": "serial",          // "serial" | "ble"
  "device_port": "/dev/cu.usbmodem1101",
  "intensity_setting": "med",
  "scheduled_trigger_times": [44, 79, 172],
  "verify_skipped": false,
  "verify_results": [
    {"check": "ch_low", "result": "pass", "utc": "..."},
    ...
  ],
  "device_events": [
    {
      "t_ms": 44012, "utc": "...",
      "event": "scheduled_trigger",
      "command": "wave-med",
      "pattern": "wave", "channel": null, "intensity": "med",
      "success": true, "message": "OK", "source": "scheduled",
      "planned_sec": 44.0, "actual_sec": 44.012
    },
    {
      "t_ms": 51234, "utc": "...",
      "event": "manual_trigger",
      "command": "p2-med",
      "pattern": "single", "channel": 2, "intensity": "med",
      "success": true, "message": "OK", "source": "manual"
    },
    {
      "t_ms": 60500, "utc": "...",
      "event": "emergency_stop", "command": "off",
      "success": true, "source": "space-key"
    }
  ],
  // ...plus shared fields: chills_reports, post_survey, etc.
}
```

Errors and "device not connected" attempts are logged with
`success: false` and a human-readable `message` — the device-events log
is the canonical record of "what the operator tried" regardless of
success.

## Safety

- **Spacebar** sends `off` on Frisson sessions; logged.
- **Stop Session button**, **STOP** in the manual panel, window close,
  audio finishing all call `device.emergency_stop()` before tearing
  down. `atexit.register(self._safe_shutdown)` is the last-resort net.
- The Verify Device cooldowns are mandatory; all fire buttons are
  disabled across the screen during cooldown.
- `_do_fire` checks `device.is_connected` before sending — never
  crashes on a dropped link, just logs the failure.

## Key Decisions & Gotchas

- **Python 3.14**: Homebrew's Python 3.14 needs `brew install python-tk@3.14`
  for tkinter. Use `pygame-ce` (not `pygame`) — standard pygame has no
  3.14 wheel.
- **DTR reset on serial open**: `ARDUINO_BOOT_DELAY = 1.5` is a wait,
  not a poll — fine for the demo.
- **Threading**: tkinter is not thread-safe. All device + EEG ops run in
  worker threads via `_worker()`; `_fire_scheduled_trigger` (called on
  the Timer thread) marshals back via `self.after(0, ...)` before doing
  anything else.
- **Per-track override file**: `track_overrides.json` lives at the
  project root, not in `Data/`, so the operator can see it next to the
  code. Gitignored.
- **`csv` import**: kept for ArcTop EEG (Suuvi mode), unrelated to
  device control.
- **Audio files**: not committed (`.gitignore`). Must be placed in
  `Stimuli/` as `Arameic.mp3`, `Hallelujah.mp3`, `Misere.mp3`.

## How to Run

```bash
cd ~/Desktop/Projects/ChillsDemo
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

## Future Work

- BLE support for the new device firmware (`FrissonBLENew` is the stub
  to fill in; it should send the same ASCII commands)
- Audio-position-based trigger scheduling (replace monotonic-clock
  timers with `pygame.mixer.music.get_pos()` polling)
- LSL marker integration for EEG synchronization
- Configurable wave offsets / pattern variants
- Battery monitoring once the device exposes it
- Data visualization / summary dashboard
