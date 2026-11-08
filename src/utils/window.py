from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass
from typing import Any


class WindowNotFoundError(RuntimeError):
    pass


class WindowActivationError(RuntimeError):
    pass


@dataclass(frozen=True)
class WindowBounds:
    left: int
    top: int
    width: int
    height: int

    def as_region(self) -> tuple[int, int, int, int]:
        return self.left, self.top, self.width, self.height


@dataclass(frozen=True)
class WindowSelector:
    title: str = ""
    process_name: str = ""
    class_name: str = ""
    hwnd: int | None = None


@dataclass(frozen=True)
class ResolvedWindow:
    hwnd: int
    title: str
    process_name: str
    class_name: str


def find_window(title_keyword: str):
    keyword = title_keyword.strip().lower()
    if not keyword:
        return None

    try:
        import pygetwindow as gw
    except ImportError as exc:
        raise WindowNotFoundError("pygetwindow is required for window lookup.") from exc

    for window in gw.getAllWindows():
        title = (window.title or "").strip()
        if keyword in title.lower():
            return window

    raise WindowNotFoundError(f"Cannot find window containing title: {title_keyword!r}")


def find_window_by_hwnd(hwnd: int):
    if hwnd <= 0:
        return None

    try:
        import pygetwindow as gw
    except ImportError as exc:
        raise WindowNotFoundError("pygetwindow is required for window lookup.") from exc

    for window in gw.getAllWindows():
        if int(getattr(window, "_hWnd", 0)) == hwnd:
            return window

    raise WindowNotFoundError(f"Cannot find window with hwnd: {hwnd}")


def _normalize(value: str) -> str:
    return value.strip().lower()


def _matches_text(expected: str, actual: str) -> bool:
    expected = _normalize(expected)
    if not expected:
        return True
    return expected in _normalize(actual)


def _get_process_name(pid: int) -> str:
    if not pid:
        return ""

    try:
        import psutil
    except ImportError:
        return ""

    try:
        return psutil.Process(pid).name()
    except psutil.Error:
        return ""


def _has_win32_filters(selector: WindowSelector) -> bool:
    return bool(selector.process_name.strip() or selector.class_name.strip())


def _resolve_window_win32(selector: WindowSelector) -> ResolvedWindow | None:
    if not sys.platform.startswith("win"):
        if _has_win32_filters(selector):
            raise WindowNotFoundError(
                "window.process_name and window.class_name matching require Windows."
            )
        return None

    try:
        import win32gui
        import win32process
    except ImportError as exc:
        if _has_win32_filters(selector):
            raise WindowNotFoundError(
                "pywin32 is required when window.process_name or window.class_name is configured."
            ) from exc
        return None

    if selector.process_name.strip():
        try:
            import psutil  # noqa: F401
        except ImportError as exc:
            raise WindowNotFoundError(
                "psutil is required when window.process_name is configured."
            ) from exc

    matches: list[ResolvedWindow] = []

    def enum_callback(hwnd: int, _: int) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True

        title = win32gui.GetWindowText(hwnd).strip()
        class_name = win32gui.GetClassName(hwnd).strip()
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        process_name = _get_process_name(pid)

        if selector.hwnd is not None and hwnd != selector.hwnd:
            return True
        if not _matches_text(selector.title, title):
            return True
        if not _matches_text(selector.class_name, class_name):
            return True
        if not _matches_text(selector.process_name, process_name):
            return True

        matches.append(
            ResolvedWindow(
                hwnd=int(hwnd),
                title=title,
                process_name=process_name,
                class_name=class_name,
            )
        )
        return True

    win32gui.EnumWindows(enum_callback, 0)

    if not matches:
        return None
    if len(matches) > 1:
        logging.warning("multiple windows matched selector=%s matches=%s", selector, matches)
    return matches[0]


def parse_window_selector(window_config: dict[str, Any]) -> WindowSelector:
    raw_hwnd = window_config.get("hwnd")
    hwnd = None if raw_hwnd in ("", None) else int(raw_hwnd)
    return WindowSelector(
        title=str(window_config.get("title", "")),
        process_name=str(window_config.get("process_name", "")),
        class_name=str(window_config.get("class_name", "")),
        hwnd=hwnd,
    )


def resolve_window(selector: WindowSelector) -> ResolvedWindow | None:
    if selector.hwnd is not None:
        window = find_window_by_hwnd(selector.hwnd)
        if window is None:
            return None
        return ResolvedWindow(
            hwnd=int(getattr(window, "_hWnd", selector.hwnd)),
            title=str(getattr(window, "title", "")),
            process_name="",
            class_name="",
        )

    resolved = _resolve_window_win32(selector)
    if resolved is not None:
        logging.info("resolved target window=%s", resolved)
        return resolved

    if _has_win32_filters(selector):
        raise WindowNotFoundError(f"Cannot find window matching selector: {selector}")

    window = find_window(selector.title)
    if window is None:
        return None
    resolved = ResolvedWindow(
        hwnd=int(getattr(window, "_hWnd", 0)),
        title=str(getattr(window, "title", "")),
        process_name="",
        class_name="",
    )
    logging.info("resolved target window by title fallback=%s", resolved)
    return resolved


def _get_foreground_hwnd() -> int:
    if not sys.platform.startswith("win"):
        return 0
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    return int(user32.GetForegroundWindow() or 0)


def _is_foreground_window(hwnd: int) -> bool:
    return hwnd > 0 and _get_foreground_hwnd() == int(hwnd)


def _is_window_minimized(hwnd: int) -> bool:
    if not sys.platform.startswith("win") or hwnd <= 0:
        return False
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.IsIconic.argtypes = (wintypes.HWND,)
    user32.IsIconic.restype = wintypes.BOOL
    return bool(user32.IsIconic(wintypes.HWND(int(hwnd))))


def is_window_foreground(hwnd: int) -> bool:
    return _is_foreground_window(hwnd)


def is_window_minimized(hwnd: int) -> bool:
    return _is_window_minimized(hwnd)


def _activate_window_native(hwnd: int) -> bool:
    """Restore and foreground a Win32 window, then verify the result.

    SetForegroundWindow can fail without raising when the calling thread does
    not own the foreground input queue. Temporarily attaching the relevant
    input queues gives UI-triggered worker actions a reliable activation path.
    """

    if not sys.platform.startswith("win") or hwnd <= 0:
        return False

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    hwnd_arg = wintypes.HWND(int(hwnd))

    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.BOOL)
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.ShowWindow.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = (wintypes.HWND,)
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.SetFocus.argtypes = (wintypes.HWND,)
    user32.SetFocus.restype = wintypes.HWND
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    # PyGetWindow's isMinimized value and cached geometry can be stale. Always
    # ask Win32 to restore first; minimized Unreal windows otherwise remain at
    # the sentinel coordinates around (-32000, -32000).
    user32.ShowWindow(hwnd_arg, 9)  # SW_RESTORE
    foreground_hwnd = user32.GetForegroundWindow()
    if int(foreground_hwnd or 0) == int(hwnd) and not _is_window_minimized(hwnd):
        return True

    current_thread = int(kernel32.GetCurrentThreadId())
    foreground_thread = (
        int(user32.GetWindowThreadProcessId(foreground_hwnd, None)) if foreground_hwnd else 0
    )
    target_thread = int(user32.GetWindowThreadProcessId(hwnd_arg, None))
    attached_threads: list[int] = []
    try:
        for thread_id in {foreground_thread, target_thread}:
            if thread_id and thread_id != current_thread:
                if user32.AttachThreadInput(current_thread, thread_id, True):
                    attached_threads.append(thread_id)

        user32.BringWindowToTop(hwnd_arg)
        user32.SetForegroundWindow(hwnd_arg)
        user32.SetFocus(hwnd_arg)
    finally:
        for thread_id in reversed(attached_threads):
            user32.AttachThreadInput(current_thread, thread_id, False)

    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        if _is_foreground_window(hwnd) and not _is_window_minimized(hwnd):
            return True
        time.sleep(0.02)
    return False


def activate_window(
    title_keyword: str = "",
    hwnd: int | None = None,
    selector: WindowSelector | None = None,
    settle_seconds: float = 0.5,
) -> WindowBounds | None:
    if selector is not None:
        resolved = resolve_window(selector)
        window = find_window_by_hwnd(resolved.hwnd) if resolved is not None else None
    else:
        window = find_window_by_hwnd(hwnd) if hwnd else find_window(title_keyword)
    if window is None:
        return None

    if getattr(window, "isMinimized", False):
        window.restore()

    hwnd_value = int(getattr(window, "_hWnd", 0))
    try:
        window.activate()
    except Exception as exc:
        logging.info("pygetwindow activate failed; using native activation: %s", exc)
        if not sys.platform.startswith("win"):
            raise

    if sys.platform.startswith("win") and hwnd_value:
        if not _is_foreground_window(hwnd_value) or _is_window_minimized(hwnd_value):
            logging.info(
                "pygetwindow did not restore/foreground hwnd=%s; using native activation",
                hwnd_value,
            )
            if not _activate_window_native(hwnd_value):
                try:
                    active_hwnd = _get_foreground_hwnd()
                except Exception:
                    active_hwnd = 0
                    logging.exception("failed to read foreground hwnd after activation failure")
                raise WindowActivationError(
                    "Failed to bring target window to foreground. "
                    f"expected={hwnd_value}, active={active_hwnd}"
                )
    time.sleep(settle_seconds)

    bounds = WindowBounds(
        left=int(window.left),
        top=int(window.top),
        width=int(window.width),
        height=int(window.height),
    )
    logging.info("target window activated title=%r bounds=%s", window.title, bounds)
    return bounds


def get_client_bounds(hwnd: int, *, log_result: bool = True) -> WindowBounds:
    if not sys.platform.startswith("win"):
        raise WindowNotFoundError("client bounds are only supported on Windows.")

    import ctypes
    from ctypes import wintypes

    class Rect(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    class Point(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetClientRect.argtypes = (wintypes.HWND, ctypes.POINTER(Rect))
    user32.GetClientRect.restype = wintypes.BOOL
    user32.ClientToScreen.argtypes = (wintypes.HWND, ctypes.POINTER(Point))
    user32.ClientToScreen.restype = wintypes.BOOL

    rect = Rect()
    if not user32.GetClientRect(wintypes.HWND(int(hwnd)), ctypes.byref(rect)):
        raise ctypes.WinError(ctypes.get_last_error())

    origin = Point(0, 0)
    if not user32.ClientToScreen(wintypes.HWND(int(hwnd)), ctypes.byref(origin)):
        raise ctypes.WinError(ctypes.get_last_error())

    bounds = WindowBounds(
        left=int(origin.x),
        top=int(origin.y),
        width=int(rect.right - rect.left),
        height=int(rect.bottom - rect.top),
    )
    if log_result:
        logging.info("target client bounds hwnd=%s bounds=%s", hwnd, bounds)
    return bounds


def get_window_identity(window_config: dict[str, Any]) -> tuple[str, int | None]:
    title = str(window_config.get("title", ""))
    raw_hwnd = window_config.get("hwnd")
    if raw_hwnd in ("", None):
        return title, None
    return title, int(raw_hwnd)
