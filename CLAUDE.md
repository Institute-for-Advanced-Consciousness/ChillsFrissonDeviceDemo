# ChillsDemo — Development Context

## What this is

A GUI desktop app (Python + customtkinter) for running chills/ASMR experiments with the Frisson haptic device. Built for demo day — designed to be operated by a research assistant with many participants cycling through.

Based on experiment code from [E4002](https://github.com/Institute-for-Advanced-Consciousness/E4002) by the Institute for Advanced Consciousness.

## Architecture

### Single file app (`app.py`)

- **FrissonBLE class** — Persistent BLE connection to the Frisson device via `bleak`. Runs its own asyncio event loop in a background thread. All public methods (`connect`, `disconnect`, `send`, `send_and_listen`) are blocking and must be called from worker threads, never the tkinter main thread.
- **ChillsDemoApp class** — customtkinter GUI with 4 pages:
  1. **Connection** — Scan & Connect (direct BLE), Test Trigger, Disconnect
  2. **Session Setup** — Participant #, song selection (Random/Arameic/Hallelujah/Misere)
  3. **Running Session** — Audio playback, chills key capture, BLE triggers at predefined times
  4. **Post-Session** — Chills yes/no, intensity rating, auto-save to JSON

### BLE Protocol (Frisson device)

The device is RFduino-based with service UUID `00002220-0000-1000-8000-00805f9b34fb`.

- 3 characteristics in the service; the **second one** (index 1) is the write target (matching the webapp's `characteristics[1]`)
- The device has a notify characteristic but the firmware does not appear to send any feedback data (fire-and-forget)
- No battery service detected — battery monitoring is not possible via BLE with this firmware

**Packet format** (13 bytes):
```
[cmd, P1_str, P2_str, P3_str, M1_str,
 P1_start, P2_start, P3_start, M1_start,
 P1_stop,  P2_stop,  P3_stop,  M1_stop]
```
- `cmd` = 20 (peltier command)
- `str` = strength 0–255
- `start`/`stop` = timing in 0.1s units (e.g., 30 = 3.0 seconds)
- P1/P2/P3 = peltier elements, M1 = motor

**Test packet**: All peltiers at 255, simultaneous, 3 seconds:
```python
bytes([20, 255, 255, 255, 0, 0, 0, 0, 0, 30, 30, 30, 0])
```

**Session trigger packet** — cascading wave sent 0.25s before each trigger timepoint:
```python
bytes([20, 255, 255, 255, 0, 5, 3, 0, 0, 35, 33, 30, 0])
```
- P3 fires immediately (at T−0.25s)
- P2 fires at +0.3s (≈T)
- P1 fires at +0.5s (at T+0.25s)
- All last 3 seconds with overlap

### Trigger timing per song (from E4002)

| Song       | Trigger times (seconds) | Est. duration |
|------------|------------------------|---------------|
| Arameic    | 44, 79, 172            | ~4:24         |
| Hallelujah | 74, 93, 145            | ~5:12         |
| Misere     | 30, 81, 100            | ~4:30         |

### Data output

JSON files saved to `Data/` with naming: `session_YYYY-MM-DD_HHMMSS_P###_Song.json`

Contains: participant ID, song, duration, all chills key-press timestamps, device trigger results (planned vs actual time, success), and post-survey responses.

Partial data is auto-saved on unexpected close (`PARTIAL_*.json`).

## Key decisions & gotchas

- **Python 3.14**: Homebrew's Python 3.14 requires `brew install python-tk@3.14` for tkinter. Use `pygame-ce` (not `pygame`) — standard pygame has no 3.14 wheel.
- **Write characteristic index**: The device has 3 BLE characteristics. Writing to index 0 causes "Invalid Attribute Value Length" error. Must use index 1 (matching the Frisson webapp's `sketch_rfduino.js`).
- **Write mode**: The code auto-detects whether to use write-with-response or write-without-response based on the characteristic's properties.
- **No device feedback**: The firmware doesn't send notifications or have readable state. Write success only means the BLE stack accepted the packet; there's no confirmation the peltiers actually fired. Suspected hardware issue being investigated separately.
- **Webapp removed**: Originally used the Frisson webapp (frissoniacs.github.io) as a BLE bridge via WebSocket. Replaced with direct BLE via bleak for simplicity and full control. The `fr_server.py` WebSocket relay is still in the repo but no longer used by the app.
- **Audio files**: Not committed (`.gitignore`). Must be placed in `Stimuli/` as `Arameic.mp3`, `Hallelujah.mp3`, `Misere.mp3`.

## How to run

```bash
cd ~/Desktop/ChillsDemo
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

## Future work

- Investigate hardware issue with peltier feedback
- Add more stimuli / configurable trigger timings
- LSL marker integration for EEG synchronization
- Battery monitoring (requires firmware update on device)
- Data visualization / summary dashboard
