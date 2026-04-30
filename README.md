# Chills Frisson Device Demo

A GUI application for running chills/ASMR experiments with the **new
Arduino Nano ESP32-based Frisson haptic device**. Connects to the device
over USB serial (BLE is scaffolded for a future firmware version), plays
audio stimuli, fires the device's three Peltier elements at editable
moments along the audio timeline, captures participant chills reports
via button presses, and saves session data automatically.

Built for demo day — designed for a research assistant to operate with
many participants cycling through quickly.

Based on [E4002](https://github.com/Institute-for-Advanced-Consciousness/E4002)
by the Institute for Advanced Consciousness.

## Features

- **USB Serial connection** (115200 baud) to the new Arduino-based Frisson device
- **Verify Device** check sequence (single-channel low/med + wave + emergency stop)
  with mandatory cooldown timers between fires (Peltier safety)
- **Editable per-track trigger times** with persisted overrides
- **Selectable trigger intensity** (low / med / high / max) per session
- **Manual trigger panel** during a session (P1, P2, P3, Wave, STOP)
- **Spacebar emergency stop** while a session is running (Frisson mode only)
- **Chills capture**: any key press is logged with a millisecond timestamp
- **Post-session survey**: chills yes/no + intensity rating (1–10)
- **Auto-save**: each session saved as JSON, with full device-event log
  (scheduled triggers, manual triggers, emergency stops, command failures)
- **Emergency save**: partial data preserved if the app is closed mid-session
- **Auto-incrementing participant numbers**

## Setup

### Quick install (macOS, double-click)

If someone sent you a **ChillsDemo zip**, this is the easy path:

1. **Unzip** the file. (Finder does this when you double-click it.)
2. **Double-click `INSTALL.command`** inside the unzipped folder.
   - If macOS warns "cannot be opened because it is from an
     unidentified developer", **right-click → Open** the first time.
     The installer dequarantines the rest of the folder so you only
     need to do this once.
   - The installer checks for Python 3.10+, creates a virtualenv,
     and installs all dependencies. ~30 s the first time.
   - If Python isn't installed, it opens the python.org download
     page and asks you to install + re-run.
3. **Double-click `ChillsDemo.command`** to launch the app. From
   then on, that's the only file you need.

### Manual install (developer / cross-platform)

Requirements:
- Python 3.10+ (tested on 3.14)
- macOS (primary target) or Linux
- The new Arduino Nano ESP32-based Frisson device, plugged in via USB

```bash
git clone https://github.com/Institute-for-Advanced-Consciousness/ChillsFrissonDeviceDemo.git
cd ChillsFrissonDeviceDemo
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

> **macOS / Homebrew Python**: If using Homebrew Python 3.14, you may
> need `brew install python-tk@3.14` for the GUI framework. The
> double-click installer detects this and prints the exact command.

### Bundling a copy to send to a friend

```bash
./make_zip.sh
```

Drops a `ChillsDemo-YYYYMMDD-HHMM.zip` next to the repo, with `venv/`,
`Data/`, `.git/`, and per-machine state stripped out. Send the zip;
recipient follows the **Quick install** steps above.

### Audio files

Place stimulus audio files in `Stimuli/`:

```
Stimuli/
├── Arameic.mp3
├── Hallelujah.mp3
└── Misere.mp3
```

These are not included in the repo.

## Usage

```bash
source venv/bin/activate
python app.py
```

### Workflow

1. **Clicker Setup** — calibrate the participant clicker (or skip).
2. **Device Connection** — pick **USB Serial (new device)**, choose the
   Arduino's port from the dropdown (usually auto-selected), click
   **Connect**. Bluetooth is shown as "coming soon" in the UI; only USB
   serial is functional today.
3. **Verify Device** — run the four checks (single-channel low, single-channel
   med, wave-low, emergency stop), each with mandatory cooldown timers between
   fires. Mark each Pass / Fail / Skip. Or click **Skip Verification**.
4. **Session Setup** — enter participant number, pick a stimulus, edit
   trigger times if needed (defaults auto-populate), pick the trigger
   intensity, click **Start Session**.
5. **Volume Check** — a reference tone plays at the track's peak loudness;
   participant adjusts headphone volume until comfortable.
6. **Running Session** — audio plays. Wave triggers fire at the configured
   times. Manual P1/P2/P3/Wave/STOP buttons are available. Spacebar is a
   global emergency stop. Participant clicks the chills button as they
   experience chills.
7. **Post-Session** — chills yes/no + intensity rating; data saves to
   `Data/`.

### Verify Device

Mirrors the breadboard bring-up protocol — prove each component works
in isolation before committing to a real participant session.

| # | Check | Action | Cooldown |
|---|-------|--------|----------|
| 1 | Single channels @ low | Fire P1 / P2 / P3 individually | 30 s |
| 2 | Single channels @ med | Fire P1 / P2 / P3 individually | 30 s |
| 3 | Wave (low) | Fire rolling wave P3 → P2 → P1 | 60 s |
| 4 | Emergency stop | Fire wave-low; auto-`off` after 500 ms | 30 s |

The cooldown is enforced across **all** fire buttons globally — you
can't rapid-fire even across different rows. Cooldowns aren't UX polish;
they're a hardware safety feature (we damaged Peltiers earlier in this
project by firing too rapidly).

### Trigger timings

Defaults shipped with the app — editable in the GUI; per-track edits
persist across launches in `track_overrides.json`.

| Song       | Default triggers (seconds) |
|------------|----------------------------|
| Arameic    | 44, 79, 172                |
| Hallelujah | 74, 93, 145                |
| Misere     | 30, 81, 100                |

### Data output

Session files are saved to `Data/` as JSON:

```
Data/session_2026-04-29_143052_P001_Arameic.json
```

Each Frisson session file contains:
- Participant ID, mode, timestamps (UTC)
- Track name, file, duration
- `connection_mode`, `device_port`, `intensity_setting`
- `scheduled_trigger_times` (the resolved list used for this session)
- `device_events[]` — every fire and emergency stop, with:
  - `t_ms` (session-relative milliseconds), `utc`
  - `event` ∈ {`scheduled_trigger`, `manual_trigger`, `emergency_stop`}
  - `command` (the literal ASCII command sent), `pattern`, `channel`,
    `intensity`, `success`, `message`, `source`
  - For scheduled triggers: `planned_sec` and `actual_sec`
- `verify_skipped` flag and `verify_results[]` per-check pass/fail/skip
- All chills button presses with timestamps
- Post-survey responses

Suuvi-mode session files keep their existing schema (with EEG fields).
Partial saves (`PARTIAL_*.json`) include the same fields when the app is
closed mid-session.

## Project structure

```
├── app.py                  # Main GUI application
├── INSTALL.command         # macOS double-click installer
├── ChillsDemo.command      # macOS double-click launcher
├── make_zip.sh             # Bundle a clean distributable zip
├── track_overrides.json    # Per-track trigger time overrides (gitignored)
├── requirements.txt        # Python dependencies
├── CLAUDE.md               # Development context for AI-assisted continuation
├── Stimuli/                # Audio files (not committed)
└── Data/                   # Session JSON files (not committed)
```

## License

Institute for Advanced Consciousness
