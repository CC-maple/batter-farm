from __future__ import annotations

import logging
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from src.core.capture import Screenshot
from src.utils.window import is_window_foreground, is_window_minimized


RegionTuple = tuple[int, int, int, int]


def _owner_is_active(owner_hwnd: int | None) -> bool:
    if not owner_hwnd or not sys.platform.startswith("win"):
        return True
    return is_window_foreground(owner_hwnd) and not is_window_minimized(owner_hwnd)


def _native_overlay_hwnd(root: Any) -> int:
    """Return Tk's outer top-level HWND instead of its internal child HWND."""

    hwnd = int(root.winfo_id())
    if not sys.platform.startswith("win"):
        return hwnd

    import ctypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    get_parent = user32.GetParent
    get_parent.argtypes = (ctypes.c_void_p,)
    get_parent.restype = ctypes.c_void_p
    get_window_long = user32.GetWindowLongW
    get_window_long.argtypes = (ctypes.c_void_p, ctypes.c_int)
    get_window_long.restype = ctypes.c_long
    gwl_style = -16
    ws_child = 0x40000000
    while get_window_long(hwnd, gwl_style) & ws_child:
        parent = int(get_parent(hwnd) or 0)
        if not parent or parent == hwnd:
            break
        hwnd = parent
    return hwnd


def _apply_native_overlay_styles(root: Any, *, click_through: bool, owner_hwnd: int | None) -> None:
    """Make a Tk overlay transparent/no-activate and owned by the game window."""

    if not sys.platform.startswith("win"):
        return

    import ctypes

    hwnd = _native_overlay_hwnd(root)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    get_window_long = user32.GetWindowLongW
    set_window_long = user32.SetWindowLongW
    get_window_long.argtypes = (ctypes.c_void_p, ctypes.c_int)
    get_window_long.restype = ctypes.c_long
    set_window_long.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_long)
    set_window_long.restype = ctypes.c_long

    gwl_exstyle = -20
    ws_ex_topmost = 0x00000008
    ws_ex_transparent = 0x00000020
    ws_ex_toolwindow = 0x00000080
    ws_ex_noactivate = 0x08000000
    style = get_window_long(hwnd, gwl_exstyle)
    extra_style = ws_ex_toolwindow | ws_ex_noactivate
    if click_through:
        extra_style |= ws_ex_transparent
    # Tk owns WS_EX_LAYERED for alpha/transparent-color. Only remove the
    # global-topmost bit and add the input/tool-window styles here.
    set_window_long(hwnd, gwl_exstyle, (style | extra_style) & ~ws_ex_topmost)

    if owner_hwnd:
        gwlp_hwndparent = -8
        set_owner = getattr(user32, "SetWindowLongPtrW", None)
        if set_owner is not None:
            set_owner.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t)
            set_owner.restype = ctypes.c_ssize_t
        else:
            set_owner = user32.SetWindowLongW
            set_owner.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_long)
            set_owner.restype = ctypes.c_long
        ctypes.set_last_error(0)
        previous_owner = set_owner(hwnd, gwlp_hwndparent, int(owner_hwnd))
        if not previous_owner and ctypes.get_last_error():
            raise ctypes.WinError(ctypes.get_last_error())


def _position_overlay_window(
    root: Any,
    x: int,
    y: int,
    width: int,
    height: int,
) -> None:
    if not sys.platform.startswith("win"):
        return

    import ctypes

    hwnd = _native_overlay_hwnd(root)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    set_window_pos = user32.SetWindowPos
    set_window_pos.argtypes = (
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
    )
    set_window_pos.restype = ctypes.c_bool

    hwnd_top = ctypes.c_void_p(0)
    swp_noactivate = 0x0010
    swp_showwindow = 0x0040
    swp_noownerzorder = 0x0200
    if not set_window_pos(
        hwnd,
        hwnd_top,
        int(x),
        int(y),
        int(width),
        int(height),
        swp_noactivate | swp_showwindow | swp_noownerzorder,
    ):
        raise ctypes.WinError(ctypes.get_last_error())


_DISPLAY_NAMES = {
    "marker_sequence": "标识任务",
    "marker_sequence_restart": "恢复弹窗后重新扫描",
    "recording_plan": "录制回放计划",
    "farm_field_latest": "农田任务",
    "fishpond_latest": "鱼塘任务",
    "pasture_latest": "牧场任务",
    "processor_latest": "加工区任务",
    "restaurant_service_latest": "餐厅任务",
    "watering": "浇水",
    "harvest": "收获",
    "fishing": "钓鱼",
    "midwifery": "助产",
    "fish_card_result": "恭喜获得鱼卡",
    "farm_header": "农场标题",
    "restaurant_reception_probe": "餐厅接待检查",
    "none": "无",
    "left": "左键",
    "right": "右键",
    "middle": "中键",
}


@dataclass
class StatusOverlay:
    config: dict[str, Any]
    owner_hwnd: int | None = None
    enabled: bool = field(init=False)
    root: Any = field(default=None, init=False)
    canvas: Any = field(default=None, init=False)
    visible: bool = field(default=False, init=False)
    show_requested: bool = field(default=True, init=False)
    state: dict[str, str] = field(default_factory=dict, init=False)
    history: list[str] = field(default_factory=list, init=False)
    last_origin: tuple[int, int] | None = field(default=None, init=False)
    last_client_size: tuple[int, int] | None = field(default=None, init=False)
    last_geometry: tuple[int, int, int, int] | None = field(default=None, init=False)
    last_pump_at: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        self.enabled = bool(self.config.get("enabled", False))
        self.show_requested = self.enabled and bool(self.config.get("start_visible", True))
        self.state.update(
            {
                "run_state": "idle",
                "current_step": "-",
                "last_marker": "-",
                "last_score": "-",
                "active_keys": "-",
                "mouse": "-",
                "last_action": "-",
            }
        )

    @property
    def hide_before_capture(self) -> bool:
        return bool(self.config.get("hide_before_capture", True))

    def update_status(self, **items: Any) -> None:
        if not self.enabled:
            return
        log_message = items.pop("log", None)
        for key, value in items.items():
            if value is None:
                continue
            if isinstance(value, float):
                self.state[key] = f"{value:.4f}"
            else:
                self.state[key] = _localize_overlay_value(key, value)
        if log_message is not None:
            self._append_log(_localize_overlay_message(log_message))
        self._refresh()

    def add_log(self, message: str) -> None:
        if not self.enabled:
            return
        self._append_log(_localize_overlay_message(message))
        self._refresh()

    def _append_log(self, message: str) -> None:
        normalized = " ".join(str(message).split())
        if not normalized:
            return
        max_log_lines = max(1, int(self.config.get("max_log_lines", 3)))
        self.history.append(normalized)
        if len(self.history) > max_log_lines:
            del self.history[:-max_log_lines]

    def _refresh(self) -> None:
        if self.last_origin is not None and self.show_requested:
            if self.visible:
                self._render_status()
            else:
                self.show()

    def bind_capture(self, capture: Screenshot) -> None:
        if not self.enabled:
            return
        if capture.region is not None:
            client_size = (int(capture.region[2]), int(capture.region[3]))
        else:
            height, width = capture.image.shape[:2]
            client_size = (int(width), int(height))
        self.bind_client_geometry(capture.origin, client_size)

    def bind_client_geometry(
        self,
        origin: tuple[int, int],
        client_size: tuple[int, int],
    ) -> None:
        """Move the existing window with the game client without recreating it."""
        if not self.enabled:
            return
        normalized_origin = int(origin[0]), int(origin[1])
        normalized_size = max(1, int(client_size[0])), max(1, int(client_size[1]))
        if normalized_origin == self.last_origin and normalized_size == self.last_client_size:
            return

        previous_size = self.last_client_size
        self.last_origin = normalized_origin
        self.last_client_size = normalized_size
        if self.visible:
            try:
                self._apply_geometry(redraw=previous_size != normalized_size)
                self.pump(force=True)
            except Exception:
                logging.exception("failed to follow target window with status overlay; disabling overlay")
                self._disable_after_error()

    def show(self) -> None:
        if not self.enabled:
            return
        self.show_requested = True
        if not _owner_is_active(self.owner_hwnd):
            self.hide(temporary=True)
            return
        try:
            self._ensure_window()
            geometry = self._apply_geometry(redraw=True)
            if not self.visible:
                self.root.deiconify()
                self.root.lift()
                self.root.update_idletasks()
                self.root.update()
                if geometry is not None:
                    self._apply_owned_position(*geometry)
            self.visible = True
        except Exception:
            logging.exception("failed to show status overlay; disabling overlay")
            self._disable_after_error()

    def _render_status(self) -> None:
        """Update the persistent canvas in place without hiding/showing its window."""
        try:
            self._apply_geometry(redraw=True)
            self.pump(force=True)
        except Exception:
            logging.exception("failed to refresh status overlay; disabling overlay")
            self._disable_after_error()

    def _apply_geometry(self, redraw: bool) -> tuple[int, int, int, int] | None:
        geometry = self._geometry()
        if geometry is None or self.root is None or self.canvas is None:
            return geometry

        x, y, width, height = geometry
        if geometry != self.last_geometry:
            previous_geometry = self.last_geometry
            previous_size = previous_geometry[2:] if previous_geometry is not None else None
            self.root.geometry(f"{width}x{height}+{x}+{y}")
            if previous_size != (width, height):
                self.canvas.configure(width=width, height=height)
                redraw = True
            self._apply_owned_position(x, y, width, height)
            log_geometry = (
                logging.info
                if previous_geometry is None or previous_size != (width, height)
                else logging.debug
            )
            log_geometry("status overlay geometry screen=(%s,%s,%s,%s)", x, y, width, height)
            self.last_geometry = geometry
        if redraw:
            self._draw_status(width, height)
        return geometry

    def hide(self, temporary: bool = False) -> None:
        if not temporary:
            self.show_requested = False
        if self.root is None:
            self.visible = False
            return
        try:
            self.root.withdraw()
            self.root.update_idletasks()
            self.root.update()
            self.visible = False
        except Exception:
            logging.exception("failed to hide status overlay")
            self._disable_after_error()

    def toggle(self) -> bool:
        if not self.enabled:
            return False
        return self.set_show_requested(not self.show_requested)

    def set_show_requested(self, show: bool) -> bool:
        self.show_requested = bool(show) and self.enabled
        if self.show_requested:
            if self.last_origin is not None:
                self.show()
        else:
            self.hide()
        return self.show_requested

    def pump(self, force: bool = False) -> None:
        if not self.enabled or self.root is None:
            return
        now = time.monotonic()
        interval = max(0.01, float(self.config.get("pump_interval_ms", 50)) / 1000.0)
        if not force and now - self.last_pump_at < interval:
            return
        try:
            self.root.update_idletasks()
            self.root.update()
            self.last_pump_at = now
        except Exception:
            logging.exception("failed to pump status overlay events; disabling overlay")
            self._disable_after_error()

    def close(self) -> None:
        self._destroy_window("failed to close status overlay")

    def _disable_after_error(self) -> None:
        self.enabled = False
        self.show_requested = False
        self._destroy_window("failed to destroy disabled status overlay")

    def _destroy_window(self, error_message: str) -> None:
        root = self.root
        self.visible = False
        if root is None:
            self.canvas = None
            return
        try:
            root.withdraw()
        except Exception:
            logging.warning("failed to withdraw status overlay during cleanup", exc_info=True)
        try:
            root.destroy()
        except Exception:
            logging.warning(error_message, exc_info=True)
            return
        if self.root is root:
            self.root = None
            self.canvas = None

    def _ensure_window(self) -> None:
        if self.root is not None:
            return

        import tkinter as tk

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", False)
        panel_color = str(self.config.get("panel_color", "#10151d"))
        self.root.configure(bg=panel_color)

        self.canvas = tk.Canvas(
            self.root,
            bg=panel_color,
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(fill="both", expand=True)
        self._apply_window_styles()
        self._apply_opacity()

    def _apply_opacity(self) -> None:
        opacity = float(self.config.get("opacity", 0.72))
        opacity = min(1.0, max(0.05, opacity))
        self.root.attributes("-alpha", opacity)

    def _apply_window_styles(self) -> None:
        _apply_native_overlay_styles(
            self.root,
            click_through=bool(self.config.get("click_through", True)),
            owner_hwnd=self.owner_hwnd,
        )

    def _apply_owned_position(self, x: int, y: int, width: int, height: int) -> None:
        if self.root is None:
            return
        _position_overlay_window(self.root, x, y, width, height)

    def _draw_status(self, width: int, height: int) -> None:
        if self.canvas is None:
            return

        self.canvas.delete("all")
        base_width, base_height = _normalize_size(self.config.get("region", [0, 690, 520, 210])[2:])
        scale = min(width / base_width, height / base_height)
        scale = min(1.4, max(0.72, scale))
        font_family = str(self.config.get("font_family", "Microsoft YaHei UI"))
        base_font_size = max(10, int(self.config.get("font_size", 13)))
        title_size = max(9, int(round((base_font_size - 2) * scale)))
        step_size = max(11, int(round((base_font_size + 2) * scale)))
        log_size = max(9, int(round((base_font_size - 2) * scale)))
        pad = max(8, int(round(12 * scale)))
        accent_width = max(3, int(round(4 * scale)))

        panel_color = str(self.config.get("panel_color", "#10151d"))
        border_color = str(self.config.get("border_color", "#344052"))
        primary_color = str(self.config.get("text_color", "#f7f9fc"))
        secondary_color = str(self.config.get("secondary_text_color", "#aab4c2"))
        accent_color = str(self.config.get("accent_color", "#69d2e7"))
        log_color = str(self.config.get("log_text_color", "#d7dee8"))

        self.canvas.create_rectangle(0, 0, width - 1, height - 1, fill=panel_color, outline=border_color, width=1)
        self.canvas.create_rectangle(0, 0, accent_width, height, fill=accent_color, outline=accent_color)

        header_y = pad
        self.canvas.create_text(
            pad,
            header_y,
            anchor="nw",
            text=str(self.config.get("title", "BATTERFARM")),
            fill=accent_color,
            font=(font_family, title_size, "bold"),
        )
        state_text, state_color = self._state_display()
        state_id = self.canvas.create_text(
            width - pad,
            header_y,
            anchor="ne",
            text=state_text,
            fill=state_color,
            font=(font_family, title_size, "bold"),
        )
        state_bbox = self.canvas.bbox(state_id)
        if state_bbox is not None:
            dot_radius = max(3, int(round(4 * scale)))
            dot_x = state_bbox[0] - dot_radius - max(4, int(round(5 * scale)))
            dot_y = header_y + max(dot_radius, title_size // 2)
            self.canvas.create_oval(
                dot_x - dot_radius,
                dot_y - dot_radius,
                dot_x + dot_radius,
                dot_y + dot_radius,
                fill=state_color,
                outline=state_color,
            )

        header_bottom = max(30, int(round(34 * scale)))
        self.canvas.create_line(pad, header_bottom, width - pad, header_bottom, fill=border_color, width=1)

        show_items = {str(item) for item in self.config.get("show_items", [])}
        step_label_y = header_bottom + max(5, int(round(7 * scale)))
        self.canvas.create_text(
            pad,
            step_label_y,
            anchor="nw",
            text="当前步骤",
            fill=secondary_color,
            font=(font_family, title_size),
        )
        step_value = self.state.get("current_step", "-") if "current_step" in show_items else "-"
        step_y = step_label_y + max(15, int(round(17 * scale)))
        self.canvas.create_text(
            pad,
            step_y,
            anchor="nw",
            text=_fit_overlay_text(step_value, width - pad * 2, step_size),
            fill=primary_color,
            font=(font_family, step_size, "bold"),
        )

        metrics: list[tuple[str, str]] = []
        if "last_action" in show_items:
            metrics.append(("动作", self.state.get("last_action", "-")))
        input_value = self._input_display(show_items)
        if input_value is not None:
            metrics.append(("输入", input_value))
        if "last_marker" in show_items:
            metrics.append(("识别", self.state.get("last_marker", "-")))
        if "last_score" in show_items:
            metrics.append(("置信度", self._score_display()))

        metric_top = max(step_y + int(round(30 * scale)), int(round(88 * scale)))
        metric_gap = max(12, int(round(16 * scale)))
        column_gap = max(10, int(round(16 * scale)))
        column_width = max(1, (width - pad * 2 - column_gap) // 2)
        for index, (label, value) in enumerate(metrics[:4]):
            column = index % 2
            row = index // 2
            metric_x = pad + column * (column_width + column_gap)
            metric_y = metric_top + row * metric_gap
            self._draw_metric(
                metric_x,
                metric_y,
                column_width,
                label,
                value,
                font_family,
                title_size,
                secondary_color,
                primary_color,
            )

        log_keys = {"logs", "recent_logs", "short_logs"}
        if show_items.intersection(log_keys) and self.history:
            divider_y = max(metric_top + metric_gap * 2, int(round(143 * scale)))
            if divider_y < height - log_size - pad:
                self.canvas.create_line(pad, divider_y, width - pad, divider_y, fill=border_color, width=1)
                line_height = max(log_size + 3, int(round(19 * scale)))
                max_log_lines = min(
                    max(1, int(self.config.get("max_log_lines", 3))),
                    max(1, (height - divider_y - pad) // line_height),
                )
                for index, message in enumerate(self.history[-max_log_lines:]):
                    is_latest = index == max_log_lines - 1
                    prefix = ">" if is_latest else "-"
                    self.canvas.create_text(
                        pad,
                        divider_y + max(5, int(round(7 * scale))) + index * line_height,
                        anchor="nw",
                        text=_fit_overlay_text(f"{prefix} {message}", width - pad * 2, log_size),
                        fill=primary_color if is_latest else log_color,
                        font=(font_family, log_size, "bold" if is_latest else "normal"),
                    )

    def _draw_metric(
        self,
        x: int,
        y: int,
        width: int,
        label: str,
        value: str,
        font_family: str,
        font_size: int,
        label_color: str,
        value_color: str,
    ) -> None:
        label_id = self.canvas.create_text(
            x,
            y,
            anchor="nw",
            text=f"{label} ",
            fill=label_color,
            font=(font_family, font_size),
        )
        label_bbox = self.canvas.bbox(label_id)
        value_x = label_bbox[2] if label_bbox is not None else x + font_size * 3
        remaining_width = max(1, width - (value_x - x))
        self.canvas.create_text(
            value_x,
            y,
            anchor="nw",
            text=_fit_overlay_text(value, remaining_width, font_size),
            fill=value_color,
            font=(font_family, font_size, "bold"),
        )

    def _state_display(self) -> tuple[str, str]:
        value = self.state.get("run_state", "idle").strip()
        normalized = value.lower()
        labels = {
            "idle": "待机",
            "running": "运行中",
            "preview": "安全预览",
            "waiting": "等待启动",
            "paused": "已暂停",
            "succeeded": "已完成",
            "success": "已完成",
            "stopped": "已停止",
            "timeout": "已超时",
            "failed": "失败",
            "error": "异常",
        }
        if normalized in {"running", "preview", "succeeded", "success"}:
            color = str(self.config.get("success_color", "#67e8a5"))
        elif normalized in {"waiting", "paused", "stopped", "timeout"}:
            color = str(self.config.get("warning_color", "#ffd166"))
        elif normalized in {"failed", "error"}:
            color = str(self.config.get("error_color", "#ff6b6b"))
        else:
            color = str(self.config.get("idle_color", "#aab4c2"))
        return labels.get(normalized, value), color

    def _input_display(self, show_items: set[str]) -> str | None:
        parts: list[str] = []
        if "active_keys" in show_items:
            keys = self.state.get("active_keys", "-")
            if keys != "-":
                parts.append(f"键 {keys}")
        if "mouse" in show_items:
            mouse = self.state.get("mouse", "-")
            if mouse != "-":
                parts.append(f"鼠 {mouse}")
        if "active_keys" not in show_items and "mouse" not in show_items:
            return None
        return "  ".join(parts) if parts else "-"

    def _score_display(self) -> str:
        raw = self.state.get("last_score", "-")
        try:
            score = float(raw)
        except (TypeError, ValueError):
            return raw
        if 0.0 <= score <= 1.0:
            return f"{score * 100:.1f}%"
        return f"{score:.2f}"

    def _geometry(self) -> tuple[int, int, int, int] | None:
        if self.last_origin is None:
            return None
        left, top, width, height = self._scaled_region()
        origin_x, origin_y = self.last_origin
        return origin_x + left, origin_y + top, width, height

    def _scaled_region(self) -> RegionTuple:
        region = _normalize_region(self.config.get("region", [0, 750, 435, 150]))
        if not bool(self.config.get("scale_with_client", True)) or self.last_client_size is None:
            return region

        base_width, base_height = _normalize_size(self.config.get("base_client_size", [1600, 900]))
        client_width, client_height = self.last_client_size
        scale_x = client_width / max(base_width, 1)
        scale_y = client_height / max(base_height, 1)
        left, top, width, height = region
        return (
            int(round(left * scale_x)),
            int(round(top * scale_y)),
            max(1, int(round(width * scale_x))),
            max(1, int(round(height * scale_y))),
        )


@dataclass
class MatchOverlay:
    config: dict[str, Any]
    parent_enabled: bool
    owner_hwnd: int | None = None
    enabled: bool = field(init=False)
    root: Any = field(default=None, init=False)
    canvas: Any = field(default=None, init=False)
    visible: bool = field(default=False, init=False)
    last_origin: tuple[int, int] | None = field(default=None, init=False)
    last_client_size: tuple[int, int] | None = field(default=None, init=False)
    last_geometry: tuple[int, int, int, int] | None = field(default=None, init=False)
    expires_at: float | None = field(default=None, init=False)
    show_requested: bool = field(default=True, init=False)
    last_pump_at: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        self.enabled = self.parent_enabled and bool(self.config.get("enabled", False))
        self.show_requested = self.enabled and bool(self.config.get("start_visible", True))

    def bind_capture(self, capture: Screenshot) -> None:
        if not self.enabled:
            return
        if capture.region is not None:
            client_size = (int(capture.region[2]), int(capture.region[3]))
        else:
            height, width = capture.image.shape[:2]
            client_size = (int(width), int(height))
        self.bind_client_geometry(capture.origin, client_size)

    def bind_client_geometry(
        self,
        origin: tuple[int, int],
        client_size: tuple[int, int],
    ) -> None:
        if not self.enabled:
            return
        normalized_origin = int(origin[0]), int(origin[1])
        normalized_size = max(1, int(client_size[0])), max(1, int(client_size[1]))
        if normalized_origin == self.last_origin and normalized_size == self.last_client_size:
            return
        self.last_origin = normalized_origin
        self.last_client_size = normalized_size
        if self.visible:
            try:
                self._apply_geometry()
                self.pump(force=True)
            except Exception:
                logging.exception("failed to follow target window with match overlay; disabling match overlay")
                self._disable_after_error()

    def show_matches(self, capture: Screenshot, matches: list[Any], selected: Any | None = None) -> None:
        if not self.enabled or not self.show_requested:
            return
        if not _owner_is_active(self.owner_hwnd):
            self.hide()
            return
        self.bind_capture(capture)
        if not matches:
            self.clear()
            return
        try:
            self._ensure_window()
            geometry = self._apply_geometry()
            if geometry is None:
                return
            x, y, width, height = geometry
            self._draw_matches(matches, selected)
            if not self.visible:
                self.root.deiconify()
                self.root.lift()
                self.root.update_idletasks()
                self.root.update()
                self._apply_owned_position(x, y, width, height)
            duration_ms = int(self.config.get("duration_ms", 1200))
            self.expires_at = time.monotonic() + max(0, duration_ms) / 1000.0 if duration_ms > 0 else None
            self.visible = True
        except Exception:
            logging.exception("failed to show match overlay; disabling match overlay")
            self._disable_after_error()

    def hide(self) -> None:
        if self.root is None:
            self.visible = False
            return
        try:
            self.root.withdraw()
            self.root.update_idletasks()
            self.root.update()
            self.visible = False
        except Exception:
            logging.exception("failed to hide match overlay")
            self._disable_after_error()

    def restore_if_fresh(self) -> bool:
        """Restore a capture-hidden match layer without extending its expiry."""
        if not self.enabled or not self.show_requested or self.root is None or self.canvas is None:
            return False
        now = time.monotonic()
        if self.expires_at is not None and now >= self.expires_at:
            self.clear()
            return False
        if not _owner_is_active(self.owner_hwnd):
            self.hide()
            return False
        try:
            geometry = self._apply_geometry()
            if geometry is None:
                return False
            x, y, width, height = geometry
            if not self.visible:
                self.root.deiconify()
                self.root.lift()
                self.root.update_idletasks()
                self.root.update()
                self._apply_owned_position(x, y, width, height)
            self.visible = True
            return True
        except Exception:
            logging.exception("failed to restore match overlay after capture; disabling match overlay")
            self._disable_after_error()
            return False

    def clear(self) -> None:
        if self.canvas is not None:
            try:
                self.canvas.delete("all")
            except Exception:
                logging.exception("failed to clear match overlay")
        self.expires_at = None
        self.hide()

    def close(self) -> None:
        self._destroy_window("failed to close match overlay")

    def set_show_requested(self, show: bool) -> None:
        self.show_requested = bool(show) and self.enabled
        if not self.show_requested:
            self.clear()

    def pump(self, force: bool = False) -> None:
        if not self.enabled or self.root is None:
            return
        now = time.monotonic()
        interval = max(0.01, float(self.config.get("pump_interval_ms", 50)) / 1000.0)
        if not force and now - self.last_pump_at < interval:
            return
        try:
            self.root.update_idletasks()
            self.root.update()
            self.last_pump_at = now
        except Exception:
            logging.exception("failed to pump match overlay events; disabling match overlay")
            self._disable_after_error()
            return
        self._hide_if_expired(now)

    def _disable_after_error(self) -> None:
        self.enabled = False
        self.show_requested = False
        self.expires_at = None
        self._destroy_window("failed to destroy disabled match overlay")

    def _destroy_window(self, error_message: str) -> None:
        root = self.root
        self.visible = False
        self.expires_at = None
        if root is None:
            self.canvas = None
            return
        try:
            root.withdraw()
        except Exception:
            logging.warning("failed to withdraw match overlay during cleanup", exc_info=True)
        try:
            root.destroy()
        except Exception:
            logging.warning(error_message, exc_info=True)
            return
        if self.root is root:
            self.root = None
            self.canvas = None

    def _ensure_window(self) -> None:
        if self.root is not None:
            return

        import tkinter as tk

        transparent_color = str(self.config.get("transparent_color", "#010203"))
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", False)
        self.root.configure(bg=transparent_color)
        self.canvas = tk.Canvas(
            self.root,
            bg=transparent_color,
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(fill="both", expand=True)
        self._apply_window_styles()
        if sys.platform.startswith("win"):
            self.root.attributes("-transparentcolor", transparent_color)
        else:
            self.root.attributes("-alpha", float(self.config.get("opacity", 0.85)))

    def _apply_window_styles(self) -> None:
        _apply_native_overlay_styles(
            self.root,
            click_through=bool(self.config.get("click_through", True)),
            owner_hwnd=self.owner_hwnd,
        )

    def _apply_owned_position(self, x: int, y: int, width: int, height: int) -> None:
        if self.root is None:
            return
        _position_overlay_window(self.root, x, y, width, height)

    def _draw_matches(self, matches: list[Any], selected: Any | None) -> None:
        self.canvas.delete("all")
        show_mode = str(self.config.get("show_mode", "best")).strip().lower()
        max_boxes = max(1, int(self.config.get("max_boxes", 6)))
        ordered = sorted(matches, key=lambda item: (-int(item.spec.priority), -float(item.score), str(item.name)))
        if show_mode != "all":
            ordered = [selected if selected is not None else ordered[0]]
        else:
            ordered = ordered[:max_boxes]

        color = str(self.config.get("color", "#00ff66"))
        selected_color = str(self.config.get("selected_color", "#ffcc00"))
        outline_color = str(self.config.get("outline_color", "#05070a"))
        label_background = str(self.config.get("label_background", "#111827"))
        label_text_color = str(self.config.get("label_text_color", "#f8fafc"))
        line_width = max(1, int(self.config.get("line_width", 3)))
        shadow_width = max(0, int(self.config.get("shadow_width", 2)))
        draw_label = bool(self.config.get("label", True))
        font_family = str(self.config.get("font_family", "Microsoft YaHei UI"))
        font_size = max(9, int(self.config.get("font_size", 13)))
        max_label_chars = max(12, int(self.config.get("max_label_chars", 38)))
        client_width, client_height = self.last_client_size or (1, 1)
        for match in ordered:
            x1, y1 = match.top_left
            x2, y2 = match.bottom_right
            is_selected = selected is not None and match is selected
            box_color = selected_color if is_selected else color
            if shadow_width > 0:
                self.canvas.create_rectangle(
                    int(x1),
                    int(y1),
                    int(x2),
                    int(y2),
                    outline=outline_color,
                    width=line_width + shadow_width * 2,
                )
            self.canvas.create_rectangle(
                int(x1),
                int(y1),
                int(x2),
                int(y2),
                outline=box_color,
                width=line_width,
            )
            if draw_label:
                padding_x = max(4, int(self.config.get("label_padding_x", 7)))
                padding_y = max(2, int(self.config.get("label_padding_y", 4)))
                score_text = f"{float(match.score) * 100:.1f}%"
                max_total_units = max(
                    8,
                    int(max(1, client_width - padding_x * 2 - 2) / max(1.0, font_size * 0.62)),
                )
                # Reserve three units because truncated names append an ellipsis.
                name_units = max(4, max_total_units - len(score_text) - 5)
                label_name = _truncate_display(_display_name(match.name), min(max_label_chars, name_units))
                label = f"{label_name}  {score_text}"
                text_id = self.canvas.create_text(
                    int(x1) + padding_x,
                    int(y1) + padding_y,
                    anchor="nw",
                    text=label,
                    fill=label_text_color,
                    font=(font_family, font_size, "bold"),
                )
                text_bbox = self.canvas.bbox(text_id)
                if text_bbox is None:
                    continue
                label_width = text_bbox[2] - text_bbox[0] + padding_x * 2
                label_height = text_bbox[3] - text_bbox[1] + padding_y * 2
                label_x = max(1, min(int(x1), max(1, client_width - label_width - 1)))
                label_y = int(y1) - label_height - 3
                if label_y < 1:
                    label_y = int(y1) + 3
                if label_y + label_height >= client_height:
                    label_y = max(1, client_height - label_height - 1)
                self.canvas.coords(text_id, label_x + padding_x, label_y + padding_y)
                background_id = self.canvas.create_rectangle(
                    label_x,
                    label_y,
                    label_x + label_width,
                    label_y + label_height,
                    fill=label_background,
                    outline=box_color,
                    width=1,
                )
                accent_width = max(3, line_width)
                accent_id = self.canvas.create_rectangle(
                    label_x,
                    label_y,
                    label_x + accent_width,
                    label_y + label_height,
                    fill=box_color,
                    outline=box_color,
                    width=0,
                )
                self.canvas.tag_lower(background_id, text_id)
                self.canvas.tag_lower(accent_id, text_id)

    def _hide_if_expired(self, now: float | None = None) -> None:
        current = time.monotonic() if now is None else float(now)
        if self.expires_at is not None and current >= self.expires_at:
            self.clear()

    def _apply_geometry(self) -> tuple[int, int, int, int] | None:
        geometry = self._geometry()
        if geometry is None or self.root is None or self.canvas is None:
            return geometry
        if geometry == self.last_geometry:
            return geometry

        x, y, width, height = geometry
        previous_geometry = self.last_geometry
        previous_size = previous_geometry[2:] if previous_geometry is not None else None
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        if previous_size != (width, height):
            self.canvas.configure(width=width, height=height)
        self._apply_owned_position(x, y, width, height)
        log_geometry = (
            logging.info
            if previous_geometry is None or previous_size != (width, height)
            else logging.debug
        )
        log_geometry("match overlay geometry screen=(%s,%s,%s,%s)", x, y, width, height)
        self.last_geometry = geometry
        return geometry

    def _geometry(self) -> tuple[int, int, int, int] | None:
        if self.last_origin is None or self.last_client_size is None:
            return None
        origin_x, origin_y = self.last_origin
        width, height = self.last_client_size
        return origin_x, origin_y, max(1, width), max(1, height)


@dataclass
class OverlayController:
    """Own both overlay windows and expose one lifecycle to the task layer."""

    status: StatusOverlay
    matches: MatchOverlay
    owner_hwnd: int | None = None
    _owner_active: bool = field(default=True, init=False)
    _restore_matches_after_capture: bool = field(default=False, init=False)

    @property
    def enabled(self) -> bool:
        return self.status.enabled or self.matches.enabled

    @property
    def hide_before_capture(self) -> bool:
        return self.status.hide_before_capture

    @property
    def show_requested(self) -> bool:
        return self.status.show_requested

    def update_status(self, **items: Any) -> None:
        self.status.update_status(**items)

    def add_log(self, message: str) -> None:
        self.status.add_log(message)

    def bind_capture(self, capture: Screenshot) -> None:
        self.status.bind_capture(capture)
        self.matches.bind_capture(capture)

    def bind_client_geometry(
        self,
        origin: tuple[int, int],
        client_size: tuple[int, int],
    ) -> None:
        self.status.bind_client_geometry(origin, client_size)
        self.matches.bind_client_geometry(origin, client_size)

    def prepare_capture(self) -> bool:
        restore_status = self.status.enabled and self.status.hide_before_capture and self.status.show_requested
        if restore_status:
            self.status.hide(temporary=True)
        self._restore_matches_after_capture = self.matches.visible
        self.matches.hide()
        return restore_status

    def finish_capture(self, capture: Screenshot | None, restore_status: bool) -> None:
        if capture is not None:
            self.bind_capture(capture)
        should_show_status = restore_status or (capture is not None and not self.status.visible)
        if should_show_status and self._refresh_owner_visibility() and self.status.enabled and self.status.show_requested:
            self.status.show()
        if self._restore_matches_after_capture:
            self.matches.restore_if_fresh()
        self._restore_matches_after_capture = False

    def show_matches(self, capture: Screenshot, matches: list[Any], selected: Any | None = None) -> None:
        if self._refresh_owner_visibility():
            self.matches.show_matches(capture, matches, selected=selected)

    def clear_matches(self) -> None:
        self.matches.clear()

    def toggle(self) -> bool:
        return self.set_visible(not self.status.show_requested)

    def set_visible(self, show: bool) -> bool:
        requested = self.status.set_show_requested(show)
        self.matches.set_show_requested(requested)
        if requested:
            self._refresh_owner_visibility()
        return requested

    def pump(self, force: bool = False) -> None:
        if not self._refresh_owner_visibility():
            return
        self.status.pump(force=force)
        self.matches.pump(force=force)

    def _refresh_owner_visibility(self) -> bool:
        active = _owner_is_active(self.owner_hwnd)
        if not active:
            if self._owner_active:
                self.status.hide(temporary=True)
                self.matches.hide()
            self._owner_active = False
            return False
        if not self._owner_active:
            self._owner_active = True
            if self.status.show_requested and self.status.last_origin is not None:
                self.status.show()
        return True

    def close(self) -> None:
        self.matches.close()
        self.status.close()


def create_status_overlay(config: dict[str, Any], owner_hwnd: int | None = None) -> StatusOverlay:
    overlay_config = dict(config.get("overlay", {}))
    if not bool(overlay_config.get("bind_to_window", True)):
        owner_hwnd = None
    return StatusOverlay(overlay_config, owner_hwnd=owner_hwnd)


def create_match_overlay(config: dict[str, Any], owner_hwnd: int | None = None) -> MatchOverlay:
    overlay_config = dict(config.get("overlay", {}))
    if not bool(overlay_config.get("bind_to_window", True)):
        owner_hwnd = None
    match_config = dict(overlay_config.get("match_boxes", {}))
    match_config.setdefault("start_visible", bool(overlay_config.get("start_visible", True)))
    return MatchOverlay(
        match_config,
        parent_enabled=bool(overlay_config.get("enabled", False)),
        owner_hwnd=owner_hwnd,
    )


def create_overlay_controller(config: dict[str, Any], owner_hwnd: int | None = None) -> OverlayController:
    overlay_config = dict(config.get("overlay", {}))
    if not bool(overlay_config.get("bind_to_window", True)):
        owner_hwnd = None
    return OverlayController(
        status=create_status_overlay(config, owner_hwnd=owner_hwnd),
        matches=create_match_overlay(config, owner_hwnd=owner_hwnd),
        owner_hwnd=owner_hwnd,
    )


def _normalize_region(value: Any) -> RegionTuple:
    values = tuple(int(item) for item in value)
    if len(values) != 4:
        raise ValueError("overlay.region must be [left, top, width, height].")
    left, top, width, height = values
    if left < 0 or top < 0 or width <= 0 or height <= 0:
        raise ValueError("overlay.region must use non-negative left/top and positive width/height.")
    return left, top, width, height


def _normalize_size(value: Any) -> tuple[int, int]:
    values = tuple(int(item) for item in value)
    if len(values) != 2:
        raise ValueError("overlay.base_client_size must be [width, height].")
    return max(1, values[0]), max(1, values[1])


def _truncate_display(value: Any, max_units: int) -> str:
    text = " ".join(str(value).split())
    if max_units <= 0:
        return ""
    units = 0
    result: list[str] = []
    for char in text:
        char_units = 1 if ord(char) < 128 else 2
        if units + char_units > max_units:
            return "".join(result).rstrip() + "..."
        result.append(char)
        units += char_units
    return "".join(result)


def _fit_overlay_text(value: Any, pixel_width: int, font_size: int) -> str:
    average_unit_width = max(1.0, font_size * 0.58)
    max_units = max(4, int(pixel_width / average_unit_width))
    return _truncate_display(value, max_units)


def _display_name(value: Any) -> str:
    text = str(value).strip()
    if not text or text == "-":
        return text or "-"
    direct = _DISPLAY_NAMES.get(text.lower())
    if direct is not None:
        return direct

    scan_match = re.fullmatch(r"marker_sequence_scan_(\d+)", text, flags=re.IGNORECASE)
    if scan_match:
        return f"第 {int(scan_match.group(1))} 轮标识扫描"

    scan_match = re.fullmatch(r"(.+)_scan_(\d+)", text, flags=re.IGNORECASE)
    if scan_match:
        return f"{_display_name(scan_match.group(1))}·第 {int(scan_match.group(2))} 次扫描"

    for suffix, label in (
        ("_after_action", "动作后检查"),
        ("_dialog_check", "弹窗检查"),
        ("_precheck", "执行前检查"),
    ):
        if text.lower().endswith(suffix):
            return f"{_display_name(text[:-len(suffix)])}·{label}"
    lowered = text.lower()
    for name in sorted(_DISPLAY_NAMES, key=len, reverse=True):
        prefix = f"{name}_"
        if lowered.startswith(prefix):
            return f"{_DISPLAY_NAMES[name]}·{_display_name(text[len(prefix):])}"
    return text


def _localize_overlay_value(key: str, value: Any) -> str:
    text = str(value)
    if key in {"current_step", "last_marker"}:
        return _display_name(text)
    if key == "last_action":
        return _localize_action(text)
    if key == "mouse":
        return _localize_mouse(text)
    return text


def _localize_action(value: Any) -> str:
    text = str(value).strip()
    if not text or text == "-":
        return text or "-"
    patterns = (
        (r"click (.+)", "点击 {0}"),
        (r"clicked (.+)", "已点击 {0}"),
        (r"press (.+)", "按下 {0}"),
        (r"release (.+)", "释放 {0}"),
        (r"down (.+)", "按下 {0}"),
        (r"up (.+)", "释放 {0}"),
        (r"mouse (left|right|middle) down", "按下鼠标{0}"),
        (r"mouse (left|right|middle) up", "释放鼠标{0}"),
    )
    if text.lower() == "mouse move":
        return "移动鼠标"
    for pattern, template in patterns:
        match = re.fullmatch(pattern, text, flags=re.IGNORECASE)
        if match:
            return template.format(_display_name(match.group(1)))
    return _display_name(text)


def _localize_mouse(value: Any) -> str:
    text = str(value).strip()
    if not text or text == "-":
        return text or "-"
    head, separator, tail = text.partition("@")
    buttons = "+".join(_display_name(item) for item in head.split("+"))
    return f"{buttons}{separator}{tail}" if separator else buttons


def _localize_overlay_message(value: Any) -> str:
    text = " ".join(str(value).split())
    if not text:
        return text

    exact = {
        "ready; F9 scan / ESC stop": "准备就绪；F9 开始扫描 / ESC 停止",
        "game focused; F9 scan / ESC stop": "游戏窗口已激活；F9 开始扫描 / ESC 停止",
        "F9 scan requested": "已收到 F9 扫描指令",
        "no marker; F9 rescan / ESC stop": "未识别到任务标识；F9 重新扫描 / ESC 停止",
        "interrupted; F9 rescan / ESC stop": "任务已中断；F9 重新扫描 / ESC 停止",
        "dialog recovered; restart scan": "弹窗已处理，重新扫描任务标识",
        "F10 interrupt": "已收到 F10 中断指令",
        "marker sequence completed": "标识任务已完成",
        "marker sequence stopped": "标识任务已停止",
        "marker sequence exited by user": "用户已结束标识任务",
        "marker sequence exited before first scan": "首次扫描前已退出",
        "marker sequence stopped before first scan": "首次扫描前任务已停止",
        "succeeded": "任务已完成",
        "stopped": "任务已停止",
        "timeout": "任务已超时",
        "failed": "任务失败",
    }
    if text in exact:
        return exact[text]

    match = re.fullmatch(r"start (.+)", text, flags=re.IGNORECASE)
    if match:
        return f"开始运行：{_display_name(match.group(1))}"
    match = re.fullmatch(r"recognized (.+), but excluded; F9 rescan / ESC stop", text, flags=re.IGNORECASE)
    if match:
        return f"已识别 {_display_name(match.group(1))}，但不符合执行条件；F9 重新扫描 / ESC 停止"
    match = re.fullmatch(r"reset (.+)", text, flags=re.IGNORECASE)
    if match:
        return f"按 {match.group(1).upper()} 复位"
    match = re.fullmatch(r"click (.+) ([01](?:\.\d+)?)", text, flags=re.IGNORECASE)
    if match:
        return f"点击 {_display_name(match.group(1))}（{float(match.group(2)) * 100:.1f}%）"

    prefix, separator, detail = text.partition(":")
    if separator:
        localized_prefix = _display_name(prefix)
        detail = detail.strip()
        detail_exact = {
            "skipped": "已跳过",
            "interrupted": "已中断",
            "dialog confirmed": "已确认弹窗",
            "no match": "未识别到目标",
            "wait": "等待识别",
            "mouse move": "移动鼠标",
        }
        if detail in detail_exact:
            return f"{localized_prefix}：{detail_exact[detail]}"
        if any("\u4e00" <= char <= "\u9fff" for char in detail):
            return f"{localized_prefix}：{detail}"
        mouse_match = re.fullmatch(r"mouse (left|right|middle) (down|up)", detail, flags=re.IGNORECASE)
        if mouse_match:
            action = "按下" if mouse_match.group(2).lower() == "down" else "释放"
            return f"{localized_prefix}：{action}鼠标{_display_name(mouse_match.group(1))}"
        key_match = re.fullmatch(r"(.+) (down|up)", detail, flags=re.IGNORECASE)
        if key_match:
            action = "按下" if key_match.group(2).lower() == "down" else "释放"
            return f"{localized_prefix}：{action} {key_match.group(1)}"
        detection_match = re.fullmatch(r"(.+) ([01](?:\.\d+)?)", detail)
        if detection_match:
            return (
                f"{localized_prefix}：识别到 {_display_name(detection_match.group(1))}"
                f"（{float(detection_match.group(2)) * 100:.1f}%）"
            )
    if any("\u4e00" <= char <= "\u9fff" for char in text):
        return text
    return _display_name(text)
