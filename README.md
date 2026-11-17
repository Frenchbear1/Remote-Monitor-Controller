# Remote Monitor Controller

A Windows tray app for controlling external monitors over DDC/CI (and your built-in display) from a frosted-glass popup. Brightness follows the sun, an ambient light sensor, or your hand on the slider.

## Highlights
- **Glass UI.** Translucent popup and settings windows with an optional Windows 11 acrylic blur, five themes (Midnight, Aurora, Graphite, Frost, Dune), glowing gradient sliders and smooth fade/slide animations.
- **Instant and responsive.** The popup opens immediately. Monitor detection and DDC/CI writes happen in the background, and rapid slider drags are coalesced so the hardware only receives the latest value.
- **Linked or per-monitor brightness.** One slider for every display, or one per monitor.
- **Auto Light.** Ambient light sensor with smoothing and step limits (linked: all displays, unlinked: the computer display).
- **Sunrise/sunset scheduling.** Drag points on a day graph, per-display curves, fixed-time or sun-relative anchors with offsets, gradual transitions and a one-click preset curve.
- **Picture controls.** Contrast, sharpness, RGB gain, black level and other VCP sliders your monitor reports.
- **Location.** Detected automatically from your IP, or enter latitude/longitude manually in Settings (manual coordinates are never overwritten).

## Requirements
- Windows 10/11 (the acrylic blur needs Windows 11 22H2 or newer; older systems use the built-in translucent glass instead)
- Python 3.10+
- For external monitors, enable DDC/CI in the monitor's on-screen menu

## Install
```powershell
git clone https://github.com/Frenchbear1/Remote-Monitor-Controller.git
cd Remote-Monitor-Controller
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Run
```powershell
.venv\Scripts\Activate.ps1
python main.py
```

The app lives in the system tray. Click the tray icon to open the popup; right-click for refresh / apply schedule / quit.

## Build the executable
```powershell
pip install pyinstaller
pyinstaller Remote-Monitor-Controller.spec
```
The result is written to `dist\Remote-Monitor-Controller.exe`.

## Settings
- **Appearance:** pick a theme; changes preview live and are discarded if you cancel.
- **General:** launch at Windows startup, and toggle the frosted blur if it looks wrong on your system.
- **Picture controls:** open monitor-specific sliders detected through DDC/CI (use *Deep scan* to ask the monitor for its full capability list).
- **Scheduling:** enable the schedule, choose gradual transitions, set your location, then edit the curve.
  - Double-click the graph to add a point, drag points to change time and brightness.
  - Each point targets `Display 1`, `Display 2` or `Both`, anchored to `sunrise`, `sunset` or a specific time (with an offset).
  - The first two detected displays map to `Display 1` and `Display 2`.

## Notes
- Settings are stored in `%APPDATA%\BrightnessTrayScheduler\config.json` (the folder name is kept so existing settings carry over).
- Startup registration uses `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`.
- If a monitor stops responding, an inline message names the display; check that DDC/CI is enabled in its menu.
