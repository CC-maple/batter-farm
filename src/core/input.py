from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import pyautogui


class SafetyError(RuntimeError):
    pass


@dataclass(frozen=True)
class InputReleaseResult:
    """Outcome of a best-effort attempt to release every tracked input."""

    attempted_keys: tuple[str, ...]
    attempted_mouse_buttons: tuple[str, ...]
    released_keys: tuple[str, ...]
    released_mouse_buttons: tuple[str, ...]
    failed_keys: tuple[tuple[str, str], ...]
    failed_mouse_buttons: tuple[tuple[str, str], ...]
    remaining_keys: frozenset[str]
    remaining_mouse_buttons: frozenset[str]

    @property
    def success(self) -> bool:
        return not (
            self.failed_keys
            or self.failed_mouse_buttons
            or self.remaining_keys
            or self.remaining_mouse_buttons
        )

    def failure_summary(self) -> str:
        details = [
            *(f"key {name}: {error}" for name, error in self.failed_keys),
            *(f"mouse {name}: {error}" for name, error in self.failed_mouse_buttons),
        ]
        if self.remaining_keys:
            details.append(f"remaining keys={sorted(self.remaining_keys)}")
        if self.remaining_mouse_buttons:
            details.append(f"remaining mouse buttons={sorted(self.remaining_mouse_buttons)}")
        return "; ".join(details) or "no release failures"


def is_admin() -> bool:
    if not sys.platform.startswith("win"):
        return True
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


SUPPORTED_CLIENT_SIZES = {
    (1280, 720),
    (1600, 900),
    (1920, 1080),
    (2560, 1440),
}


@dataclass(frozen=True)
class ClientCoordinateMapper:
    origin: tuple[int, int]
    recorded_size: tuple[int, int]
    current_size: tuple[int, int]

    @classmethod
    def from_recording(
        cls,
        recording: dict[str, Any],
        origin: tuple[int, int],
        current_size: tuple[int, int],
    ) -> "ClientCoordinateMapper":
        window = recording.get("window", {})
        region = window.get("capture_region") if isinstance(window, dict) else None
        if not isinstance(region, list) or len(region) != 4:
            raise SafetyError("Recording is missing window.capture_region [left, top, width, height].")
        recorded_size = int(region[2]), int(region[3])
        if recorded_size[0] <= 0 or recorded_size[1] <= 0:
            raise SafetyError(f"Recording client size is invalid: {recorded_size}")
        normalized_current = int(current_size[0]), int(current_size[1])
        if normalized_current not in SUPPORTED_CLIENT_SIZES:
            raise SafetyError(
                f"Unsupported current client size: {normalized_current}; "
                f"supported={sorted(SUPPORTED_CLIENT_SIZES)}"
            )
        return cls(
            origin=(int(origin[0]), int(origin[1])),
            recorded_size=recorded_size,
            current_size=normalized_current,
        )

    def screen_point(self, client: Any, inside_client: bool) -> tuple[int, int]:
        if not inside_client:
            raise SafetyError("Recording mouse event is outside the game client; absolute fallback is disabled.")
        if not isinstance(client, list) or len(client) != 2:
            raise SafetyError("Recording mouse event has no usable client coordinates.")
        client_x, client_y = int(client[0]), int(client[1])
        recorded_width, recorded_height = self.recorded_size
        if not (0 <= client_x < recorded_width and 0 <= client_y < recorded_height):
            raise SafetyError(
                f"Recording client point is out of bounds: {(client_x, client_y)} not within {self.recorded_size}"
            )
        current_width, current_height = self.current_size
        scaled_x = min(current_width - 1, int(round(client_x * current_width / recorded_width)))
        scaled_y = min(current_height - 1, int(round(client_y * current_height / recorded_height)))
        return self.origin[0] + scaled_x, self.origin[1] + scaled_y


@dataclass
class InputController:
    _COMMON_RELEASE_KEYS = ("shift", "ctrl", "alt", "win", "w", "a", "s", "d", "e", "q")

    click_delay_seconds: float = 0.15
    click_hold_seconds: float = 0.08
    move_duration_seconds: float = 0.05
    click_backend: str = "pyautogui"
    fail_safe: bool = True
    target_window_title: str = ""
    target_window_hwnd: int | None = None
    enabled: bool = True
    held_keys: set[str] = field(default_factory=set, init=False)
    held_mouse_buttons: set[str] = field(default_factory=set, init=False)

    def __post_init__(self) -> None:
        pyautogui.FAILSAFE = self.fail_safe

    def safe_click(self, x: int, y: int) -> None:
        self._ensure_enabled()
        backend = self.click_backend.lower()
        if backend not in {"postmessage", "post_message"}:
            self.assert_target_window_active()
        logging.info("click target x=%s y=%s backend=%s", x, y, self.click_backend)
        if backend in {"postmessage", "post_message"}:
            self._postmessage_click(x, y)
        elif backend in {"sendinput", "send_input"}:
            self._sendinput_click(x, y)
        elif backend == "win32":
            self._win32_click(x, y)
        else:
            self._pyautogui_click(x, y)
        time.sleep(self.click_delay_seconds)

    def move_to(self, x: int, y: int, duration_seconds: float | None = None) -> None:
        self._ensure_enabled()
        self.assert_target_window_active()
        backend = self.click_backend.lower()
        duration = self.move_duration_seconds if duration_seconds is None else max(0.0, float(duration_seconds))
        logging.info(
            "move target x=%s y=%s backend=%s duration=%.4f",
            x,
            y,
            self.click_backend,
            duration,
        )
        if backend in {"postmessage", "post_message"}:
            self._postmessage_move(x, y)
        elif backend in {"sendinput", "send_input"}:
            self._sendinput_move(x, y)
        elif backend == "win32":
            self._win32_move(x, y)
        else:
            pyautogui.moveTo(x, y, duration=duration)
        if duration > 0:
            time.sleep(duration)

    def move_relative(self, dx: int, dy: int, duration_seconds: float | None = None) -> None:
        self._ensure_enabled()
        self.assert_target_window_active()
        backend = self.click_backend.lower()
        duration = self.move_duration_seconds if duration_seconds is None else float(duration_seconds)
        logging.info("move relative dx=%s dy=%s backend=%s", dx, dy, self.click_backend)
        if backend in {"sendinput", "send_input"} and sys.platform.startswith("win"):
            self._sendinput_mouse(dx=int(dx), dy=int(dy), flags=0x0001)
        elif backend == "win32" and sys.platform.startswith("win"):
            x, y = pyautogui.position()
            self._win32_move(int(x) + int(dx), int(y) + int(dy))
        else:
            pyautogui.moveRel(int(dx), int(dy), duration=duration)
        if duration > 0:
            time.sleep(duration)

    def drag_relative(
        self,
        dx: int,
        dy: int,
        button: str = "left",
        duration_seconds: float | None = None,
        steps: int = 12,
    ) -> None:
        self._ensure_enabled()
        self.assert_target_window_active()
        backend = self.click_backend.lower()
        duration = self.move_duration_seconds if duration_seconds is None else float(duration_seconds)
        normalized = str(button).strip().lower()
        step_count = max(1, int(steps))
        logging.info(
            "drag relative dx=%s dy=%s button=%s steps=%s backend=%s",
            dx,
            dy,
            normalized,
            step_count,
            self.click_backend,
        )

        if backend in {"sendinput", "send_input"} and sys.platform.startswith("win"):
            down, up, mouse_data = self._mouse_button_input(normalized)
            self._sendinput_mouse(flags=down, mouse_data=mouse_data)
            self.held_mouse_buttons.add(normalized)
            try:
                remaining_x = int(dx)
                remaining_y = int(dy)
                for index in range(step_count):
                    steps_left = step_count - index
                    move_x = int(round(remaining_x / steps_left))
                    move_y = int(round(remaining_y / steps_left))
                    remaining_x -= move_x
                    remaining_y -= move_y
                    self._sendinput_mouse(dx=move_x, dy=move_y, flags=0x0001)
                    if duration > 0:
                        time.sleep(duration / step_count)
            finally:
                self._sendinput_mouse(flags=up, mouse_data=mouse_data)
                self.held_mouse_buttons.discard(normalized)
        else:
            pyautogui.dragRel(int(dx), int(dy), duration=duration, button=normalized)

        if duration > 0:
            time.sleep(min(0.05, duration))

    def key_down(self, key: str) -> None:
        self._ensure_enabled()
        self.assert_target_window_active()
        normalized = self._normalize_key(key)
        logging.info("key down key=%s backend=%s", normalized, self.click_backend)
        if self.click_backend.lower() in {"sendinput", "send_input"} and sys.platform.startswith("win"):
            self._sendinput_key(normalized, key_up=False)
        else:
            pyautogui.keyDown(normalized)
        self.held_keys.add(normalized)

    def key_up(self, key: str) -> None:
        self._ensure_enabled()
        normalized = self._normalize_key(key)
        logging.info("key up key=%s backend=%s", normalized, self.click_backend)
        if self.click_backend.lower() in {"sendinput", "send_input"} and sys.platform.startswith("win"):
            self._sendinput_key(normalized, key_up=True)
        else:
            pyautogui.keyUp(normalized)
        self.held_keys.discard(normalized)

    def press(self, key: str, hold_seconds: float = 0.05) -> None:
        self.key_down(key)
        try:
            time.sleep(max(0.0, float(hold_seconds)))
        finally:
            self.key_up(key)

    def click_button(self, button: str = "left") -> None:
        self._ensure_enabled()
        self.assert_target_window_active()
        normalized = str(button).strip().lower()
        logging.info("click button button=%s backend=%s", normalized, self.click_backend)
        if self.click_backend.lower() in {"sendinput", "send_input"} and sys.platform.startswith("win"):
            down, up, mouse_data = self._mouse_button_input(normalized)
            self._sendinput_mouse(flags=down, mouse_data=mouse_data)
            self.held_mouse_buttons.add(normalized)
            try:
                time.sleep(self.click_hold_seconds)
            finally:
                self._sendinput_mouse(flags=up, mouse_data=mouse_data)
                self.held_mouse_buttons.discard(normalized)
        else:
            pyautogui.mouseDown(button=normalized)
            self.held_mouse_buttons.add(normalized)
            try:
                time.sleep(self.click_hold_seconds)
            finally:
                pyautogui.mouseUp(button=normalized)
                self.held_mouse_buttons.discard(normalized)
        time.sleep(self.click_delay_seconds)

    def right_click(self) -> None:
        self.click_button("right")

    def mouse_down(self, button: str = "left", x: int | None = None, y: int | None = None) -> None:
        self._ensure_enabled()
        self.assert_target_window_active()
        normalized = str(button).strip().lower()
        logging.info("mouse down button=%s x=%s y=%s backend=%s", normalized, x, y, self.click_backend)
        if x is not None and y is not None:
            self.move_to(int(x), int(y))

        if self.click_backend.lower() in {"sendinput", "send_input"} and sys.platform.startswith("win"):
            down, _, mouse_data = self._mouse_button_input(normalized)
            self._sendinput_mouse(flags=down, mouse_data=mouse_data)
        else:
            pyautogui.mouseDown(button=normalized)
        self.held_mouse_buttons.add(normalized)

    def mouse_up(self, button: str = "left", after_delay_seconds: float | None = None) -> None:
        self._ensure_enabled()
        normalized = str(button).strip().lower()
        logging.info("mouse up button=%s backend=%s", normalized, self.click_backend)
        if self.click_backend.lower() in {"sendinput", "send_input"} and sys.platform.startswith("win"):
            _, up, mouse_data = self._mouse_button_input(normalized)
            self._sendinput_mouse(flags=up, mouse_data=mouse_data)
        else:
            pyautogui.mouseUp(button=normalized)
        self.held_mouse_buttons.discard(normalized)
        delay = self.click_delay_seconds if after_delay_seconds is None else max(0.0, float(after_delay_seconds))
        if delay > 0:
            time.sleep(delay)

    def _mouse_button_flags(self, button: str) -> tuple[int, int]:
        down, up, _ = self._mouse_button_input(button)
        return down, up

    def _mouse_button_input(self, button: str) -> tuple[int, int, int]:
        normalized = str(button).strip().lower()
        if normalized == "left":
            return 0x0002, 0x0004, 0
        if normalized == "right":
            return 0x0008, 0x0010, 0
        if normalized == "middle":
            return 0x0020, 0x0040, 0
        if normalized == "x1":
            return 0x0080, 0x0100, 0x0001
        if normalized == "x2":
            return 0x0080, 0x0100, 0x0002
        raise ValueError(f"Unsupported mouse button for SendInput: {button!r}")

    def scroll(self, dx: int = 0, dy: int = 0) -> None:
        self._ensure_enabled()
        self.assert_target_window_active()
        horizontal = int(dx)
        vertical = int(dy)
        logging.info("mouse scroll dx=%s dy=%s backend=%s", horizontal, vertical, self.click_backend)
        if self.click_backend.lower() in {"sendinput", "send_input"} and sys.platform.startswith("win"):
            if vertical:
                self._sendinput_mouse(flags=0x0800, mouse_data=(vertical * 120) & 0xFFFFFFFF)
            if horizontal:
                self._sendinput_mouse(flags=0x1000, mouse_data=(horizontal * 120) & 0xFFFFFFFF)
            return
        if vertical:
            pyautogui.scroll(vertical)
        if horizontal:
            pyautogui.hscroll(horizontal)

    def toggle_mouse_mode(self, key: str = "`") -> None:
        logging.info("toggle mouse mode key=%s", key)
        self.press(key, hold_seconds=0.05)

    def _pyautogui_click(self, x: int, y: int) -> None:
        pyautogui.moveTo(x, y, duration=self.move_duration_seconds)
        pyautogui.mouseDown(x=x, y=y)
        self.held_mouse_buttons.add("left")
        try:
            time.sleep(self.click_hold_seconds)
        finally:
            pyautogui.mouseUp(x=x, y=y)
            self.held_mouse_buttons.discard("left")

    def _win32_click(self, x: int, y: int) -> None:
        if not sys.platform.startswith("win"):
            self._pyautogui_click(x, y)
            return

        user32 = self._get_user32()
        self._win32_move(x, y)
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        self.held_mouse_buttons.add("left")
        try:
            time.sleep(self.click_hold_seconds)
        finally:
            user32.mouse_event(0x0004, 0, 0, 0, 0)
            self.held_mouse_buttons.discard("left")

    def _win32_move(self, x: int, y: int) -> None:
        if not sys.platform.startswith("win"):
            pyautogui.moveTo(x, y, duration=self.move_duration_seconds)
            return

        user32 = self._get_user32()
        if not user32.SetCursorPos(int(x), int(y)):
            import ctypes

            raise ctypes.WinError(ctypes.get_last_error())

    def _sendinput_click(self, x: int, y: int) -> None:
        if not sys.platform.startswith("win"):
            self._pyautogui_click(x, y)
            return

        self._sendinput_move(x, y)
        time.sleep(self.move_duration_seconds)
        self._sendinput_mouse(flags=0x0002)
        self.held_mouse_buttons.add("left")
        try:
            time.sleep(self.click_hold_seconds)
        finally:
            self._sendinput_mouse(flags=0x0004)
            self.held_mouse_buttons.discard("left")

    def _sendinput_move(self, x: int, y: int) -> None:
        if not sys.platform.startswith("win"):
            pyautogui.moveTo(x, y, duration=self.move_duration_seconds)
            return

        normalized_x, normalized_y, desktop = self._normalize_for_virtual_desktop(x, y)
        logging.info(
            "sendinput absolute move screen=(%s, %s) normalized=(%s, %s) virtual_desktop=%s",
            x,
            y,
            normalized_x,
            normalized_y,
            desktop,
        )
        self._sendinput_mouse(
            dx=normalized_x,
            dy=normalized_y,
            flags=0x0001 | 0x8000 | 0x4000,
        )

    def _sendinput_mouse(self, flags: int, dx: int = 0, dy: int = 0, mouse_data: int = 0) -> None:
        import ctypes
        from ctypes import wintypes

        user32 = self._get_user32()
        ulong_ptr = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

        class MouseInput(ctypes.Structure):
            _fields_ = [
                ("dx", wintypes.LONG),
                ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ulong_ptr),
            ]

        class InputUnion(ctypes.Union):
            _fields_ = [("mi", MouseInput)]

        class Input(ctypes.Structure):
            _anonymous_ = ("union",)
            _fields_ = [
                ("type", wintypes.DWORD),
                ("union", InputUnion),
            ]

        item = Input()
        item.type = 0
        item.mi = MouseInput(int(dx), int(dy), int(mouse_data), int(flags), 0, 0)

        user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int)
        user32.SendInput.restype = wintypes.UINT
        sent = user32.SendInput(1, ctypes.byref(item), ctypes.sizeof(Input))
        if sent != 1:
            raise ctypes.WinError(ctypes.get_last_error())

    def _sendinput_key(self, key: str, key_up: bool) -> None:
        import ctypes
        from ctypes import wintypes

        user32 = self._get_user32()
        ulong_ptr = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

        class KeyboardInput(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ulong_ptr),
            ]

        class MouseInput(ctypes.Structure):
            _fields_ = [
                ("dx", wintypes.LONG),
                ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ulong_ptr),
            ]

        class HardwareInput(ctypes.Structure):
            _fields_ = [
                ("uMsg", wintypes.DWORD),
                ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD),
            ]

        class InputUnion(ctypes.Union):
            _fields_ = [("mi", MouseInput), ("ki", KeyboardInput), ("hi", HardwareInput)]

        class Input(ctypes.Structure):
            _anonymous_ = ("union",)
            _fields_ = [
                ("type", wintypes.DWORD),
                ("union", InputUnion),
            ]

        vk = self._virtual_key(key)
        user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
        user32.MapVirtualKeyW.restype = wintypes.UINT
        scan_code = int(user32.MapVirtualKeyW(vk, 0))
        flags = 0x0008 | (0x0002 if key_up else 0)

        item = Input()
        item.type = 1
        item.ki = KeyboardInput(0, scan_code, flags, 0, 0)
        user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int)
        user32.SendInput.restype = wintypes.UINT
        sent = user32.SendInput(1, ctypes.byref(item), ctypes.sizeof(Input))
        if sent != 1:
            raise ctypes.WinError(ctypes.get_last_error())

    def _postmessage_click(self, x: int, y: int) -> None:
        if not sys.platform.startswith("win"):
            self._pyautogui_click(x, y)
            return

        hwnd = self._resolve_target_hwnd()
        client_x, client_y = self._screen_to_client(hwnd, x, y)
        l_param = self._make_lparam(client_x, client_y)
        user32 = self._get_user32()
        logging.info(
            "postmessage click hwnd=%s screen=(%s, %s) client=(%s, %s)",
            hwnd,
            x,
            y,
            client_x,
            client_y,
        )
        user32.PostMessageW(hwnd, 0x0006, 1, 0)
        user32.PostMessageW(hwnd, 0x0200, 0, l_param)
        time.sleep(self.move_duration_seconds)
        user32.PostMessageW(hwnd, 0x0201, 0x0001, l_param)
        time.sleep(self.click_hold_seconds)
        user32.PostMessageW(hwnd, 0x0202, 0, l_param)

    def _postmessage_move(self, x: int, y: int) -> None:
        if not sys.platform.startswith("win"):
            pyautogui.moveTo(x, y, duration=self.move_duration_seconds)
            return

        hwnd = self._resolve_target_hwnd()
        client_x, client_y = self._screen_to_client(hwnd, x, y)
        l_param = self._make_lparam(client_x, client_y)
        user32 = self._get_user32()
        logging.info(
            "postmessage move hwnd=%s screen=(%s, %s) client=(%s, %s)",
            hwnd,
            x,
            y,
            client_x,
            client_y,
        )
        user32.PostMessageW(hwnd, 0x0200, 0, l_param)

    def _screen_to_client(self, hwnd: int, x: int, y: int) -> tuple[int, int]:
        import ctypes
        from ctypes import wintypes

        class Point(ctypes.Structure):
            _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

        point = Point(int(x), int(y))
        user32 = self._get_user32()
        user32.ScreenToClient.argtypes = (wintypes.HWND, ctypes.POINTER(Point))
        user32.ScreenToClient.restype = wintypes.BOOL
        if not user32.ScreenToClient(wintypes.HWND(int(hwnd)), ctypes.byref(point)):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(point.x), int(point.y)

    def _resolve_target_hwnd(self) -> int:
        if self.target_window_hwnd:
            return int(self.target_window_hwnd)

        user32 = self._get_user32()
        hwnd = int(user32.GetForegroundWindow())
        if hwnd:
            return hwnd

        raise SafetyError("Cannot resolve a target hwnd for postmessage input.")

    def _make_lparam(self, x: int, y: int) -> int:
        return (int(x) & 0xFFFF) | ((int(y) & 0xFFFF) << 16)

    def _normalize_for_virtual_desktop(self, x: int, y: int) -> tuple[int, int, dict[str, int]]:
        user32 = self._get_user32()
        left = int(user32.GetSystemMetrics(76))
        top = int(user32.GetSystemMetrics(77))
        width = int(user32.GetSystemMetrics(78))
        height = int(user32.GetSystemMetrics(79))
        if width <= 0 or height <= 0:
            screen_width, screen_height = pyautogui.size()
            left, top, width, height = 0, 0, int(screen_width), int(screen_height)

        normalized_x = round((int(x) - left) * 65535 / max(width - 1, 1))
        normalized_y = round((int(y) - top) * 65535 / max(height - 1, 1))
        normalized_x = max(0, min(65535, normalized_x))
        normalized_y = max(0, min(65535, normalized_y))
        return normalized_x, normalized_y, {
            "left": left,
            "top": top,
            "width": width,
            "height": height,
        }

    def _get_user32(self) -> Any:
        import ctypes

        return ctypes.WinDLL("user32", use_last_error=True)

    def _normalize_key(self, key: str) -> str:
        value = str(key).strip().lower()
        aliases = {
            "alt_l": "alt",
            "alt_r": "alt",
            "ctrl_l": "ctrl",
            "ctrl_r": "ctrl",
            "control_l": "ctrl",
            "control_r": "ctrl",
            "shift_l": "shift",
            "shift_r": "shift",
            "win_l": "win",
            "win_r": "win",
        }
        if value in aliases:
            return aliases[value]
        if value == "backtick":
            return "`"
        return value

    def _virtual_key(self, key: str) -> int:
        key = self._normalize_key(key)
        special = {
            "`": 0xC0,
            "space": 0x20,
            "enter": 0x0D,
            "esc": 0x1B,
            "escape": 0x1B,
            "shift": 0x10,
            "ctrl": 0x11,
            "control": 0x11,
            "alt": 0x12,
            "win": 0x5B,
            "tab": 0x09,
        }
        if key in special:
            return special[key]
        if key.startswith("f") and key[1:].isdigit():
            function_number = int(key[1:])
            if 1 <= function_number <= 24:
                return 0x70 + function_number - 1
        if len(key) == 1 and "a" <= key <= "z":
            return ord(key.upper())
        if len(key) == 1 and "0" <= key <= "9":
            return ord(key)
        raise ValueError(f"Unsupported key for SendInput: {key!r}")

    def assert_target_window_active(self) -> None:
        if self.target_window_hwnd and sys.platform.startswith("win"):
            user32 = self._get_user32()
            active_hwnd = int(user32.GetForegroundWindow())
            if active_hwnd != int(self.target_window_hwnd):
                self._activate_target_window()
                active_hwnd = int(user32.GetForegroundWindow())
            if active_hwnd != int(self.target_window_hwnd):
                raise SafetyError(
                    "Active hwnd does not match target. "
                    f"expected={self.target_window_hwnd}, active={active_hwnd}"
                )
            return

        expected_title = self.target_window_title.strip()
        if not expected_title:
            return

        try:
            import pygetwindow as gw
        except ImportError as exc:
            raise SafetyError(
                "pygetwindow is required when window.title is configured."
            ) from exc

        active_window = gw.getActiveWindow()
        active_title = active_window.title if active_window else ""
        if expected_title.lower() not in active_title.lower():
            self._activate_target_window()
            active_window = gw.getActiveWindow()
            active_title = active_window.title if active_window else ""

        if expected_title.lower() not in active_title.lower():
            raise SafetyError(
                "Active window does not match target. "
                f"expected contains={expected_title!r}, active={active_title!r}"
            )

    def _activate_target_window(self) -> None:
        try:
            from src.utils.window import activate_window

            activate_window(
                title_keyword=self.target_window_title,
                hwnd=self.target_window_hwnd,
                settle_seconds=0.2,
            )
        except Exception:
            logging.exception("failed to activate target window before input")

    def release_all(self) -> InputReleaseResult:
        attempted_keys = tuple(dict.fromkeys((*sorted(self.held_keys), *self._COMMON_RELEASE_KEYS)))
        attempted_mouse_buttons = tuple(sorted(self.held_mouse_buttons))
        if not self.enabled:
            self.held_keys.clear()
            self.held_mouse_buttons.clear()
            return InputReleaseResult(
                attempted_keys=(),
                attempted_mouse_buttons=(),
                released_keys=(),
                released_mouse_buttons=(),
                failed_keys=(),
                failed_mouse_buttons=(),
                remaining_keys=frozenset(),
                remaining_mouse_buttons=frozenset(),
            )

        released_keys: list[str] = []
        released_mouse_buttons: list[str] = []
        failed_keys: list[tuple[str, str]] = []
        failed_mouse_buttons: list[tuple[str, str]] = []

        for key in attempted_keys:
            try:
                self.key_up(key)
                released_keys.append(key)
            except Exception as exc:
                # A failed release is still potentially held.  Keep it in the
                # tracked state so strict callers cannot mistake it for success.
                self.held_keys.add(key)
                failed_keys.append((key, f"{type(exc).__name__}: {exc}"))
                logging.exception("failed to release held key: %s", key)

        for button in attempted_mouse_buttons:
            try:
                self.mouse_up(button, after_delay_seconds=0.0)
                released_mouse_buttons.append(button)
            except Exception as exc:
                self.held_mouse_buttons.add(button)
                failed_mouse_buttons.append((button, f"{type(exc).__name__}: {exc}"))
                logging.exception("failed to release mouse button: %s", button)

        return InputReleaseResult(
            attempted_keys=attempted_keys,
            attempted_mouse_buttons=attempted_mouse_buttons,
            released_keys=tuple(released_keys),
            released_mouse_buttons=tuple(released_mouse_buttons),
            failed_keys=tuple(failed_keys),
            failed_mouse_buttons=tuple(failed_mouse_buttons),
            remaining_keys=frozenset(self.held_keys),
            remaining_mouse_buttons=frozenset(self.held_mouse_buttons),
        )

    def _ensure_enabled(self) -> None:
        if not self.enabled:
            raise SafetyError("Input dispatch is disabled in dry-run preview mode.")
