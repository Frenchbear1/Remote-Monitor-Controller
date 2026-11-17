from __future__ import annotations

import sys
from pathlib import Path

try:
    import winreg
except ImportError:  # non-Windows (development / tests)
    winreg = None  # type: ignore[assignment]


RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "BrightnessTrayScheduler"


def build_startup_command() -> str:
    if getattr(sys, "frozen", False):
        return f"\"{Path(sys.executable).resolve()}\""

    interpreter = Path(sys.executable).resolve()
    pythonw = interpreter.with_name("pythonw.exe")
    if pythonw.exists():
        interpreter = pythonw
    script_path = Path(sys.argv[0]).resolve()
    return f"\"{interpreter}\" \"{script_path}\""


def is_startup_enabled() -> bool:
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_READ) as key:
            _, _ = winreg.QueryValueEx(key, RUN_VALUE_NAME)
            return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def set_startup_enabled(enabled: bool) -> bool:
    if winreg is None:
        return True
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE
        ) as key:
            if enabled:
                winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ, build_startup_command())
                return True
            try:
                winreg.DeleteValue(key, RUN_VALUE_NAME)
            except FileNotFoundError:
                pass
            return True
    except OSError:
        return False
