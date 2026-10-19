# Stellite NX - Fortnite Festival autoplayer
#
# Based on Stellite by Jinx (https://github.com/JinxTheCatto/Stellite)
# Licensed under the GNU AGPL v3 (see LICENSE). Credit to Jinx and Stellite must be kept.
# Modified 2026-10-17: new engine (tracking, timing prediction, hold notes, overlay, replay).
# Modified 2026-10-18: merged with Stellite v2 (song charts from fnfpaths, chart sync, song picker,
# song timer overlay, overdrive path). charts.py and overlay.py are Stellite v2's, based on
# Festival OD Coach by Aknownthreat (GPL-3.0).
#
# Usage:
#   python bot.py                       play (Caps Lock on = playing)
#   python bot.py --replay FILE.npz     run the engine on a recording made with F6 and
#                                       print every press/release (add --out video.mp4)
# Copyright (C) Jinx (jinxthecat_) and the Stellite contributors. Changes for Stellite NX (C) 2026 Starborne.
# This program is free software: you can redistribute it and/or modify it under the
# terms of the GNU Affero General Public License version 3 (see LICENSE). It comes WITHOUT ANY WARRANTY.

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import threading
import time

import cv2
import numpy as np

from engine import Geometry, KeyScheduler, NoteDetector, Planner, clock, load_templates

if getattr(sys, "frozen", False):  # running as Stellite.exe
    HERE = os.path.dirname(os.path.abspath(sys.executable))
    RES = getattr(sys, "_MEIPASS", HERE)
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
    RES = HERE
CONFIG_PATH = os.path.join(HERE, "config.json")

DEFAULTS = {
    "lanes": 5,
    "keys": ["d", "f", "j", "k", "l"],
    "keys_4_lane": ["d", "f", "j", "k"],
    "play_key": "capslock",  # Caps Lock on = playing; any other key switches playing on/off
    "hotkeys": {
        "exit": "p",
        "overlay": "f8",
        "offset_down": "f9",
        "offset_up": "f10",
        "save_config": "f11",
        "snapshot": "f7",
        "record": "f6",
        "video": "f5",
        "song_select": "[",
        "sync": "]",
        "nudge_earlier": "-",
        "nudge_later": "=",
        "inst_lead": "num1",
        "inst_bass": "num2",
        "inst_drums": "num3",
        "inst_vocals": "num4",
        "inst_pro_lead": "num5",
        "inst_pro_bass": "num6",
        "inst_next": "f1",
        "overdrive_mode": "f2",
        "auto_detect": "f3",
    },
    "timing": {
        "offset_ms": 0.0,
        "tap_hold_ms": 30.0,
        "tap_linger_ms": 1000.0,
        "min_release_gap_ms": 25.0,
        "min_key_up_ms": 12.0,
        "dedupe_ms": 20.0,
        "lift_offset_ms": 4.0,
        "lift_prepress_ms": 40.0,
    },
    "geometry": {
        "press_line_y": 792.0,
        "lookahead_px": 230.0,
        "below_line_px": 45.0,
        "lane_width_5": 134.5,
        "lane_width_4": 138.75,
        "center_offset_5": 15.7,
        "center_offset_4": 1.0,
        "side_margin_px": 30.0,
        "taper": 0.0010,
        "learn_taper": True,
    },
    "detection": {
        "match_scale": 0.5,
        "template_scales": [1.0, 0.86],
        "min_hits": 2,
        "lift_templates": ["diamond"],
        "lift_min_edge": 40.0,
        "lift_min_std": 14.0,
        "lift_strong": 0.84,
        "lift_lead_px": 95.0,
        "templates": {
            "tile": 0.71,
            "white": 0.71,
            "battlestage": 0.71,
            "diamond": 0.76,
            "highcontrast": 0,
            "whitehighcontrast": 0,
        },
    },
    "holds": {
        "enabled": True,
        "min_tail_px": 28.0,
        "tail_min_brightness": 60.0,
        "tail_min_contrast": 40.0,
        "tail_near_contrast": 25.0,
        "tail_half_width_px": 9.0,
        "tail_search_slack_px": 8.0,
        "flank_offset_px": 30.0,
        "max_gap_px": 6.0,
        "press_line_margin_px": 18.0,
        "release_late_ms": 8.0,
        "bridge_px": 40.0,
        "min_end_slack_ms": 30.0,
        "glow_min_brightness": 170.0,
    },
    "charts": {
        "enabled": True,
        "instrument": "lead",
        "auto_detect": True,
        "detect_every_s": 0.25,
        "prepare_all": True,  # get every chart for your instrument ready ahead of time (once), so songs load instantly  # how often the screen is read for the song name while no chart is playing
        "sync_tolerance_ms": 35.0,
        "match_tolerance_ms": 35.0,
        "fill_in": True,
        "fill_in_lead_ms": 120.0,
        "fill_in_agreement": 0.9,
        "nudge_step_ms": 5.0,
        "show_overlay": True,
        "use_overdrive": False,
        "overdrive_key": "space",
        "overdrive_offset_ms": 0.0,
    },
    "capture": {"monitor": 0},
    "debug": {
        "overlay_on_start": False,
        "record_seconds": 3.0,
        "video_fps": 60,
        "judgment_box": [390, 760, 640, 905],
    },
}


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def migrate_old(old: dict) -> dict:
    """Accept the original Stellite config.json format."""
    new = {"keys": [old.get(f"key_{i}", DEFAULTS["keys"][i - 1]) for i in range(1, 6)],
           "hotkeys": {"exit": old.get("exit_key", "p")}}
    t = {"tile": old.get("min_confidence", 0.71)}
    yes = lambda k: str(old.get(k, "false")).lower() == "true"
    t["white"] = old.get("white_tile_min_confidence", 0.71) if yes("use_white_tile") else 0
    t["battlestage"] = old.get("battlestage_tile_min_confidence", 0.71) if yes("use_battlestage_tile") else 0
    t["diamond"] = old.get("diamond_tile_min_confidence", 0.84) if yes("use_diamond_tile") else 0
    new["detection"] = {"templates": t}
    if yes("always_single_lanemode"):
        new["lanes"] = old.get("single_lanemode_lanes", 5)
    else:
        new["lanes"] = "ask"
    return new


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r") as f:
            user = json.load(f)
    except FileNotFoundError:
        user = {}
    except Exception as e:
        print(f"ERROR: could not read config.json: {e}")
        sys.exit(1)
    if "key_1" in user:
        user = migrate_old(user)
    return deep_merge(DEFAULTS, user)


def save_config(cfg: dict):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


BANNER = r"""
   _____ __       ____  _ __          _   ___  __
  / ___// /____  / / / (_) /____     / | / / |/ /
  \__ \/ __/ _ \/ / / / / __/ _ \   /  |/ /|   /
 ___/ / /_/  __/ / / / / /_/  __/  / /|  //   |
/____/\__/\___/_/_/_/_/\__/\___/  /_/ |_//_/|_|
"""


# --------------------------------------------------------------------------------------
def run_live(cfg: dict):
    """Console version: same player as the app (core.Stellite), with a typed song picker."""
    import winio  # noqa: F401  (Windows only)
    from core import Stellite, INSTRUMENT_NAMES

    try:
        import colorama
        colorama.init()
    except Exception:
        pass
    os.system("title Stellite NX")
    print(BANNER)
    print("Fortnite Festival autoplayer. Based on Stellite by jinxthecat_ (AGPL-3.0).")
    if cfg["lanes"] == "ask":
        cfg["lanes"] = int(input("Number of lanes (4 = easy-hard, 5 = expert): ").strip())
    st = Stellite(cfg, console=True)

    def picker():
        if not st.songs_ready:
            print("\nThe song list isn't loaded yet")
            return
        import charts
        print(f"\nPick a song (click this window to type, Esc = keep the current one)")
        song = charts.prompt_song(st.songs)
        if song is None:
            return
        inst = charts.prompt_instrument(song, st.instrument)
        if inst is not None:
            st.set_song(song, inst)

    st.picker = picker
    hk = cfg["hotkeys"]
    print(f"CAPS LOCK on = play.  {hk['exit'].upper()} = exit.  {hk['song_select']} = pick a song.  "
          f"{hk['inst_next'].upper()} = next instrument.  {hk['overdrive_mode'].upper()} = overdrive manual/auto.  "
          f"{hk['auto_detect'].upper()} = song auto detect on/off.  {hk['nudge_earlier']} / {hk['nudge_later']} = timing.  "
          f"{hk['video'].upper()} = debug video.")
    print(f"Instrument: {INSTRUMENT_NAMES.get(st.instrument, st.instrument)}.  "
          f"Overdrive: {'AUTO' if cfg['charts'].get('use_overdrive') else 'MANUAL'}.  "
          f"Song auto detect: {'ON' if cfg['charts'].get('auto_detect', True) else 'OFF'}\n")
    try:
        st.run()
    except KeyboardInterrupt:
        st.running = False


class DebugVideo:
    """Writes the overlay + the game's judgment label to an mp4 from a background thread,
    plus a CSV of every key press/release (with the reason) and every frame's timestamp."""

    def __init__(self):
        self.active = False
        self.path = ""
        self.t0 = 0.0
        self._q = None
        self._thread = None
        self._last = 0.0
        self._frames = []

    def start(self, path, fps, t0):
        import queue
        self.path, self.fps, self.t0 = path, fps, t0
        self._q = queue.Queue(maxsize=240)
        self._frames = []
        self._last = 0.0
        self.active = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def due(self, t):
        return t - self._last >= 1.0 / self.fps

    def push(self, img, label, t):
        self._last = t
        h = max(img.shape[0], label.shape[0])
        canvas = np.zeros((h, label.shape[1] + img.shape[1], 3), np.uint8)
        canvas[:label.shape[0], :label.shape[1]] = label
        canvas[:img.shape[0], label.shape[1]:] = img
        if canvas.shape[1] > 1400:
            f = 1400 / canvas.shape[1]
            canvas = cv2.resize(canvas, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
        try:
            self._q.put_nowait((canvas, t))
        except Exception:
            pass  # writer is behind: drop this frame rather than slow the bot down

    def _run(self):
        writer = None
        idx = 0
        while True:
            item = self._q.get()
            if item is None:
                break
            canvas, t = item
            if writer is None:
                h, w = canvas.shape[:2]
                writer = cv2.VideoWriter(self.path, cv2.VideoWriter_fourcc(*"mp4v"), self.fps, (w - w % 2, h - h % 2))
            h, w = canvas.shape[:2]
            writer.write(canvas[:h - h % 2, :w - w % 2])
            self._frames.append((idx, t - self.t0))
            idx += 1
        if writer is not None:
            writer.release()

    def stop(self, trace, keys):
        self.active = False
        self._q.put(None)
        self._thread.join(timeout=30)
        with open(self.path[:-4] + ".csv", "w") as f:
            f.write("time_s,key,action,reason\n")
            for t, lane, down, why in list(trace):
                if t >= self.t0:
                    f.write(f"{t - self.t0:.4f},{keys[lane]},{'down' if down else 'up'},{why}\n")
        with open(self.path[:-4] + "_frames.csv", "w") as f:
            f.write("frame,time_s\n")
            for i, t in self._frames:
                f.write(f"{i},{t:.4f}\n")


def _save_recording(rec, cfg, sw, sh):
    frames = np.stack([f for f, _ in rec])
    times = np.array([t for _, t in rec], dtype=np.float64)
    meta = json.dumps({"screen_w": sw, "screen_h": sh, "config": cfg})
    path = os.path.join(HERE, "snapshots", time.strftime("rec_%Y%m%d-%H%M%S.npz"))

    def work():
        np.savez_compressed(path, frames=frames, times=times, meta=np.array(meta))
        print(f"\nRecording saved: {os.path.relpath(path, HERE)} ({len(rec)} frames)")

    threading.Thread(target=work, daemon=True).start()


# --------------------------------------------------------------------------------------
def run_replay(cfg: dict, path: str, out: str | None, quiet: bool = False):
    data = np.load(path, allow_pickle=False)
    frames, times = data["frames"], data["times"]
    meta = json.loads(str(data["meta"]))
    rec_cfg = meta["config"]
    cfg = copy.deepcopy(cfg)
    cfg["geometry"] = rec_cfg["geometry"]  # region must match the recording
    cfg["lanes"] = rec_cfg["lanes"]
    sw, sh = meta["screen_w"], meta["screen_h"]
    geo = Geometry(cfg, sw, sh)
    if (geo.h, geo.w) != frames.shape[1:3]:
        print("Recording size does not match geometry")
        return None
    det = NoteDetector(cfg, load_templates(cfg, os.path.join(RES, "assets"), sh, log=lambda *_: None), sh)
    sched = KeyScheduler(cfg["lanes"], lambda a: None, cfg, threaded=False, log=True)
    planner = Planner(cfg, geo, det, sched)
    writer = None
    keys = cfg["keys"]
    wake = None
    for frame, t in zip(frames, times):
        t = float(t)
        guard = 0
        while wake is not None and wake <= t and guard < 1000:  # fire events between frames at their exact time
            wake = sched.step(wake)
            guard += 1
        planner.process(frame, t)
        wake = sched.step(t)
        if out:
            img = planner.draw(frame, float(t), keys=keys)
            if writer is None:
                writer = cv2.VideoWriter(out, cv2.VideoWriter_fourcc(*"mp4v"), 60, (img.shape[1], img.shape[0]))
            writer.write(img)
    # drain
    t_end = float(times[-1]) + 1.0
    guard = 0
    while wake is not None and wake <= t_end and guard < 10000:
        wake = sched.step(wake)
        guard += 1
    if writer:
        writer.release()
    t0 = float(times[0])
    if not quiet:
        for t, lane, down in sched.log:
            print(f"{(t - t0) * 1000:9.1f} ms  {keys[lane].upper()}  {'DOWN' if down else 'up'}")
        print(f"\n{sched.presses} presses, {sched.holds} holds, {sched.lifts} lifts")
    return sched.log


def main():
    ap = argparse.ArgumentParser(description="Stellite NX - Fortnite Festival autoplayer")
    ap.add_argument("--replay", help="run the engine on a recording (.npz) instead of playing")
    ap.add_argument("--out", help="with --replay: write an overlay video (.mp4)")
    args = ap.parse_args()
    cfg = load_config()
    if args.replay:
        run_replay(cfg, args.replay, args.out)
    else:
        run_live(cfg)


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    main()
