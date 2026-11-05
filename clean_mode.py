#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清洁模式 —— 把屏幕和键盘暂时锁住，安心擦一擦。

用法:
    python3 clean_mode.py            # 打开菜单，选时长后开始
    python3 clean_mode.py 90         # 直接开始 90 秒
    python3 clean_mode.py --white    # 白色背景，更容易看清指纹
"""

from __future__ import annotations

import argparse
import ctypes
import shutil
import subprocess
import sys
import threading
import time
import traceback
import tkinter as tk

FONT = "PingFang SC"

MENU_BG = "#0b0b0c"
INK = "#f5f5f7"
MUTED = "#7d7d84"
FAINT = "#4b4b52"
ACCENT = "#3ddc84"

CHIP_BG = "#17181a"
CHIP_BG_ON = "#dff8ea"
CHIP_FG = "#9a9aa2"
CHIP_FG_ON = "#0b2a19"

THEMES = {
    "dark": dict(
        bg="#000000", track="#151515", ring="#2e8a62",
        time="#6f6f6f", hint="#9c9c9c", faint="#4a4a4a",
        ok="#3ddc84",
    ),
    "light": dict(
        bg="#f8f8f7", track="#e7e7e4", ring="#8ed7bb",
        time="#a3a3a0", hint="#8a8a86", faint="#c6c6c2",
        ok="#0f9d63",
    ),
}

# (秒数, 按钮文字)
OPTIONS = ((30, "30 秒"), (60, "1 分钟"), (90, "1 分半"), (180, "3 分钟"))


def font(size: int, weight: str = "normal"):
    return (FONT, size, weight)


def pretty(seconds: float) -> str:
    s = int(round(seconds))
    if s < 60:
        return f"{s} 秒"
    return f"{s // 60} 分 {s % 60:02d} 秒"


def clock(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    return f"{s // 60}:{s % 60:02d}"


def rounded_rect(cv: tk.Canvas, x1, y1, x2, y2, r, fill):
    """在 Canvas 上画一个圆角矩形（纯填充）。"""
    cv.create_rectangle(x1 + r, y1, x2 - r, y2, fill=fill, outline="")
    cv.create_rectangle(x1, y1 + r, x2, y2 - r, fill=fill, outline="")
    for cx, cy in ((x1, y1), (x2 - 2 * r, y1), (x1, y2 - 2 * r), (x2 - 2 * r, y2 - 2 * r)):
        cv.create_oval(cx, cy, cx + 2 * r, cy + 2 * r, fill=fill, outline="")


# --------------------------------------------------------------- 系统级按键拦截
_TAP_CB = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                           ctypes.c_void_p, ctypes.c_void_p)

_CG_PATH = "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
_CF_PATH = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
_AS_PATH = "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"

_UTF8 = 0x08000100
_KEY_DOWN, _KEY_UP, _FLAGS_CHANGED = 10, 11, 12
_TAP_DISABLED = (-2 & 0xFFFFFFFF, -1 & 0xFFFFFFFF)
_KCG_KEYCODE_FIELD = 9
_ESC_KEYCODE = 53


def accessibility_trusted() -> bool:
    """当前进程有没有「辅助功能」权限。"""
    try:
        lib = ctypes.CDLL(_AS_PATH)
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(lib.AXIsProcessTrusted())
    except OSError:
        return False


def request_accessibility() -> None:
    """尝试让本程序出现在「辅助功能」列表里。

    注意：不要用 AXIsProcessTrustedWithOptions —— 在 macOS 26 上那个 API
    会直接 SIGSEGV（已在实机复现），这里改用一次 AX 访问来登记。
    """
    try:
        lib = ctypes.CDLL(_AS_PATH)
        cf = ctypes.CDLL(_CF_PATH)
        lib.AXUIElementCreateSystemWide.restype = ctypes.c_void_p
        lib.AXUIElementCreateSystemWide.argtypes = []
        lib.AXUIElementCopyAttributeValue.restype = ctypes.c_int32
        lib.AXUIElementCopyAttributeValue.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        attr = cf.CFStringCreateWithCString(None, b"AXFocusedApplication", _UTF8)
        element = lib.AXUIElementCreateSystemWide()
        if element and attr:
            value = ctypes.c_void_p()
            lib.AXUIElementCopyAttributeValue(element, attr, ctypes.byref(value))
    except Exception:
        pass


def open_accessibility_settings() -> None:
    try:
        subprocess.Popen(
            ["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


class KeyShield:
    """在系统层吞掉所有按键（Esc 除外），需要「辅助功能」权限。

    拿不到权限时 start() 返回 False，程序照常运行，只是组合键拦不住。
    """

    def __init__(self):
        self.active = False
        self.heartbeat = 0.0
        self._cg = None
        self._cf = None
        self._tap = None
        self._loop = None
        self._thread = None
        self._ready = threading.Event()
        self._callback = None

    def start(self) -> bool:
        if self.active:
            return True
        if sys.platform != "darwin":
            return False
        try:
            cg = ctypes.CDLL(_CG_PATH)
            cf = ctypes.CDLL(_CF_PATH)
            cg.CGEventTapCreate.restype = ctypes.c_void_p
            cg.CGEventTapCreate.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                            ctypes.c_uint64, _TAP_CB, ctypes.c_void_p]
            cg.CGEventTapEnable.argtypes = [ctypes.c_void_p, ctypes.c_bool]
            cg.CGEventGetIntegerValueField.restype = ctypes.c_int64
            cg.CGEventGetIntegerValueField.argtypes = [ctypes.c_void_p, ctypes.c_int]
            cf.CFMachPortCreateRunLoopSource.restype = ctypes.c_void_p
            cf.CFMachPortCreateRunLoopSource.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
            cf.CFRunLoopGetCurrent.restype = ctypes.c_void_p
            cf.CFRunLoopGetCurrent.argtypes = []
            cf.CFRunLoopAddSource.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
            cf.CFRunLoopStop.argtypes = [ctypes.c_void_p]
            cf.CFRunLoopRun.argtypes = []
            cf.CFStringCreateWithCString.restype = ctypes.c_void_p
            cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        except OSError:
            return False

        self._cg, self._cf = cg, cf
        self._callback = _TAP_CB(self._on_event)
        self.heartbeat = time.monotonic()
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(2.0)
        return self.active

    def _run(self) -> None:
        try:
            mask = (1 << _KEY_DOWN) | (1 << _KEY_UP) | (1 << _FLAGS_CHANGED)
            tap = self._cg.CGEventTapCreate(0, 0, 0, mask, self._callback, None)
        except Exception:
            tap = None
        if not tap:
            self._ready.set()
            return
        try:
            self._tap = tap
            source = self._cf.CFMachPortCreateRunLoopSource(None, tap, 0)
            loop = self._cf.CFRunLoopGetCurrent()
            self._loop = loop
            mode = self._cf.CFStringCreateWithCString(None, b"kCFRunLoopDefaultMode", _UTF8)
            self._cf.CFRunLoopAddSource(loop, source, mode)
            self._cg.CGEventTapEnable(tap, True)
            self.active = True
            self.heartbeat = time.monotonic()
            self._ready.set()
            self._cf.CFRunLoopRun()
        except Exception:
            pass
        finally:
            self.active = False
            self._ready.set()

    def _on_event(self, proxy, etype, event, info):
        try:
            if etype in _TAP_DISABLED:           # 系统临时禁用，重新打开
                if self._tap:
                    self._cg.CGEventTapEnable(self._tap, True)
                return event
            if etype not in (_KEY_DOWN, _KEY_UP):
                return event
            if not self.active:
                return event
            if time.monotonic() - self.heartbeat > 2.5:
                return event                     # 界面疑似卡住，放行保命
            if self._cg.CGEventGetIntegerValueField(event, _KCG_KEYCODE_FIELD) == _ESC_KEYCODE:
                return event                     # Esc 留给界面做「连按两下退出」
            return None                          # 其余全部吞掉
        except Exception:
            return event

    def stop(self) -> None:
        self.active = False
        try:
            if self._tap:
                self._cg.CGEventTapEnable(self._tap, False)
        except Exception:
            pass
        try:
            if self._loop:
                self._cf.CFRunLoopStop(self._loop)
        except Exception:
            pass


class CleanApp:
    """菜单 → 3 秒倒计时 → 全屏清洁 → 完成。"""

    def __init__(self, root: tk.Tk, duration: int | None = None,
                 theme: str = "dark", demo: bool = False):
        self.root = root
        self.demo = demo
        self.theme = theme if theme in THEMES else "dark"
        self.preset = duration or 60
        self.duration = float(self.preset)   # 秒
        self.state = "menu"
        self.created_at = time.monotonic()
        self.gen = 0
        self.count_left = 3
        self.start_at = 0.0
        self.elapsed = 0.0
        self.close_at = 0.0
        self.esc_armed_at: float | None = None
        self.alive = True
        self.canvas: tk.Canvas | None = None
        self.menu_frame: tk.Frame | None = None
        self.chip_labels: dict[int, tk.Label] = {}
        self.theme_labels: dict[str, tk.Label] = {}
        self.start_canvas: tk.Canvas | None = None
        self._caffeinate: subprocess.Popen | None = None
        self._win_awake = False
        self.shield = KeyShield()
        self.perm_label: tk.Label | None = None
        self.menu_gen = 0
        self.shield_prompted = False
        self.shield_note = ""

        root.title("清洁模式")
        root.configure(bg=MENU_BG)
        root.attributes("-fullscreen", True)
        root.protocol("WM_DELETE_WINDOW", self.quit)

        root.bind_all("<KeyPress>", self.on_key)
        root.bind_all("<KeyRelease>", self.on_key_release)
        for seq in ("<Command-q>", "<Command-w>", "<Command-m>",
                    "<Command-h>", "<Command-Tab>", "<Command-`>"):
            root.bind_all(seq, self.on_shortcut)

        self.build_menu()
        self._raise_window()

    def _raise_window(self) -> None:
        """把窗口提到最前。"""
        if self.state != "menu":
            return
        self.root.lift()
        self.root.focus_force()

    # ------------------------------------------------------------------ 菜单
    def build_menu(self) -> None:
        self._hide_stage()
        m = tk.Frame(self.root, bg=MENU_BG)
        m.place(relx=0.5, rely=0.5, anchor="center")
        self.menu_frame = m

        tk.Label(m, text="清洁模式", font=font(30, "bold"),
                 bg=MENU_BG, fg=INK).pack()
        tk.Label(m, text="接下来的几分钟，屏幕和键盘只属于抹布",
                 font=font(13), bg=MENU_BG, fg=MUTED).pack(pady=(8, 0))

        # 时长
        row = tk.Frame(m, bg=MENU_BG)
        row.pack(pady=(30, 0))
        self.chip_labels = {}
        for sec, text in OPTIONS:
            lb = tk.Label(row, text=text, font=font(13), padx=16, pady=8,
                          bd=0, highlightthickness=0, cursor="hand2")
            lb.pack(side="left", padx=5)
            lb.bind("<Button-1>", lambda _e, s=sec: self.pick_duration(s))
            self.chip_labels[sec] = lb

        # 背景色
        trow = tk.Frame(m, bg=MENU_BG)
        trow.pack(pady=(14, 0))
        tk.Label(trow, text="背景", font=font(11), bg=MENU_BG,
                 fg=FAINT).pack(side="left", padx=(0, 8))
        self.theme_labels = {}
        for key, text in (("dark", "纯黑 · 看灰尘"), ("light", "纯白 · 看指纹")):
            lb = tk.Label(trow, text=text, font=font(11), padx=13, pady=6,
                          bd=0, highlightthickness=0, cursor="hand2")
            lb.pack(side="left", padx=4)
            lb.bind("<Button-1>", lambda _e, k=key: self.pick_theme(k))
            self.theme_labels[key] = lb

        self.pick_duration(self.preset)
        self.pick_theme(self.theme)

        # 开始按钮
        sw, sh, r = 264, 62, 31
        self.start_canvas = tk.Canvas(m, width=sw, height=sh, bg=MENU_BG,
                                      highlightthickness=0, bd=0)
        self.start_canvas.pack(pady=(34, 0))
        self.start_canvas.bind("<Enter>", lambda _e: self._paint_start(True))
        self.start_canvas.bind("<Leave>", lambda _e: self._paint_start(False))
        self.start_canvas.bind("<Button-1>", lambda _e: self.begin())
        self._paint_start(False)

        # 小贴士
        tips = tk.Frame(m, bg=MENU_BG)
        tips.pack(pady=(40, 0))
        for line in ("清洁剂先喷在纤维布上，别直接喷屏幕",
                     "键盘先关机，再用软毛刷或气吹扫缝隙",
                     "期间不会息屏、不会休眠、不会误触"):
            tk.Label(tips, text="· " + line, font=font(11), bg=MENU_BG,
                     fg="#5f5f66").pack(anchor="w", pady=2)

        self.perm_label = tk.Label(m, text="", font=font(11), bg=MENU_BG,
                                   fg="#5f5f66", cursor="hand2")
        self.perm_label.pack(pady=(22, 0))
        self.perm_label.bind("<Button-1>", lambda _e: self.on_perm_click())
        self.menu_gen += 1
        self._refresh_perm_label(self.menu_gen)

        tk.Label(m, text="Esc 退出   ·   开始后倒计时 3 秒   ·   连按两下 Esc 可中断",
                 font=font(10), bg=MENU_BG, fg="#3c3c42").pack(pady=(20, 0))

    def _paint_start(self, hover: bool) -> None:
        cv = self.start_canvas
        if cv is None:
            return
        cv.delete("all")
        fill = "#4ee896" if hover else ACCENT
        rounded_rect(cv, 0, 0, 264, 62, 31, fill)
        cv.create_text(132, 31, text="开始清洁", font=font(18, "bold"),
                       fill="#06210f")

    def pick_duration(self, sec: int) -> None:
        if self.state != "menu":
            return
        self.preset = sec
        self.duration = float(sec)
        for sec_, lb in self.chip_labels.items():
            on = sec_ == sec
            lb.configure(bg=CHIP_BG_ON if on else CHIP_BG,
                         fg=CHIP_FG_ON if on else CHIP_FG)

    def pick_theme(self, key: str) -> None:
        if self.state != "menu":
            return
        self.theme = key
        for k, lb in self.theme_labels.items():
            on = k == key
            lb.configure(bg=CHIP_BG_ON if on else CHIP_BG,
                         fg=CHIP_FG_ON if on else CHIP_FG)

    # ------------------------------------------------------------------ 流程
    def begin(self) -> None:
        if self.state != "menu":
            return
        # 窗口刚创建时 macOS 还在做全屏动画，这时切画面会把全屏打断
        wait = 1.2 - (time.monotonic() - self.created_at)
        if wait > 0:
            self.root.after(int(wait * 1000) + 10, self.begin)
            return
        self._start_shield()
        self.state = "count"
        self.gen += 1
        g = self.gen
        self.count_left = 3
        self._show_stage()
        self._start_keep_awake()
        self._safe(self.draw_count, 3)
        self.root.after(1000, lambda: self._count_tick(g))

    def _count_tick(self, g: int) -> None:
        if not self.alive or self.state != "count" or g != self.gen:
            return
        self.shield.heartbeat = time.monotonic()
        self.count_left -= 1
        if self.count_left <= 0:
            self.start_cleaning()
        else:
            self._safe(self.draw_count, self.count_left)
            self.root.after(1000, lambda: self._count_tick(g))

    def start_cleaning(self) -> None:
        if not self.alive:
            return
        self.state = "clean"
        self.gen += 1
        g = self.gen
        self.start_at = time.monotonic()
        self.esc_armed_at = None
        try:
            self.root.bell()
        except tk.TclError:
            pass
        self._tick(g)

    def _tick(self, g: int) -> None:
        if not self.alive or self.state != "clean" or g != self.gen:
            return
        now = time.monotonic()
        self.shield.heartbeat = now
        remaining = self.duration - (now - self.start_at)
        if remaining <= 0:
            self.finish(self.duration)
            return
        self._safe(self.draw_clean, remaining, self._esc_left(now))
        self.root.after(40, lambda: self._tick(g))

    def _esc_left(self, now: float):
        """第一次按 Esc 之后，还有多久可以再按一下确认退出。"""
        if self.esc_armed_at is None:
            return None
        left = 1.5 - (now - self.esc_armed_at)
        if left <= 0:
            self.esc_armed_at = None
            return None
        return left

    def _safe(self, fn, *args) -> None:
        """绘制出问题也不要让计时循环停掉（停掉就是整屏卡死）。"""
        try:
            fn(*args)
        except Exception:
            traceback.print_exc()

    # ------------------------------------------------------------ 组合键拦截
    def _start_shield(self) -> None:
        if not accessibility_trusted():
            if not self.shield_prompted:
                self.shield_prompted = True
                request_accessibility()
            return
        if not self.shield.start():
            self.shield_note = "已授权，但需要重启一次程序才生效"

    def _refresh_perm_label(self, g: int) -> None:
        if g != self.menu_gen or self.state != "menu" or self.perm_label is None:
            return
        if accessibility_trusted():
            self.perm_label.configure(
                text="● 组合键拦截已开启：⌘Space、⌘Tab 等会被屏蔽", fg="#5f9e7e")
        else:
            self.perm_label.configure(
                text="○ 组合键拦截未开启 · 点这里授权（辅助功能）", fg="#b08a4a")
        self.root.after(1500, lambda: self._refresh_perm_label(g))

    def on_perm_click(self) -> None:
        if self.state != "menu" or accessibility_trusted():
            return
        request_accessibility()
        open_accessibility_settings()

    def finish(self, elapsed: float) -> None:
        if not self.alive:
            return
        self.state = "done"
        self.elapsed = elapsed
        self.shield.stop()
        self._stop_keep_awake()
        if self.canvas is not None:
            self.canvas.configure(cursor="")
        self.close_at = time.monotonic() + 0.8
        try:
            self.root.bell()
        except tk.TclError:
            pass
        self._safe(self.draw_done, self.elapsed)
        if self.demo:
            self.root.after(2000, self.quit)

    def cancel(self) -> None:
        if self.state not in ("count", "clean"):
            return
        self.state = "menu"
        self.shield.stop()
        self.gen += 1
        self.esc_armed_at = None
        self._stop_keep_awake()
        self.build_menu()

    def quit(self) -> None:
        if not self.alive:
            return
        self.alive = False
        self.state = "quit"
        # 不在按键/定时回调里直接销毁窗口，避免 Tk 在事件派发中途退出卡死
        try:
            self.root.after(1, self._destroy)
        except tk.TclError:
            pass

    def _destroy(self) -> None:
        self.shield.stop()
        try:
            self._stop_keep_awake()
        except Exception:
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ 事件
    def on_key(self, event: tk.Event):
        key = event.keysym
        if self.state == "menu":
            if key == "Escape":
                self.quit()
            elif key in ("Return", "KP_Enter", "space"):
                self.begin()
            return
        if self.state == "count":
            if key == "Escape":
                self.cancel()
            return "break"
        if self.state == "clean":
            if key == "Escape":
                now = time.monotonic()
                if self.esc_armed_at is not None and now - self.esc_armed_at <= 1.5:
                    self.quit()
                else:
                    self.esc_armed_at = now
            return "break"
        if self.state == "done":
            if time.monotonic() >= self.close_at:
                self.quit()
            return "break"

    def on_key_release(self, event: tk.Event):
        # 只吞事件，不用它判断长按：macOS 自动重复会成对派发按下/松开
        return "break"

    def on_shortcut(self, _event=None):
        if self.state in ("count", "clean"):
            return "break"
        return None

    def on_click(self, _event=None):
        if self.state == "done" and time.monotonic() >= self.close_at:
            self.quit()

    # ------------------------------------------------------------------ 绘制
    def _show_stage(self) -> None:
        if self.menu_frame is not None:
            self.menu_frame.destroy()
            self.menu_frame = None
            self.chip_labels = {}
            self.theme_labels = {}
            self.start_canvas = None
        if self.canvas is None:
            cv = tk.Canvas(self.root, bg="#000000", highlightthickness=0, bd=0)
            cv.bind("<Configure>", lambda _e: self.redraw())
            cv.bind("<Button-1>", self.on_click)
            self.canvas = cv
        self.canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self.canvas.configure(cursor="none")
        self.root.update_idletasks()
        self.root.focus_force()
        self.root.after(900, self._ensure_focus)

    def _ensure_focus(self) -> None:
        if self.state in ("count", "clean"):
            self.root.lift()
            self.root.focus_force()

    def _hide_stage(self) -> None:
        if self.canvas is not None:
            self.canvas.destroy()
            self.canvas = None

    def redraw(self) -> None:
        if self.canvas is None:
            return
        if self.state == "count":
            self._safe(self.draw_count, self.count_left)
        elif self.state == "clean":
            now = time.monotonic()
            remaining = max(0.0, self.duration - (now - self.start_at))
            self._safe(self.draw_clean, remaining, self._esc_left(now))
        elif self.state == "done":
            self._safe(self.draw_done, self.elapsed)

    def _size(self):
        cv = self.canvas
        w = cv.winfo_width()
        h = cv.winfo_height()
        return cv, w, h

    def draw_count(self, n: int) -> None:
        if self.canvas is None:
            return
        cv, w, h = self._size()
        cv.delete("all")
        cv.configure(bg=THEMES[self.theme]["bg"])
        if w < 50 or h < 50:
            return
        cv.create_text(w / 2, h * 0.44, text=str(n), font=font(140, "bold"),
                       fill=THEMES[self.theme]["time"])
        cv.create_text(w / 2, h * 0.44 + 150, text="准备好抹布",
                       font=font(14), fill=THEMES[self.theme]["faint"])

    def draw_clean(self, remaining: float, esc_left: float | None = None,
                   total: float | None = None) -> None:
        if self.canvas is None:
            return
        cv, w, h = self._size()
        cv.delete("all")
        t = THEMES[self.theme]
        cv.configure(bg=t["bg"])
        if w < 80 or h < 80:
            return

        cx, cy = w / 2, h * 0.44
        r = min(w * 0.26, h * 0.27)
        ring_w = max(5, r * 0.028)
        box = (cx - r, cy - r, cx + r, cy + r)

        frac = 0.0 if not (total or self.duration) else max(
            0.0, min(1.0, remaining / (total or self.duration)))
        if frac <= 0:
            frac = 0.0
        cv.create_oval(*box, outline=t["track"], width=ring_w)
        if frac > 0.001:
            cv.create_arc(*box, start=90, extent=-359.9 * frac,
                          style="arc", outline=t["ring"], width=ring_w)

        cv.create_text(cx, cy - 4, text=clock(remaining),
                       font=font(int(max(30, min(96, r * 0.42)))),
                       fill=t["time"])
        cv.create_text(cx, cy - r - 46, text="清 洁 中", font=font(13),
                       fill=t["faint"])
        cv.create_text(cx, cy + r + 56, text="屏幕和键盘已锁定 · 请开始擦拭",
                       font=font(16), fill=t["hint"])
        cv.create_text(cx, cy + r + 90,
                       text=f"共 {pretty(self.duration)}，到点自动解锁",
                       font=font(12), fill=t["faint"])

        # 底部：Esc 提示 / 第二次按下的确认窗口
        y = h - 64
        if esc_left is not None:
            cv.create_text(cx, y, text="再按一下 Esc 退出", font=font(12),
                           fill=t["hint"])
            bar_w, bar_h = 200, 3
            bx = cx - bar_w / 2
            cv.create_rectangle(bx, y + 22, bx + bar_w, y + 22 + bar_h,
                                fill=t["track"], outline="")
            cv.create_rectangle(bx, y + 22,
                                bx + bar_w * min(1.0, esc_left / 1.5),
                                y + 22 + bar_h, fill=t["ok"], outline="")
        else:
            cv.create_text(cx, y, text="连按两下 Esc 可提前结束",
                           font=font(11), fill=t["faint"])
        if not self.shield.active:
            cv.create_text(cx, h - 30,
                           text=self.shield_note or "组合键未拦截 · 需在系统设置「辅助功能」中授权",
                           font=font(10), fill="#8a7048")

    def draw_done(self, elapsed: float) -> None:
        if self.canvas is None:
            return
        cv, w, h = self._size()
        cv.delete("all")
        t = THEMES[self.theme]
        cv.configure(bg=t["bg"])
        if w < 80 or h < 80:
            return

        cx, cy = w / 2, h * 0.44
        rr = min(54, h * 0.07)
        cv.create_oval(cx - rr, cy - rr - 30, cx + rr, cy + rr - 30,
                       outline=t["ok"], width=3)
        cv.create_text(cx, cy - 31, text="✓", font=font(int(rr * 0.95)),
                       fill=t["ok"])
        cv.create_text(cx, cy + rr + 14, text="清洁完成", font=font(26, "bold"),
                       fill=t["hint"])
        cv.create_text(cx, cy + rr + 58,
                       text=f"用时 {pretty(elapsed)} · 键盘和鼠标已恢复",
                       font=font(13), fill=t["faint"])
        cv.create_text(cx, h - 90, text="按任意键或点击任意位置关闭",
                       font=font(12), fill=t["faint"])

    # -------------------------------------------------------------- 不休眠
    def _start_keep_awake(self) -> None:
        if sys.platform == "darwin" and shutil.which("caffeinate"):
            try:
                self._caffeinate = subprocess.Popen(
                    ["caffeinate", "-d", "-i", "-t", str(int(self.duration) + 30)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                self._caffeinate = None
        elif sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(
                    0x80000000 | 0x00000001 | 0x00000002)
                self._win_awake = True
            except Exception:
                pass

    def _stop_keep_awake(self) -> None:
        if self._caffeinate is not None:
            try:
                self._caffeinate.terminate()
                self._caffeinate.wait(timeout=1)
            except Exception:
                pass
            self._caffeinate = None
        if self._win_awake:
            try:
                import ctypes
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
            except Exception:
                pass
            self._win_awake = False


def main() -> int:
    p = argparse.ArgumentParser(description="锁住屏幕和键盘，安心擦拭。")
    p.add_argument("seconds", nargs="?", type=int, default=None,
                   help="直接开始的秒数，例如 90")
    p.add_argument("--white", action="store_true", help="白色背景，更容易看见指纹")
    p.add_argument("--demo", action="store_true", help=argparse.SUPPRESS)
    args = p.parse_args()

    if args.seconds is not None and args.seconds <= 0:
        p.error("秒数需要大于 0")

    root = tk.Tk()
    duration = args.seconds
    if duration is None and args.demo:
        duration = 6
    app = CleanApp(root, duration=duration,
                   theme="light" if args.white else "dark",
                   demo=args.demo)
    if args.seconds is not None:
        root.after(350, app.begin)
    if args.demo:
        root.after(2200, app.begin)
    try:
        root.mainloop()
    finally:
        app._stop_keep_awake()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
