# Remote Monitor Controller

Remote Monitor Controller is a fast Windows tray app for external display control over DDC/CI and built-in display control, with smooth automation and instant manual override from the tray.

## Highlights
- Extremely quick monitor discovery and reconnect from the tray.
- Auto Brightness mode using ambient light sensor data with smoothing and step limits.
- Monitor controls beyond brightness (contrast, sharpness, RGB gain, black level, and other supported VCP sliders).
- Linked mode (one slider for all displays) and unlinked per-monitor mode.
- Sunrise/sunset and fixed-time scheduling with offsets, per-display targets, and gradual transitions.
- Persistent settings and startup launch support.

## Features
- Tray icon with quick actions: show popup, refresh monitors, apply schedule now, quit.
- Compact popup panel from tray click (instead of a full main window).
- Linked mode:
  - `Link all displays` + one slider applies the same value to every detected monitor.
- Unlinked mode:
  - separate sliders for each monitor.
- Settings:
  - startup toggle,
  - monitor picture-control popup (DDC/CI detected sliders like contrast, sharpness, black level, RGB gain, etc.),
  - Auto Light ambient-sensor mode (linked: all displays, unlinked: computer display only),
  - schedule enable/disable,
  - gradual interpolation between schedule points,
  - one-click sunrise/sunset ramp preset rules,
  - auto location detect from IP or manual latitude/longitude,
  - per-rule display target: `Display 1`, `Display 2`, or `Both`,
  - anchor choices: `sunrise`, `sunset`, or fixed `specific time`,
  - customizable schedule rules like `Display 1 + sunset +30 min -> 75%`.
- Popup opens with current live monitor brightness values.

## Requirements
- Windows 10/11
- Python 3.10+
- For external monitors, enable DDC/CI in the monitor OSD menu.

## Install
```powershell
cd "C:\Users\labar\Downloads\All Projects\Display-Brightness-Tray"
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Run
```powershell
cd "C:\Users\labar\Downloads\All Projects\Display-Brightness-Tray"
.venv\Scripts\Activate.ps1
python main.py
```

After launch, the app lives in the system tray.

## Notes
- Startup registration uses:
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`
- Schedule can target `Display 1`, `Display 2`, or `Both` per rule.
- If you use display-targeted rules, the first two detected displays map to `Display 1` and `Display 2`.
- If auto-location fails, set latitude/longitude manually in Settings.
