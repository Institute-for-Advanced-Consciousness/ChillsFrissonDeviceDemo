# ChillsDemo — Development Context

## What this is

A GUI desktop app (Python + customtkinter) for running chills/ASMR experiments with the Frisson haptic device. Built for demo day — designed to be operated by a research assistant with many participants cycling through.

Based on experiment code from [E4002](https://github.com/Institute-for-Advanced-Consciousness/E4002) by the Institute for Advanced Consciousness.

**A new replacement device is being designed.** The BLE protocol and app will be updated together. See "New Device Design Notes" below.

## Architecture

### Single file app (`app.py`)

- **FrissonBLE class** — Persistent BLE connection to the Frisson device via `bleak`. Runs its own asyncio event loop in a background thread. All public methods (`connect`, `disconnect`, `send`, `send_and_listen`) are blocking and must be called from worker threads, never the tkinter main thread.
- **ChillsDemoApp class** — customtkinter GUI with 4 pages:
  1. **Connection** — Scan & Connect (direct BLE), Test Trigger, Disconnect
  2. **Session Setup** — Participant #, song selection (Random/Arameic/Hallelujah/Misere)
  3. **Running Session** — Audio playback, chills key capture, BLE triggers at predefined times
  4. **Post-Session** — Chills yes/no, intensity rating, auto-save to JSON

### Data output

JSON files saved to `Data/` with naming: `session_YYYY-MM-DD_HHMMSS_P###_Song.json`

Contains: participant ID, song, duration, all chills key-press timestamps, device trigger results (planned vs actual time, success), and post-survey responses.

Partial data is auto-saved on unexpected close (`PARTIAL_*.json`).

---

## Current Frisson Device — Complete BLE Reverse-Engineering

### Hardware

The current Frisson device is built on an **RFduino** (now discontinued) — an Arduino-compatible board with built-in BLE. It controls:

- **3 Peltier elements** (P1, P2, P3) — thermoelectric modules that create a cold/warm sensation on the skin. These are the primary "chills" actuators.
- **1 Motor** (M1) — a vibration motor. Defaults to off (strength 0) in the experiment.

The device is battery-powered (swappable batteries), has no display, and has no way to report its own state.

### BLE Service & Characteristics

**Service UUID**: `00002220-0000-1000-8000-00805f9b34fb` (RFduino default service)

The service exposes **3 characteristics** (discovered via bleak enumeration):

| Index | Likely UUID | Properties | Purpose |
|-------|------------|------------|---------|
| 0 | `00002221-...` | write, write-without-response | **DO NOT USE** — appears to be a control/config char with a short max value length. Writing 13 bytes here causes GATT error 13 ("Invalid Attribute Value Length"). |
| 1 | `00002222-...` | write, write-without-response | **THE WRITE TARGET** — accepts the 13-byte command packet. This is what the webapp uses (`characteristics[1]`). |
| 2 | `00002223-...` | notify | Notification characteristic — subscribed but **never fires**. The firmware does not send any data back. |

**Critical**: Always use `characteristics[1]` (index 1). The webapp source (`sketch_rfduino.js` line 328) hardcodes this: `writeCharacteristic = characteristics[1]`.

**No other services detected**: No Battery Service (0x180F), no Device Information Service (0x180A). The device exposes only the single RFduino service.

### Packet Format (13 bytes)

```
Byte   Field         Range     Description
─────  ────────────  ────────  ───────────────────────────────
0      cmd           20        Command type (20 = peltier control)
1      P1_strength   0–255     Peltier 1 intensity
2      P2_strength   0–255     Peltier 2 intensity
3      P3_strength   0–255     Peltier 3 intensity
4      M1_strength   0–255     Motor intensity (usually 0)
5      P1_start      0–255     Peltier 1 start time (×0.1s)
6      P2_start      0–255     Peltier 2 start time (×0.1s)
7      P3_start      0–255     Peltier 3 start time (×0.1s)
8      M1_start      0–255     Motor start time (×0.1s)
9      P1_stop       0–255     Peltier 1 stop time (×0.1s)
10     P2_stop       0–255     Peltier 2 stop time (×0.1s)
11     P3_stop       0–255     Peltier 3 stop time (×0.1s)
12     M1_stop       0–255     Motor stop time (×0.1s)
```

**Timing resolution**: 100 ms (0.1 seconds). Values are in units of 0.1s, so `30` = 3.0 seconds. Max representable time = 25.5 seconds (255 × 0.1s).

**How timing works**: When the device receives the packet, it starts an internal timer. Each element activates at its `start` time and deactivates at its `stop` time. Duration = stop − start. All elements are relative to the moment the packet is received.

**Command byte**: Only value `20` is used (peltier). From the non-RFduino variant (`sketch.js`), the protocol also defines `30` = Motor and `50` = LED, but these use a different 4-byte packet format on a different firmware (see below).

### Packet Examples

**All peltiers simultaneously for 3 seconds** (test trigger):
```python
bytes([20, 255, 255, 255, 0, 0, 0, 0, 0, 30, 30, 30, 0])
#      cmd  P1   P2   P3   M1  P1s P2s P3s M1s P1e P2e P3e M1e
```

**Default webapp cascade** (P1 → P2 → P3, 1 second each):
```python
bytes([20, 255, 255, 255, 0, 0, 10, 20, 0, 10, 20, 30, 0])
#      P1: 0.0–1.0s  P2: 1.0–2.0s  P3: 2.0–3.0s
```

**Our session trigger** (reverse cascade P3 → P2 → P1, sent 0.25s early):
```python
bytes([20, 255, 255, 255, 0, 5, 3, 0, 0, 35, 33, 30, 0])
#      P3: immediate (0.0–3.0s)
#      P2: +0.3s     (0.3–3.3s)
#      P1: +0.5s     (0.5–3.5s)
```

### Alternate Firmware Variant (sketch.js — NOT currently loaded)

The webapp repo also contains `sketch.js` which uses a different BLE protocol. This is loaded by some other HTML page (not index.html) and targets a different hardware variant:

**Service UUID**: `6E400001-B5A3-F393-E0A9-E50E24DCCA9E` (Nordic UART Service / NUS)

**Write characteristic**: `characteristics[0]` (index 0, not 1)

**Packet format**: 4 bytes per element, sent individually:
```
[action, element_index, duration, strength]
```

**Action codes**:
- `20` = Peltier
- `30` = Motor
- `50` = LED

**Element location indices** (from `locP` array): `[0, 3, 4, 2]`

This variant sends each element as a separate BLE write with 100ms intervals between them, using `setInterval`. It provides finer per-element control but requires multiple writes per trigger.

**Key insight**: If the new device uses NUS (Nordic UART Service), this 4-byte per-element protocol might be more appropriate. It allows individual element control and is more extensible.

### What We Know About Device Behavior

1. **Fire-and-forget**: The device accepts the 13-byte packet and acts on it. No acknowledgment, no status, no error reporting.
2. **No readable state**: All characteristics with "read" property returned no useful data or errored.
3. **No notifications**: The notify characteristic exists but the firmware never pushes data through it. We subscribed to ALL notify characteristics across all services — nothing came.
4. **No battery reporting**: No Battery Service. No way to know charge level.
5. **Connection is the only health signal**: If `BleakClient.is_connected` is True, the radio link is alive. That's the only feedback we have. A disconnection event fires when the device powers off or goes out of range.
6. **Suspected hardware issue**: During testing (2026-04-20), the device connected and accepted writes successfully, but peltier activation was inconsistent. Possibly a hardware/wiring issue separate from BLE.

### Frisson Webapp Architecture (for reference)

Source: https://github.com/frissoniacs/frissoniacs.github.io

The webapp (`index.html`) loads `sketch_rfduino.js` (not `sketch.js`). Built with p5.js + p5ble.

**WebSocket messages the webapp sends** (to `ws://localhost:8766`):
- `FW_Frisson_Hello` — on WebSocket open (page loaded)
- `FW_Frisson_Trigger` — after a successful BLE write to the device
- `FW_Stimulus_Start` — when video playback starts
- `FW_Stimulus_End` — when video finishes
- `FW_Stimulus_Reset` — when video is reset

**WebSocket messages the webapp listens for**:
- `trigger_device` — calls `writeToBle()` (our primary trigger command)
- `start_stimulus` — starts video playback
- `reset_stimulus` — resets video

**Webapp GUI sliders** (visible after BLE connect):
- Timings panel: P1_Start/Stop, P2_Start/Stop, P3_Start/Stop, M1_Start/Stop (0–6s, 0.1s steps), Stimulus_Duration, Timings
- Strength panel: P1_Strength, P2_Strength, P3_Strength, M1_Strength (0–255)

**Keyboard shortcuts in the webapp**: `p` = manual trigger, `m` = start/stop video, `r` = reset video

Our app no longer uses the webapp — we connect directly via bleak. But `fr_server.py` is kept in the repo in case the webapp is needed for debugging.

---

## New Device Design Notes

A replacement device is being designed. These are recommendations based on reverse-engineering the current device:

### What the current device lacks (and the new one should have)

1. **Per-element status feedback** — After receiving a trigger, the device should notify back with per-element confirmation: which elements activated, their measured current/voltage, and any errors (e.g., open circuit = element disconnected, overcurrent = short).

2. **Battery level** — Expose standard BLE Battery Service (UUID `0x180F`) with Battery Level characteristic (`0x2A19`). Report 0–100%. Push notifications when level changes significantly or drops below threshold.

3. **Device state characteristic** — A readable characteristic that reports: connection uptime, last trigger time, element health, firmware version, temperature readings if applicable.

4. **Error reporting** — Notify on faults: element failure, low battery, thermal shutdown, overcurrent protection events.

5. **Configuration persistence** — Allow writing calibration/config data that persists across power cycles (e.g., default intensity limits, element mapping).

6. **Higher timing resolution** — The current 100ms (0.1s) resolution is limiting for precise perceptual experiments. Consider 10ms or 1ms resolution. With 16-bit timing fields instead of 8-bit, you get 0–65535 ms range at 1ms resolution.

### Recommended BLE Service Design for New Device

```
Frisson Service (custom UUID)
├── Trigger Characteristic (write, write-without-response)
│   Write command packets to fire elements.
│   
├── Status Characteristic (read, notify)
│   Readable: current device state (JSON or structured binary)
│   Notify: pushed after each trigger with per-element results
│   
├── Config Characteristic (read, write)
│   Read/write device configuration (timing limits, defaults, etc.)
│   
└── Error Characteristic (notify)
    Pushed on faults: element failure, low battery, etc.

Battery Service (0x180F, standard)
└── Battery Level (0x2A19, read, notify)

Device Information Service (0x180A, standard)
├── Manufacturer Name
├── Firmware Revision
├── Hardware Revision
└── Serial Number
```

### Recommended Trigger Packet Format (new device)

Expand from 13 bytes to a more flexible format:

```
Byte 0:     Packet version (1)
Byte 1:     Command (0x01 = trigger, 0x02 = stop all, 0x03 = query status)
Byte 2:     Number of elements (N)
Bytes 3+:   Per-element blocks (6 bytes each):
              [element_id, strength, start_ms_hi, start_ms_lo, stop_ms_hi, stop_ms_lo]
```

This allows:
- Variable number of elements (future-proof)
- 16-bit timing (0–65535 ms at 1ms resolution)
- Explicit element IDs (not positional)
- Room for new commands

### Recommended Status Response Format

After receiving a trigger, the device should notify on the Status characteristic:

```
Byte 0:     Response type (0x01 = trigger ack)
Byte 1:     Status (0x00 = all OK, 0x01 = partial failure, 0x02 = all failed)
Byte 2:     Number of elements
Bytes 3+:   Per-element status (3 bytes each):
              [element_id, status (0=ok, 1=open, 2=short, 3=overcurrent), measured_strength]
```

### Python App Changes Needed for New Device

When the new device is ready, the app needs:
1. Update `FRISSON_SERVICE_UUID` to the new service UUID
2. Update packet construction to the new format
3. Subscribe to the Status characteristic and parse trigger acknowledgments
4. Read Battery Service and display in status bar
5. Handle error notifications (show warnings, auto-retry)
6. Read Device Information for firmware version display
7. Update `send_and_listen` to parse structured responses instead of raw hex dumps

The `FrissonBLE` class is already structured to support this — it has notify subscription, read-all-chars, and the `send_and_listen` pattern. The main work is updating packet formats and adding response parsing.

---

## Trigger Timing Per Song (from E4002)

| Song       | Trigger times (seconds) | Est. duration |
|------------|------------------------|---------------|
| Arameic    | 44, 79, 172            | ~4:24         |
| Hallelujah | 74, 93, 145            | ~5:12         |
| Misere     | 30, 81, 100            | ~4:30         |

## Key Decisions & Gotchas

- **Python 3.14**: Homebrew's Python 3.14 requires `brew install python-tk@3.14` for tkinter. Use `pygame-ce` (not `pygame`) — standard pygame has no 3.14 wheel.
- **Write characteristic index**: The device has 3 BLE characteristics. Writing to index 0 causes GATT error 13 "Invalid Attribute Value Length". Must use index 1 (matching the Frisson webapp's `sketch_rfduino.js`).
- **Write mode**: The code auto-detects whether to use write-with-response or write-without-response based on the characteristic's properties.
- **No device feedback**: The firmware doesn't send notifications or have readable state. Write success only means the BLE stack accepted the packet; there's no confirmation the peltiers actually fired.
- **Webapp removed**: Originally used the Frisson webapp (frissoniacs.github.io) as a BLE bridge via WebSocket. Replaced with direct BLE via bleak for simplicity and full control. The `fr_server.py` WebSocket relay is still in the repo but no longer used.
- **Audio files**: Not committed (`.gitignore`). Must be placed in `Stimuli/` as `Arameic.mp3`, `Hallelujah.mp3`, `Misere.mp3`.
- **Threading model**: tkinter is not thread-safe. All BLE operations run in worker threads via `_worker()`. The FrissonBLE class has its own asyncio event loop in a daemon thread. UI updates from callbacks use `self.after(0, fn)` to schedule on the main thread.
- **BleakClient lifecycle**: The client must stay alive on the same event loop that created it. That's why FrissonBLE runs `loop.run_forever()` in a dedicated thread and submits coroutines via `run_coroutine_threadsafe`. Creating a new `asyncio.run()` per operation would kill the connection.

## How to Run

```bash
cd ~/Desktop/ChillsDemo
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

## Future Work

- **New device integration** — replace RFduino protocol with new device's BLE protocol
- Add more stimuli / configurable trigger timings
- LSL marker integration for EEG synchronization
- Battery monitoring (available once new device has Battery Service)
- Per-element trigger confirmation in session data
- Data visualization / summary dashboard
- Configurable cascade timing (currently hardcoded P3→P2→P1 with 0.25s offsets)
