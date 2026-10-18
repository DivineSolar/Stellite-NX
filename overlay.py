# Stellite overlay
# Click through song clock and overdrive countdown that never steals focus from Fortnite
# Based on the Festival OD Coach overlay by Aknownthreat, made for the Stellite project (GPL-3.0)

import os
import gc
import json
import queue
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
import win32api
import win32con
import win32gui

TRANSPARENT = "#ff00ff" # this color gets cut out of the window
COLORS = {
    "white": "#ffffff",
    "dim": "#c8c8d0",
    "accent": "#f5c542",
    "now": "#ffe566",
    "good": "#7dff9a",
}
POSITION_FILE = os.path.join("data", "overlay.json")

GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000

def load_position(width, height):
    screen_width = win32api.GetSystemMetrics(0)
    screen_height = win32api.GetSystemMetrics(1)
    try:
        with open(POSITION_FILE, "r") as file:
            position = json.load(file)
        x = max(0, min(screen_width - width, int(position["x"])))
        y = max(0, min(screen_height - height, int(position["y"])))
        return x, y
    except Exception:
        return screen_width - width - 28, 28

def save_position(x, y):
    try:
        os.makedirs("data", exist_ok=True)
        with open(POSITION_FILE, "w") as file:
            json.dump({"x": x, "y": y}, file)
    except Exception:
        pass

def apply_style(hwnd, clickthrough):
    style = win32gui.GetWindowLong(hwnd, GWL_EXSTYLE)
    style |= WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | win32con.WS_EX_TOPMOST
    if clickthrough:
        style |= WS_EX_TRANSPARENT
    else:
        style &= ~WS_EX_TRANSPARENT
    win32gui.SetWindowLong(hwnd, GWL_EXSTYLE, style)
    win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0, win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE)

class Overlay:
    # click through while caps lock is on, drag it around while caps lock is off
    def __init__(self):
        self.updates = queue.Queue(maxsize=8)
        self.closed = False
        self.hwnd = None
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True, name="stellite-overlay")
        self.thread.start()
        self.ready.wait(timeout=3.0)

    def send(self, update):
        if self.closed:
            return
        # only the newest update matters, throw away old ones if tk is behind
        while self.updates.full():
            try:
                self.updates.get_nowait()
            except queue.Empty:
                break
        try:
            self.updates.put_nowait(update)
        except queue.Full:
            pass

    def show(self, big, small, color = "dim", big_color = "white"):
        self.send(("text", big, small, color, big_color))

    def set_clickthrough(self, enabled):
        self.send(("clickthrough", bool(enabled)))

    def close(self):
        self.closed = True
        self.thread.join(timeout=1.0)

    def run(self):
        root = tk.Tk()
        root.withdraw()
        root.title("Stellite Overlay")
        root.overrideredirect(True)
        root.configure(bg=TRANSPARENT)
        root.attributes("-topmost", True)
        root.attributes("-transparentcolor", TRANSPARENT)

        width, height = 280, 96
        x, y = load_position(width, height)
        root.geometry(f"{width}x{height}+{x}+{y}")

        try:
            big_font = tkfont.Font(family="Segoe UI Semibold", size=32)
            small_font = tkfont.Font(family="Segoe UI Semibold", size=12)
        except tk.TclError:
            big_font = tkfont.Font(size=32, weight="bold")
            small_font = tkfont.Font(size=12, weight="bold")

        shell = tk.Frame(root, bg=TRANSPARENT)
        shell.pack(fill="both", expand=True)
        big = tk.Label(shell, text="-:--", font=big_font, fg=COLORS["dim"], bg=TRANSPARENT)
        big.pack(anchor="center", pady=(6, 0))
        small = tk.Label(shell, text="Stellite", font=small_font, fg=COLORS["dim"], bg=TRANSPARENT)
        small.pack(anchor="center")

        drag = {"x": 0, "y": 0}
        clickthrough = {"on": True}

        def start_drag(event):
            if not clickthrough["on"]:
                drag["x"] = event.x_root - root.winfo_x()
                drag["y"] = event.y_root - root.winfo_y()

        def on_drag(event):
            if not clickthrough["on"]:
                root.geometry(f"+{event.x_root - drag['x']}+{event.y_root - drag['y']}")

        def end_drag(event):
            if not clickthrough["on"]:
                save_position(root.winfo_x(), root.winfo_y())

        for widget in (root, shell, big, small):
            widget.bind("<ButtonPress-1>", start_drag)
            widget.bind("<B1-Motion>", on_drag)
            widget.bind("<ButtonRelease-1>", end_drag)

        root.deiconify()
        root.update_idletasks()
        hwnd = int(root.winfo_id())
        parent = win32gui.GetParent(hwnd)
        if parent:
            hwnd = parent
        self.hwnd = hwnd
        apply_style(hwnd, True)
        self.ready.set()

        last_topmost = 0.0

        def poll():
            nonlocal last_topmost
            if self.closed:
                root.destroy()
                return
            try:
                while True:
                    update = self.updates.get_nowait()
                    if update[0] == "clickthrough":
                        clickthrough["on"] = update[1]
                        apply_style(hwnd, update[1])
                    else:
                        big.configure(text=update[1], fg=COLORS.get(update[4], update[4]))
                        small.configure(text=update[2] if clickthrough["on"] else "drag to move, caps lock locks it", fg=COLORS.get(update[3], update[3]))
            except queue.Empty:
                pass

            # fortnite likes to cover it up, put it back on top every few seconds
            now = time.perf_counter()
            if now - last_topmost >= 5.0:
                last_topmost = now
                try:
                    win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0, win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE)
                except Exception:
                    pass
            root.after(50, poll)

        root.after(50, poll)
        root.mainloop()
        # tk has to be cleaned up on the thread that made it, otherwise python crashes on exit
        root = shell = big = small = big_font = small_font = None
        gc.collect()
