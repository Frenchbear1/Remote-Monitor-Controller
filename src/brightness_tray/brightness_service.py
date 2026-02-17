from __future__ import annotations

import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import screen_brightness_control as sbc

from .models import clamp_brightness


@dataclass
class MonitorHandle:
    key: str
    name: str
    display_index: int
    method_name: str | None


@dataclass
class PictureControl:
    code: int
    name: str
    minimum: int
    maximum: int
    value: int


class BrightnessService:
    _PICTURE_CODE_ORDER: tuple[int, ...] = (
        0x10,  # Luminance / Brightness
        0x12,  # Contrast
        0x16,  # Red gain
        0x18,  # Green gain
        0x1A,  # Blue gain
        0x6C,  # Black level
        0x6E,  # Red black level
        0x70,  # Green black level
        0x72,  # Blue black level
        0x87,  # Sharpness
    )
    _PICTURE_CODE_NAMES: dict[int, str] = {
        0x10: "Brightness",
        0x12: "Contrast",
        0x13: "Backlight",
        0x16: "Red Gain",
        0x18: "Green Gain",
        0x1A: "Blue Gain",
        0x6C: "Black Level",
        0x6E: "Red Black Level",
        0x70: "Green Black Level",
        0x72: "Blue Black Level",
        0x87: "Sharpness",
        0x8B: "Saturation",
        0x8C: "Hue",
    }

    def __init__(self) -> None:
        self.monitors: list[MonitorHandle] = []
        self._picture_controls_cache: dict[str, list[PictureControl]] = {}

    def refresh_monitors(self) -> list[MonitorHandle]:
        raw_monitors = sbc.list_monitors_info(allow_duplicates=False)
        parsed: list[MonitorHandle] = []
        seen_keys: dict[str, int] = {}

        for fallback_index, raw in enumerate(raw_monitors):
            monitor = self._from_raw_monitor(raw, fallback_index, seen_keys)
            parsed.append(monitor)

        self.monitors = parsed
        active_keys = {monitor.key for monitor in parsed}
        stale_keys = [key for key in self._picture_controls_cache if key not in active_keys]
        for key in stale_keys:
            self._picture_controls_cache.pop(key, None)
        return list(self.monitors)

    def get_brightness(self, monitor: MonitorHandle) -> int | None:
        for call_kwargs in self._build_call_args(monitor):
            try:
                value = sbc.get_brightness(**call_kwargs)
                if isinstance(value, list):
                    value = value[0]
                return clamp_brightness(value)
            except Exception:
                continue
        return None

    def set_brightness(self, monitor: MonitorHandle, value: int) -> bool:
        target = clamp_brightness(value)
        for call_kwargs in self._build_call_args(monitor):
            try:
                sbc.set_brightness(target, **call_kwargs)
                return True
            except Exception:
                continue
        return False

    def list_picture_controls(
        self,
        monitor: MonitorHandle,
        use_cache: bool = True,
        include_capabilities: bool = False,
    ) -> list[PictureControl]:
        if self._normalize_method(monitor.method_name) != "vcp":
            return []
        if use_cache:
            cached_controls = self._picture_controls_cache.get(monitor.key)
            if cached_controls is not None:
                return self._clone_picture_controls(cached_controls)

        with self._open_vcp_monitor_handle(monitor.display_index) as (has_handle, handle):
            if not has_handle:
                return []

            candidate_codes = self._candidate_picture_codes(
                handle,
                include_capabilities=include_capabilities,
            )
            controls: list[PictureControl] = []
            seen_codes: set[int] = set()

            for code in candidate_codes:
                if code in seen_codes:
                    continue
                seen_codes.add(code)

                feature = self._read_vcp_feature(handle, code, max_tries=2)
                if feature is None:
                    continue

                current, maximum = feature
                if maximum <= 0:
                    continue

                minimum = 0
                upper_bound = max(maximum, current)
                bounded_value = max(minimum, min(current, upper_bound))
                controls.append(
                    PictureControl(
                        code=code,
                        name=self._picture_control_name(code),
                        minimum=minimum,
                        maximum=upper_bound,
                        value=bounded_value,
                    )
                )

            cloned_controls = self._clone_picture_controls(controls)
            self._picture_controls_cache[monitor.key] = cloned_controls
            return self._clone_picture_controls(cloned_controls)

    def set_picture_control(self, monitor: MonitorHandle, code: int, value: int) -> bool:
        if self._normalize_method(monitor.method_name) != "vcp":
            return False

        normalized_code = max(0, min(0xFF, int(code)))
        target_value = max(0, min(10_000, int(value)))
        with self._open_vcp_monitor_handle(monitor.display_index) as (has_handle, handle):
            if not has_handle:
                return False

            success = self._write_vcp_feature(handle, normalized_code, target_value)
            if not success:
                return False

            cached_controls = self._picture_controls_cache.get(monitor.key)
            if cached_controls is None:
                return True
            for control in cached_controls:
                if control.code != normalized_code:
                    continue
                control.value = max(control.minimum, min(control.maximum, target_value))
                break
            return True

    @staticmethod
    def _from_raw_monitor(
        raw_monitor: dict[str, Any], fallback_index: int, seen_keys: dict[str, int]
    ) -> MonitorHandle:
        display_index = raw_monitor.get("index", fallback_index)
        if not isinstance(display_index, int):
            display_index = fallback_index

        method_name: str | None = None
        raw_method = raw_monitor.get("method")
        if isinstance(raw_method, str):
            method_name = raw_method
        elif raw_method is not None:
            method_name = getattr(raw_method, "__name__", str(raw_method))

        raw_name = str(raw_monitor.get("name") or "").strip()
        fallback_name = f"Display {fallback_index + 1}"
        name = raw_name or fallback_name
        if BrightnessService._normalize_method(method_name) == "wmi":
            if not raw_name or BrightnessService._is_generic_monitor_name(raw_name):
                device_name = BrightnessService.system_device_name()
                if device_name:
                    name = device_name

        serial = str(raw_monitor.get("serial") or raw_monitor.get("edid") or display_index)
        base_key = f"{(method_name or 'unknown').lower()}|{name}|{serial}"
        count = seen_keys.get(base_key, 0)
        seen_keys[base_key] = count + 1
        key = base_key if count == 0 else f"{base_key}|{count}"

        return MonitorHandle(
            key=key,
            name=name,
            display_index=display_index,
            method_name=method_name,
        )

    @staticmethod
    def _build_call_args(monitor: MonitorHandle) -> list[dict[str, Any]]:
        call_args: list[dict[str, Any]] = []
        method = BrightnessService._normalize_method(monitor.method_name)
        if method:
            call_args.append({"display": monitor.display_index, "method": method})
        call_args.append({"display": monitor.display_index})
        return call_args

    def _candidate_picture_codes(
        self,
        monitor_handle: Any,
        include_capabilities: bool,
    ) -> list[int]:
        capabilities_codes: list[int] = []
        if include_capabilities:
            capabilities_codes = self._read_vcp_codes_from_capabilities(monitor_handle)
        if capabilities_codes:
            capabilities_set = set(capabilities_codes)
            candidates = [code for code in self._PICTURE_CODE_ORDER if code in capabilities_set]
            for code in capabilities_codes:
                if code in candidates:
                    continue
                if not self._looks_like_picture_code(code):
                    continue
                candidates.append(code)
            if candidates:
                return candidates

        candidates = list(self._PICTURE_CODE_ORDER)
        return candidates

    @staticmethod
    def _clone_picture_controls(controls: list[PictureControl]) -> list[PictureControl]:
        return [
            PictureControl(
                code=control.code,
                name=control.name,
                minimum=control.minimum,
                maximum=control.maximum,
                value=control.value,
            )
            for control in controls
        ]

    @classmethod
    def _picture_control_name(cls, code: int) -> str:
        return cls._PICTURE_CODE_NAMES.get(code, f"VCP 0x{code:02X}")

    @classmethod
    def _looks_like_picture_code(cls, code: int) -> bool:
        if code in cls._PICTURE_CODE_NAMES:
            return True
        if code in (0x86, 0x88):
            return True
        return False

    @staticmethod
    @contextmanager
    def _open_vcp_monitor_handle(display_index: int):
        if os.name != "nt":
            yield (False, None)
            return

        try:
            import screen_brightness_control.windows as sbc_windows
        except Exception:
            yield (False, None)
            return

        monitor_iterator = sbc_windows.VCP.iter_physical_monitors(start=display_index)
        handle = None
        has_handle = False
        try:
            handle = next(monitor_iterator)
            has_handle = True
        except StopIteration:
            handle = None
            has_handle = False
        except Exception:
            handle = None
            has_handle = False

        try:
            yield (has_handle, handle)
        finally:
            if has_handle:
                try:
                    from ctypes import windll

                    windll.dxva2.DestroyPhysicalMonitor(handle)
                except Exception:
                    pass
            try:
                monitor_iterator.close()
            except Exception:
                pass

    @staticmethod
    def _read_vcp_feature(
        monitor_handle: Any,
        code: int,
        max_tries: int = 2,
    ) -> tuple[int, int] | None:
        try:
            from ctypes import byref, windll
            from ctypes.wintypes import BYTE, DWORD
        except Exception:
            return None

        code_byte = BYTE(max(0, min(0xFF, int(code))))
        for attempt in range(max_tries):
            current = DWORD()
            maximum = DWORD()
            success = windll.dxva2.GetVCPFeatureAndVCPFeatureReply(
                monitor_handle,
                code_byte,
                None,
                byref(current),
                byref(maximum),
            )
            if success:
                return int(current.value), int(maximum.value)
            time.sleep(0.01 if attempt < 1 else 0.03)
        return None

    @staticmethod
    def _write_vcp_feature(
        monitor_handle: Any,
        code: int,
        value: int,
        max_tries: int = 2,
    ) -> bool:
        try:
            from ctypes import windll
            from ctypes.wintypes import BYTE, DWORD
        except Exception:
            return False

        code_byte = BYTE(max(0, min(0xFF, int(code))))
        target_value = DWORD(max(0, int(value)))
        for attempt in range(max_tries):
            success = windll.dxva2.SetVCPFeature(monitor_handle, code_byte, target_value)
            if success:
                return True
            time.sleep(0.01 if attempt < 1 else 0.03)
        return False

    @classmethod
    def _read_vcp_codes_from_capabilities(cls, monitor_handle: Any) -> list[int]:
        capabilities = cls._read_capabilities_string(monitor_handle)
        if not capabilities:
            return []
        return cls._extract_vcp_codes(capabilities)

    @staticmethod
    def _read_capabilities_string(monitor_handle: Any) -> str | None:
        try:
            from ctypes import byref, create_string_buffer, windll
            from ctypes.wintypes import DWORD
        except Exception:
            return None

        length = DWORD()
        if not windll.dxva2.GetCapabilitiesStringLength(monitor_handle, byref(length)):
            return None
        if length.value <= 0 or length.value > 32_768:
            return None

        buffer = create_string_buffer(length.value)
        if not windll.dxva2.CapabilitiesRequestAndCapabilitiesReply(
            monitor_handle,
            buffer,
            length,
        ):
            return None

        raw = buffer.value
        if not raw:
            return None
        return raw.decode("ascii", errors="ignore")

    @staticmethod
    def _extract_vcp_codes(capabilities: str) -> list[int]:
        marker = "vcp("
        lowered = capabilities.lower()
        start = lowered.find(marker)
        if start < 0:
            return []

        cursor = start + len(marker)
        depth = 1
        payload_chars: list[str] = []
        while cursor < len(capabilities):
            character = capabilities[cursor]
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0:
                    break
            payload_chars.append(character)
            cursor += 1

        payload = "".join(payload_chars)
        codes: list[int] = []
        token_chars: list[str] = []
        nested_depth = 0

        def flush_token() -> None:
            if not token_chars:
                return
            token = "".join(token_chars)
            token_chars.clear()
            if re.fullmatch(r"[0-9a-fA-F]{2}", token):
                codes.append(int(token, 16))

        for character in payload:
            if character == "(":
                if nested_depth == 0:
                    flush_token()
                nested_depth += 1
                continue
            if character == ")":
                if nested_depth > 0:
                    nested_depth -= 1
                if nested_depth == 0:
                    token_chars.clear()
                continue
            if nested_depth > 0:
                continue
            if character.isspace():
                flush_token()
                continue
            token_chars.append(character)
        flush_token()

        return codes

    @staticmethod
    def _normalize_method(method_name: str | None) -> str | None:
        if not method_name:
            return None
        lower_name = method_name.lower()
        if "wmi" in lower_name:
            return "wmi"
        if "vcp" in lower_name:
            return "vcp"
        return None

    @staticmethod
    def _is_generic_monitor_name(name: str) -> bool:
        normalized = re.sub(r"\s+", " ", name.strip().lower())
        if normalized.startswith("none "):
            return True
        return normalized in {
            "",
            "none",
            "display",
            "internal display",
            "built-in display",
            "generic pnp monitor",
            "generic monitor",
        } or normalized.startswith("display ")

    @staticmethod
    def _query_windows_device_name() -> str | None:
        if os.name != "nt":
            return None
        try:
            import winreg  # type: ignore
        except Exception:
            return None

        paths = (
            r"SYSTEM\CurrentControlSet\Control\ComputerName\ActiveComputerName",
            r"SYSTEM\CurrentControlSet\Control\ComputerName\ComputerName",
        )
        for path in paths:
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:  # type: ignore[attr-defined]
                    value, _kind = winreg.QueryValueEx(key, "ComputerName")  # type: ignore[attr-defined]
            except Exception:
                continue
            if isinstance(value, str):
                cleaned = value.strip()
                if cleaned:
                    return cleaned
        return None

    @staticmethod
    def _normalize_device_name_for_ui(name: str) -> str:
        # Windows commonly stores this value uppercase; use title-case only for simple words.
        if name.isupper() and name.isalpha():
            return name.title()
        return name

    @staticmethod
    def system_device_name() -> str | None:
        registry_name = BrightnessService._query_windows_device_name()
        if registry_name:
            return BrightnessService._normalize_device_name_for_ui(registry_name)

        env_name = os.environ.get("COMPUTERNAME", "").strip()
        if env_name:
            return BrightnessService._normalize_device_name_for_ui(env_name)

        host_name = os.environ.get("HOSTNAME", "").strip()
        if host_name:
            return BrightnessService._normalize_device_name_for_ui(host_name)
        return None
