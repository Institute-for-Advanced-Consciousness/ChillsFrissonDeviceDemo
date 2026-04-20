# Chills Frisson Device Demo

A GUI application for running chills/ASMR experiments with the [Frisson](https://frissoniacs.github.io/) haptic device. Connects directly to the device over Bluetooth Low Energy, plays audio stimuli, triggers the device's peltier elements at predefined moments, captures participant chills reports via button presses, and saves session data automatically.

Built for demo day — designed for a research assistant to operate with many participants cycling through quickly.

Based on [E4002](https://github.com/Institute-for-Advanced-Consciousness/E4002) by the Institute for Advanced Consciousness.

## Features

- **Direct BLE connection** to the Frisson device (no webapp needed)
- **Test trigger** fires all 3 peltier elements at full strength for 3 seconds
- **3 audio stimuli** with predefined trigger timings from the E4002 protocol
- **Cascading trigger pattern**: P3 fires 0.25s before the beat, P2 on the beat, P1 0.25s after — all at max intensity for 3 seconds with overlap
- **Chills capture**: any key press (clicker or keyboard) is logged with a millisecond timestamp
- **Post-session survey**: chills yes/no + intensity rating (1–10)
- **Auto-save**: each session saved as a JSON file with full data
- **Emergency save**: partial data preserved if the app is closed mid-session
- **Auto-incrementing participant numbers**
- **Live device status** in the status bar with disconnect detection

## Setup

### Requirements

- Python 3.10+ (tested on 3.14)
- macOS (for BLE via CoreBluetooth) or Linux (via BlueZ)
- Frisson haptic device (RFduino-based)

### Install

```bash
git clone https://github.com/Institute-for-Advanced-Consciousness/ChillsFrissonDeviceDemo.git
cd ChillsFrissonDeviceDemo
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

> **macOS note**: If using Homebrew Python 3.14, you may need `brew install python-tk@3.14` for the GUI framework.

### Audio files

Place your stimulus audio files in the `Stimuli/` directory:

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

1. **Device Connection** — Power on the Frisson device, click "Scan & Connect". Test the trigger to verify the device responds.
2. **Session Setup** — Enter participant number, select a song (or Random), click "Start Session".
3. **Running Session** — Audio plays. The device triggers automatically at the predefined moments. The participant presses a clicker whenever they experience chills. A counter shows real-time chills count.
4. **Post-Session** — Rate whether chills were experienced and their intensity. Click "Save & Next Participant" — data is saved and the app is ready for the next person.

### Data output

Session files are saved to `Data/` as JSON:

```
Data/session_2026-04-20_143052_P001_Arameic.json
```

Each file contains:
- Participant ID and timestamp
- Song name and duration
- Every chills button press with its timestamp
- Device trigger results (planned time, actual time, success)
- Post-survey responses (chills yes/no, intensity 1–10)

### Trigger timings

| Song       | Triggers (seconds into track) |
|------------|-------------------------------|
| Arameic    | 44, 79, 172                   |
| Hallelujah | 74, 93, 145                   |
| Misere     | 30, 81, 100                   |

## Project structure

```
├── app.py              # Main GUI application
├── fr_server.py        # WebSocket relay server (legacy, not used by app)
├── requirements.txt    # Python dependencies
├── CLAUDE.md           # Development context for AI-assisted continuation
├── Stimuli/            # Audio files (not committed)
└── Data/               # Session JSON files (not committed)
```

## License

Institute for Advanced Consciousness
