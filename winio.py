# Stellite NX - Windows input/output helpers
# Based on Stellite by Jinx (AGPL-3.0). Modified 2026-10-17. See LICENSE.
#
# - SendInput with hardware scan codes (what games read), several keys per call for chords
# - GetAsyncKeyState hotkeys (no global keyboard hook needed)
# - 1 ms system timer resolution + high process priority for steady timing
# - Screen capture thread on top of bettercam that timestamps every new frame itself
# Copyright (C) Jinx (jinxthecat_) and the Stellite contributors. Changes for Stellite NX (C) 2026 Starborne.
# This program is free software: you can redistribute it and/or modify it under the
# terms of the GNU Affero General Public License version 3 (see LICENSE). It comes WITHOUT ANY WARRANTY.

from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

ULONG_PTR = ctypes.c_size_t
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT
user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)
user32.MapVirtualKeyW.restype = wintypes.UINT
user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetKeyState.argtypes = (ctypes.c_int,)
user32.GetKeyState.restype = ctypes.c_short

_NAMED = {
    "space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "backspace": 0x08, "shift": 0x10, "ctrl": 0x11, "control": 0x11, "alt": 0x12,
    "capslock": 0x14, "caps lock": 0x14, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "home": 0x24, "end": 0x23, "insert": 0x2D, "delete": 0x2E, "pageup": 0x21, "pagedown": 0x22,
    "page up": 0x21, "page down": 0x22, ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE,
    "/": 0xBF, "`": 0xC0, "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
    "lshift": 0xA0, "rshift": 0xA1, "lctrl": 0xA2, "rctrl": 0xA3,
    "numadd": 0x6B, "numsub": 0x6D, "nummul": 0x6A, "numdiv": 0x6F, "numdec": 0x6E,
    "lalt": 0xA4, "ralt": 0xA5,
}
_EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0xA3, 0x6F, 0xA5}


def vk_from_name(name: str) -> int:
    k = str(name).strip().lower()
    if len(k) == 1 and (k.isalpha() or k.isdigit()):
        return ord(k.upper())
    if k.startswith("f") and k[1:].isdigit() and 1 <= int(k[1:]) <= 24:
        return 0x70 + int(k[1:]) - 1
    if k.startswith("num") and k[3:].isdigit():
        return 0x60 + int(k[3:])
    if k in _NAMED:
        return _NAMED[k]
    raise ValueError(f"Unknown key name: {name!r}")


class Keyboard:
    """Sends a batch of (lane, is_down) as one SendInput call using scan codes."""

    def __init__(self, key_names):
        self.codes = []
        for name in key_names:
            vk = vk_from_name(name)
            sc = user32.MapVirtualKeyW(vk, 0)  # MAPVK_VK_TO_VSC
            flags = KEYEVENTF_SCANCODE | (KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED else 0)
            self.codes.append((sc, flags))

    def send(self, actions):
        n = len(actions)
        arr = (INPUT * n)()
        for i, (lane, down) in enumerate(actions):
            sc, flags = self.codes[lane]
            arr[i].type = INPUT_KEYBOARD
            arr[i].ki = KEYBDINPUT(0, sc, flags | (0 if down else KEYEVENTF_KEYUP), 0, 0)
        user32.SendInput(n, arr, ctypes.sizeof(INPUT))


class Hotkeys:
    """Edge-triggered hotkey polling."""

    def __init__(self, mapping: dict):
        self.vk = {action: vk_from_name(key) for action, key in mapping.items() if key}
        self.prev = {a: False for a in self.vk}

    def poll(self):
        fired = []
        for action, vk in self.vk.items():
            down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
            if down and not self.prev[action]:
                fired.append(action)
            self.prev[action] = down
        return fired

    @staticmethod
    def held(name: str) -> bool:
        return bool(user32.GetAsyncKeyState(vk_from_name(name)) & 0x8000)


def capslock_on() -> bool:
    return bool(user32.GetKeyState(0x14) & 1)


def setup_process():
    """DPI aware (true pixel coords), 1 ms timer, high priority."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
    try:
        ctypes.WinDLL("winmm").timeBeginPeriod(1)
    except Exception:
        pass
    try:
        kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x00000080)  # HIGH_PRIORITY_CLASS
    except Exception:
        pass


def teardown_process():
    try:
        ctypes.WinDLL("winmm").timeEndPeriod(1)
    except Exception:
        pass


def screen_size(monitor: int = 0):
    import bettercam

    cam = bettercam.create(output_idx=monitor, output_color="GRAY")
    w, h = cam.width, cam.height
    return cam, w, h


class Capture:
    """Polls DXGI desktop duplication for new frames and stamps each one the moment it
    arrives. The main loop always gets the newest frame (older ones are dropped)."""

    def __init__(self, cam, region, clock):
        self.cam = cam
        self.region = region
        self.clock = clock
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._frame = None
        self._t = 0.0
        self._stop = False
        self.count = 0
        self.error = None
        self._thread = threading.Thread(target=self._run, name="capture", daemon=True)

    def start(self):
        self._thread.start()

    def _run(self):
        import numpy as np

        while not self._stop:
            try:
                f = self.cam.grab(region=self.region)
            except Exception as e:  # e.g. display mode change
                self.error = e
                time.sleep(0.05)
                continue
            if f is None:
                time.sleep(0.0002)
                continue
            t = self.clock()
            f = np.array(f[:, :, 0] if f.ndim == 3 else f, copy=True)  # own copy, never a reused buffer
            with self._lock:
                self._frame, self._t = f, t
                self.count += 1
            self._event.set()

    def latest(self, timeout=0.05):
        if not self._event.wait(timeout):
            return None, None
        with self._lock:
            self._event.clear()
            return self._frame, self._t

    def stop(self):
        self._stop = True
        self._thread.join(timeout=1)
