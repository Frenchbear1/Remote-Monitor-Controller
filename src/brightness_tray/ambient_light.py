from __future__ import annotations

import re
import subprocess
import threading


_FLOAT_PATTERN = re.compile(r"[-+]?\d+(?:\.\d+)?")
_POWERSHELL_PATH = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
_LIGHT_SENSOR_SCRIPT = r"""
try {
    [void][Windows.Devices.Sensors.LightSensor, Windows, ContentType=WindowsRuntime]
    $sensor = [Windows.Devices.Sensors.LightSensor]::GetDefault()
    if ($null -eq $sensor) {
        return
    }
    $reading = $sensor.GetCurrentReading()
    if ($null -eq $reading) {
        return
    }
    [string]$reading.IlluminanceInLux
} catch {
    return
}
"""


class AmbientLightService:
    def __init__(self, poll_interval_seconds: float = 2.2) -> None:
        self.poll_interval_seconds = max(1.0, float(poll_interval_seconds))

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._latest_lux: float | None = None
        self._last_error: str | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=0.8)
        self._thread = None

    def probe_sensor(self) -> float | None:
        lux = self._query_lux()
        with self._lock:
            if lux is not None:
                self._latest_lux = lux
                self._last_error = None
            else:
                self._last_error = "Ambient light sensor reading unavailable."
        return lux

    def latest_lux(self) -> float | None:
        with self._lock:
            return self._latest_lux

    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            lux = self._query_lux()
            with self._lock:
                if lux is not None:
                    self._latest_lux = lux
                    self._last_error = None
                elif self._latest_lux is None:
                    self._last_error = "Ambient light sensor reading unavailable."

            if self._stop_event.wait(self.poll_interval_seconds):
                break

    @staticmethod
    def _query_lux() -> float | None:
        startupinfo = None
        creationflags = 0
        if hasattr(subprocess, "STARTUPINFO") and hasattr(subprocess, "STARTF_USESHOWWINDOW"):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        try:
            completed = subprocess.run(
                [
                    _POWERSHELL_PATH,
                    "-NoProfile",
                    "-Command",
                    _LIGHT_SENSOR_SCRIPT,
                ],
                capture_output=True,
                text=True,
                timeout=2.6,
                check=False,
                startupinfo=startupinfo,
                creationflags=creationflags,
            )
        except Exception:
            return None

        if completed.returncode != 0:
            return None

        output = (completed.stdout or "").strip()
        if not output:
            return None

        match = _FLOAT_PATTERN.search(output)
        if match is None:
            return None
        try:
            lux = float(match.group(0))
        except (TypeError, ValueError):
            return None
        if lux < 0:
            return 0.0
        return lux
