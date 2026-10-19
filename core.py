# Stellite NX - the player itself, shared by the app (app.py) and the console version (bot.py)
#
# Based on Stellite by Jinx (https://github.com/JinxTheCatto/Stellite), AGPL-3.0.
# Modified 2026-10-18: one engine class with song charts, automatic song detection, instrument and
# overdrive hotkeys, so a GUI can drive it.
# Copyright (C) Jinx (jinxthecat_) and the Stellite contributors. Changes for Stellite NX (C) 2026 Starborne.
# This program is free software: you can redistribute it and/or modify it under the
# terms of the GNU Affero General Public License version 3 (see LICENSE). It comes WITHOUT ANY WARRANTY.

from __future__ import annotations

import os
import queue
import sys
import threading
import time
import traceback
from collections import deque

import cv2
import numpy as np

from engine import Geometry, KeyScheduler, NoteDetector, Planner, clock, load_templates

# where config.json / snapshots / downloaded charts live, and where bundled files are
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
    RES_DIR = getattr(sys, "_MEIPASS", APP_DIR)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
    RES_DIR = APP_DIR

VERSION = "1.0.0"
# Where people can get this version's source code. Put the link of your release (or GitHub fork) here
# before building a release; the About screen shows it. The source zip ships next to the exe as well.
SOURCE_URL = "https://github.com/daxlagan-debug/Stellite-NX"
UPSTREAM_URL = "https://github.com/JinxTheCatto/Stellite"

INSTRUMENTS = ["lead", "bass", "drums", "vocals", "pro_lead", "pro_bass"]
INSTRUMENT_NAMES = {"lead": "Lead", "bass": "Bass", "drums": "Drums", "vocals": "Vocals",
                    "pro_lead": "Pro Lead", "pro_bass": "Pro Bass"}
INSTRUMENT_HOTKEYS = {f"inst_{i}": i for i in INSTRUMENTS}

# hotkey actions shown in the app, in order: (action, label, group)
HOTKEY_ACTIONS = [
    ("inst_lead", "Lead", "Instruments"), ("inst_bass", "Bass", "Instruments"),
    ("inst_drums", "Drums", "Instruments"), ("inst_vocals", "Vocals", "Instruments"),
    ("inst_pro_lead", "Pro Lead", "Instruments"), ("inst_pro_bass", "Pro Bass", "Instruments"),
    ("inst_next", "Next instrument", "Instruments"),
    ("overdrive_mode", "Overdrive: manual / auto", "Play"),
    ("auto_detect", "Song auto detect on / off", "Play"),
    ("song_select", "Pick a song", "Play"),
    ("sync", "Sync chart on GO", "Play"),
    ("nudge_earlier", "Notes 5 ms earlier", "Timing"), ("nudge_later", "Notes 5 ms later", "Timing"),
    ("offset_down", "Notes 1 ms earlier", "Timing"), ("offset_up", "Notes 1 ms later", "Timing"),
    ("overlay", "Debug overlay", "Debug"), ("video", "Debug video (whole song)", "Debug"),
    ("snapshot", "Snapshot", "Debug"), ("record", "Short recording", "Debug"),
]


_KEY_NAMES = {"capslock": "Caps Lock", "space": "Space", "lshift": "Left Shift", "rshift": "Right Shift",
              "lctrl": "Left Ctrl", "rctrl": "Right Ctrl", "lalt": "Left Alt", "ralt": "Right Alt",
              "pageup": "Page Up", "pagedown": "Page Down", "enter": "Enter", "tab": "Tab"}


def key_label(key: str) -> str:
    k = str(key or "").lower()
    if k in _KEY_NAMES:
        return _KEY_NAMES[k]
    if k.startswith("num") and len(k) > 3:
        return "Num " + {"add": "+", "sub": "-", "mul": "*", "div": "/", "dec": "."}.get(k[3:], k[3:])
    return k.upper()


def split_name(name: str):
    if " by " in name:
        title, artist = name.rsplit(" by ", 1)
        return title, artist
    return name, ""


class Stellite:
    def __init__(self, cfg: dict, console: bool = False):
        self.cfg = cfg
        self.console = console
        self.lock = threading.RLock()
        self.logs: deque = deque(maxlen=200)
        self.log_seq = 0
        self.running = False
        self.thread = None
        self.error = ""
        self.picker = None  # console mode: function that asks for a song
        self.search_requests = 0  # app mode: song_select hotkey asks the app to open the search
        # engine (made in run)
        self.planner = self.sched = self.det = None
        self.active = False
        self.fps = 0.0
        self.screen = ""
        self.ready = False
        # songs
        c = cfg["charts"]
        self.charts = None
        self.songs, self.lengths = [], {}
        self.songs_ready = False
        self.song = None
        self.chart = self.sync = self.assist = None
        self.loading = ""
        self.instrument = str(c.get("instrument", "lead"))
        self.detector = None
        self.detected = ""
        self.timer_overlay = None
        self.od_kb = None
        self.od_release = None
        self.msg = ("", 0.0)
        self.last_overlay = 0.0
        self._hot_dirty = True
        self._keys_dirty = False
        self._overlay_dirty = False
        self._restart = False
        self._songs_started = False
        self.hotkeys_paused = False
        self.closed = False
        self.playing = False  # play state when the play key is not Caps Lock
        self._kb = None
        self._kb_lock = threading.Lock()

    # ------------------------------------------------------------------ logging
    def log(self, text: str):
        with self.lock:
            self.log_seq += 1
            self.logs.append((self.log_seq, time.strftime("%H:%M:%S"), text))
        if self.console:
            print("\n" + text)

    def note(self, text: str):
        self.msg = (text, clock() + 1.5)

    # ------------------------------------------------------------------ songs
    def _load_song_list(self):
        if not self.cfg["charts"].get("enabled", True):
            self.log("Song charts are off: playing from the screen only")
            return
        try:
            os.chdir(APP_DIR)
            import charts
            self.charts = charts
            songs = charts.load_songs()
            lengths = charts.load_song_lengths()
        except Exception as e:
            self.log(f"Song list unavailable ({e}): playing from the screen only")
            return
        with self.lock:
            self.songs, self.lengths = songs or [], lengths or {}
            self.songs_ready = bool(self.songs)
        if not self.songs:
            self.log("No song list from fnfpaths: playing from the screen only")
            return
        self.log(f"{len(self.songs)} songs ready")
        self._start_detector()
        threading.Thread(target=self._prepare_charts, daemon=True, name="chart-prepare").start()

    def _start_detector(self):
        try:
            from songdetect import AutoSongDetector
        except Exception as e:
            self.log(f"Song detection unavailable ({e})")
            return
        self.detector = AutoSongDetector(self.songs, self._song_detected, self._should_look, self.log,
                                         interval=float(self.cfg["charts"].get("detect_every_s", 0.25)))
        if self.detector.start():
            if self.cfg["charts"].get("auto_detect", True):
                self.log("Song auto detect is on: the song is read off the screen (Up Next, playlists, in song)")
        else:
            self.detector = None

    def _should_look(self):
        if not self.cfg["charts"].get("auto_detect", True) or (self.sync is not None and self.sync.locked):
            return False
        # a chart is loaded and notes are being played: keep checking, but gently (it's probably the right song)
        playing_notes = self.active and clock() - getattr(self, "last_press_t", -9.0) < 3.0
        return "slow" if (self.chart is not None and playing_notes) else True

    def _song_detected(self, song):
        with self.lock:
            same = self.song is not None and self.song["name"] == song["name"]
        self.detected = song["name"]
        if same:
            return
        self.log(f"Song on screen: {song['name']}")
        self.set_song(song, source="auto")

    def search(self, query: str, limit: int = 12):
        if not self.songs_ready:
            return []
        import charts
        return [{"name": s["name"], "title": split_name(s["name"])[0], "artist": split_name(s["name"])[1],
                 "instruments": [i for i in INSTRUMENTS if i in s["paths"]]}
                for s in charts.find_songs(self.songs, query)[:limit]]

    def set_song(self, song, instrument: str | None = None, source: str = "manual"):
        """song: dict from the song list or its name. Loads the chart in the background."""
        if isinstance(song, str):
            match = next((s for s in self.songs if s["name"] == song), None)
            if match is None:
                self.log(f"Unknown song: {song}")
                return False
            song = match
        if instrument:
            self.instrument = instrument
        if source == "manual" and self.detector is not None:
            self.detector.picked(song)
        self._spawn_load(song, self.instrument, source)
        return True

    def _spawn_load(self, song, instrument, source):
        self.load_gen = getattr(self, "load_gen", 0) + 1
        threading.Thread(target=self._load_chart, args=(song, instrument, source, self.load_gen), daemon=True).start()

    def _load_chart(self, song, instrument, source, gen=None):
        t0 = time.perf_counter()
        available = [i for i in INSTRUMENTS if i in song["paths"]]
        if not available:
            self.log(f"No charts for {song['name']}")
            return
        if instrument not in available:
            self.log(f"{song['name']} has no {INSTRUMENT_NAMES.get(instrument, instrument)} chart, "
                     f"using {INSTRUMENT_NAMES[available[0]]}")
            instrument = available[0]
        self.loading = song["name"]
        try:
            chart = self.charts.load_chart(song, instrument, self.lengths, prefer_saved=True)
        except Exception as e:
            if gen is None or gen == self.load_gen:
                self.loading = ""
            self.log(f"Could not read the chart for {song['name']} ({e})")
            return
        if gen is not None and gen != self.load_gen:
            return  # a newer song or instrument was asked for while this one loaded
        from chartassist import ChartAssist
        sync = self.charts.ChartSync(chart, self.cfg["charts"]["sync_tolerance_ms"] / 1000.0)
        sync.resume(clock())
        assist = ChartAssist(chart, sync, self.cfg)
        with self.lock:
            self.song, self.chart, self.sync, self.assist = song, chart, sync, assist
            self.loading = ""
            if self.planner is not None and getattr(self, "lanes_now", 5) == 5:
                self.planner.assist = assist
        sus = sum(1 for n in chart["notes"] if n[2] > 0)
        self.log(f"{'Detected' if source == 'auto' else 'Loaded'}: {song['name']} - "
                 f"{INSTRUMENT_NAMES[instrument]} Expert ({len(chart['notes'])} notes, {sus} sustains) "
                 f"in {(time.perf_counter() - t0) * 1000:.0f} ms")
        threading.Thread(target=self._after_load, args=(song, instrument, chart.get("from_saved")),
                         daemon=True, name="chart-prefetch").start()

    def _prepare_charts(self):
        """Once, in the background: download and read every chart for the chosen instrument, so any song
        loads in a few ms when it's detected. Pauses while playing; one download a second at most."""
        self.charts.start_parser()
        self.prep = (0, 0)
        announced = set()
        while not self.closed:
            if not self.cfg["charts"].get("prepare_all", True):
                time.sleep(1.0)
                continue
            instrument = self.instrument
            todo = [s for s in self.songs if instrument in s["paths"] and not self.charts.is_ready(s, instrument)]
            total = sum(1 for s in self.songs if instrument in s["paths"])
            self.prep = (total - len(todo), total)
            if not todo:
                if instrument not in announced and total:
                    announced.add(instrument)
                    self.log(f"All {total} {INSTRUMENT_NAMES[instrument]} charts are ready: songs load instantly")
                time.sleep(2.0)
                continue
            if instrument not in announced and len(todo) > 20 and not getattr(self, "_prep_said", None) == instrument:
                self._prep_said = instrument
                self.log(f"Getting {len(todo)} {INSTRUMENT_NAMES[instrument]} charts ready in the background "
                         "(once), so every song loads instantly")
            for song in todo:
                if self.instrument != instrument or not self.cfg["charts"].get("prepare_all", True):
                    break
                while self.active or self.loading:  # never while playing or loading the song that's needed
                    time.sleep(0.5)
                t0 = time.time()
                try:
                    image = song["paths"][instrument]["image"]
                    png, saved = self.charts.fetch_png(image, prefer_saved=True)
                    self.charts.read_png(png, image, ahead=True)
                except Exception:
                    time.sleep(5.0)  # offline or fnfpaths is down: try again slowly
                self.prep = (self.prep[0] + 1, total)
                time.sleep(max(0.0, 1.0 - (time.time() - t0)))

    def _after_load(self, song, instrument, from_saved):
        """Get the song's other instruments ready (switching is instant then) and update a saved chart
        if fnfpaths changed it."""
        if from_saved:
            try:
                image = song["paths"][instrument]["image"]
                if self.charts.refresh_png(image):
                    cur = self.chart
                    if (cur is not None and cur.get("name") == song["name"] and cur.get("instrument") == instrument
                            and not (self.sync is not None and self.sync.locked)):
                        self.log(f"fnfpaths updated the {INSTRUMENT_NAMES[instrument]} chart for {song['name']}: reloading it")
                        self._spawn_load(song, instrument, "manual")
            except Exception:
                pass
        for other in INSTRUMENTS:
            if other == instrument or other not in song["paths"] or self.charts.is_ready(song, other):
                continue
            if self.song is None or self.song["name"] != song["name"]:
                return  # moved on to another song
            try:
                png, _ = self.charts.fetch_png(song["paths"][other]["image"], prefer_saved=True)
                self.charts.read_png(png, song["paths"][other]["image"], ahead=True)
            except Exception:
                pass

    def set_instrument(self, instrument: str):
        if instrument not in INSTRUMENTS:
            return False
        self.instrument = instrument
        self.cfg["charts"]["instrument"] = instrument
        self._save()
        self.note(INSTRUMENT_NAMES[instrument])
        if self.song is not None and self.chart is not None and self.chart.get("instrument") != instrument:
            self.log(f"Instrument: {INSTRUMENT_NAMES[instrument]}")
            self._spawn_load(self.song, instrument, "manual")
        else:
            self.log(f"Instrument: {INSTRUMENT_NAMES[instrument]}")
        return True

    def next_instrument(self):
        order = INSTRUMENTS
        if self.song is not None:
            order = [i for i in INSTRUMENTS if i in self.song["paths"]] or INSTRUMENTS
        current = self.chart.get("instrument") if self.chart else self.instrument
        i = order.index(current) if current in order else -1
        return self.set_instrument(order[(i + 1) % len(order)])

    def set_overdrive_auto(self, on: bool):
        self.cfg["charts"]["use_overdrive"] = bool(on)
        self._save()
        self.log("Overdrive: AUTO (fnfpaths path)" if on else "Overdrive: MANUAL (yours)")
        self.note("overdrive auto" if on else "overdrive manual")
        return True

    def set_auto_detect(self, on: bool):
        self.cfg["charts"]["auto_detect"] = bool(on)
        self._save()
        if on and self.detector is not None:
            self.detector.forget()
        self.log("Song auto detect: ON" if on else "Song auto detect: OFF")
        return True

    def nudge(self, ms: float):
        self.cfg["timing"]["offset_ms"] = round(self.cfg["timing"]["offset_ms"] + ms, 1)
        if self.planner is not None:
            self.planner.offset = self.cfg["timing"]["offset_ms"] / 1000.0
        self._save()
        v = self.cfg["timing"]["offset_ms"]
        words = "on time" if v == 0 else f"{abs(v):g} ms {'later' if v > 0 else 'earlier'}"
        self.log(f"Notes now play {words}")
        self.note(f"notes {words}")
        return v

    def sync_on_go(self):
        if self.sync is None:
            return
        if self.sync.locked and not self.sync.manual:
            self.note("already synced")
        else:
            self.sync.start_now(clock())
            self.note("synced on GO")
            self.log("Synced on GO")

    # ------------------------------------------------------------------ keys
    # One key, one job. The bot's own lane and overdrive presses are seen by the hotkey reader, so a
    # lane key, the overdrive key and the play key can never be hotkeys as well.
    def _lane_sets(self):
        return {5: self.cfg["keys"], 4: self.cfg["keys_4_lane"]}

    def _play_key(self):
        return str(self.cfg.get("play_key", "capslock") or "capslock").lower()

    def _caps_play(self):
        return self._play_key() in ("capslock", "caps lock")

    def _jobs(self, skip=None):
        """{key: what it does} for lane keys, play key and overdrive key (not hotkeys)."""
        jobs = {}
        for n, keys in self._lane_sets().items():
            for i, k in enumerate(keys):
                if ("lane", n, i) != skip and k:
                    jobs.setdefault(str(k).lower(), f"lane {i + 1}" + ("" if n == 5 else " (4 lanes)"))
        if skip != "play":
            jobs[self._play_key()] = "the play key"
        if skip != "overdrive":
            jobs[str(self.cfg["charts"].get("overdrive_key", "space")).lower()] = "the overdrive key"
        return jobs

    @staticmethod
    def _check_key(key: str):
        import winio
        winio.vk_from_name(key)  # raises on a key it can't press or read

    def _free_hotkey(self, key: str, why: str):
        for action, k in self.cfg["hotkeys"].items():
            if k and str(k).lower() == key:
                self.cfg["hotkeys"][action] = ""
                self.log(f"Hotkey {key_label(key)} removed from '{dict((a, l) for a, l, g in HOTKEY_ACTIONS).get(action, action)}': it is {why} now")
        self._hot_dirty = True

    def _result(self, error: str = ""):
        if error:
            self.log(error)
        return {"ok": not error, "error": error, "hotkeys": dict(self.cfg["hotkeys"]), "controls": self.controls()}

    def set_hotkey(self, action: str, key: str):
        key = (key or "").strip().lower()
        if key:
            try:
                self._check_key(key)
            except ValueError:
                return self._result(f"{key_label(key)} can't be used as a hotkey")
            job = self._jobs().get(key)
            if job:
                return self._result(f"{key_label(key)} is {job}. Pick another key for this hotkey")
            for other, k in self.cfg["hotkeys"].items():
                if other != action and str(k).lower() == key:
                    self.cfg["hotkeys"][other] = ""
        self.cfg["hotkeys"][action] = key
        self._save()
        self._hot_dirty = True
        return self._result()

    def set_lane_key(self, lanes: int, lane: int, key: str):
        lanes = 4 if int(lanes) == 4 else 5
        keys = self._lane_sets()[lanes]
        key = (key or "").strip().lower()
        if not key or not 0 <= int(lane) < lanes:
            return self._result("Every lane needs a key")
        lane = int(lane)
        try:
            self._check_key(key)
        except ValueError:
            return self._result(f"{key_label(key)} can't be used as a lane key")
        if key in keys and keys.index(key) != lane:
            j = keys.index(key)  # already another lane's key: swap the two
            keys[j] = keys[lane]
        else:
            job = self._jobs(skip=("lane", lanes, lane)).get(key)
            if job and not job.startswith("lane"):
                return self._result(f"{key_label(key)} is {job}. Pick another key for lane {lane + 1}")
        keys[lane] = key
        self._free_hotkey(key, f"lane {lane + 1}")
        self._save()
        self._keys_dirty = True
        self.log(f"Lane keys{'' if lanes == 5 else ' (4 lanes)'}: {' '.join(key_label(k) for k in keys)}")
        return self._result()

    def set_play_key(self, key: str):
        key = (key or "").strip().lower() or "capslock"
        try:
            self._check_key(key)
        except ValueError:
            return self._result(f"{key_label(key)} can't be used as the play key")
        job = self._jobs(skip="play").get(key)
        if job:
            return self._result(f"{key_label(key)} is {job}. Pick another play key")
        self.cfg["play_key"] = key
        self._free_hotkey(key, "the play key")
        self.playing = False
        self._save()
        self.log("Play key: Caps Lock (on = playing)" if self._caps_play() else f"Play key: {key_label(key)} switches playing on and off")
        return self._result()

    def set_overdrive_key(self, key: str):
        key = (key or "").strip().lower()
        if not key:
            return self._result("Overdrive needs a key")
        try:
            self._check_key(key)
        except ValueError:
            return self._result(f"{key_label(key)} can't be used for overdrive")
        job = self._jobs(skip="overdrive").get(key)
        if job:
            return self._result(f"{key_label(key)} is {job}. Pick another overdrive key")
        if self.od_release is not None and self.od_kb is not None:
            self.od_kb.send([(0, False)])
            self.od_release = None
        self.cfg["charts"]["overdrive_key"] = key
        self.od_kb = None
        self._free_hotkey(key, "the overdrive key")
        self._save()
        self.log(f"Overdrive key: {key_label(key)} (the key Fortnite uses for overdrive)")
        return self._result()

    def reset_keys(self):
        from bot import DEFAULTS
        self.cfg["keys"] = list(DEFAULTS["keys"])
        self.cfg["keys_4_lane"] = list(DEFAULTS["keys_4_lane"])
        self.cfg["play_key"] = DEFAULTS["play_key"]
        self.cfg["charts"]["overdrive_key"] = DEFAULTS["charts"]["overdrive_key"]
        self.cfg["hotkeys"] = dict(DEFAULTS["hotkeys"])
        self.od_kb = None
        self.playing = False
        self._save()
        self._keys_dirty = self._hot_dirty = True
        self.log("Keys and hotkeys are back to the defaults")
        return self._result()

    def set_lanes(self, lanes: int):
        lanes = 4 if int(lanes) == 4 else 5
        if self.cfg["lanes"] == lanes:
            return self._result()
        self.cfg["lanes"] = lanes
        self._save()
        self.log(f"{lanes} lanes: restarting the note reader")
        self.restart()
        return self._result()

    def set_monitor(self, monitor: int):
        self.cfg["capture"]["monitor"] = max(0, int(monitor))
        self._save()
        self.log(f"Reading screen {self.cfg['capture']['monitor'] + 1}: restarting the note reader")
        self.restart()
        return self._result()

    def set_setting(self, name: str, value):
        c = self.cfg["charts"]
        if name == "show_overlay":
            c["show_overlay"] = bool(value)
            self._overlay_dirty = True
            self.log("In-game song timer: " + ("on" if value else "off"))
        elif name == "fill_in":
            c["fill_in"] = bool(value)
            if self.assist is not None:
                self.assist.fill_in = bool(value)
            self.log("Chart fills in notes the screen missed: " + ("on" if value else "off"))
        elif name == "overdrive_offset_ms":
            c["overdrive_offset_ms"] = round(max(-500.0, min(500.0, float(value))), 1)
            self.log(f"Overdrive presses {abs(c['overdrive_offset_ms']):g} ms "
                     f"{'later' if c['overdrive_offset_ms'] >= 0 else 'earlier'} than the path")
        elif name == "prepare_all":
            c["prepare_all"] = bool(value)
            self.log("Getting all charts ready ahead of time: " + ("on" if value else "off"))
        elif name == "detect_every_s":
            c["detect_every_s"] = round(max(0.25, min(5.0, float(value))), 2)
            if self.detector is not None:
                self.detector.interval = c["detect_every_s"]
        else:
            return self._result(f"Unknown setting {name}")
        self._save()
        return self._result()

    def controls(self) -> dict:
        c = self.cfg["charts"]
        return {"lanes": self.cfg["lanes"] if self.cfg["lanes"] in (4, 5) else 5,
                "keys": list(self.cfg["keys"][:5]), "keys_4_lane": list(self.cfg["keys_4_lane"][:4]),
                "play_key": self._play_key(), "overdrive_key": c.get("overdrive_key", "space"),
                "monitor": int(self.cfg["capture"].get("monitor", 0)),
                "show_overlay": bool(c.get("show_overlay", True)), "fill_in": bool(c.get("fill_in", True)),
                "overdrive_offset_ms": float(c.get("overdrive_offset_ms", 0.0)),
                "detect_every_s": float(c.get("detect_every_s", 0.25)),
                "detect_read_ms": round(self.detector.read_ms) if self.detector is not None else 0,
                "prepare_all": bool(c.get("prepare_all", True)),
                "prep_done": getattr(self, "prep", (0, 0))[0], "prep_total": getattr(self, "prep", (0, 0))[1]}

    def set_option(self, section: str, name: str, value):
        self.cfg.setdefault(section, {})[name] = value
        self._save()
        return True

    def _save(self):
        if getattr(self, "read_only", False):
            return
        try:
            from bot import save_config
            save_config(self.cfg)
        except Exception as e:
            self.log(f"Could not save config.json ({e})")

    # ------------------------------------------------------------------ status for the app
    def state(self) -> dict:
        with self.lock:
            song, chart, sync, assist = self.song, self.chart, self.sync, self.assist
            logs = list(self.logs)[-40:]
        now = clock()
        status, timer, progress, next_od = "none", "-:--", 0.0, ""
        length = None
        if self.loading:
            status = "loading"
        elif chart is not None:
            length = chart.get("length") or chart.get("end")
            if sync.locked:
                status = "synced" if assist.healthy() else "loose"
                t = sync.song_time(now)
                timer = self._clock(length - t) if length else self._clock(t)
                progress = max(0.0, min(1.0, t / length)) if length else 0.0
                if self.cfg["charts"].get("use_overdrive"):
                    labels = chart.get("overdrive_labels", [])
                    for i, (p, lo, hi) in enumerate(chart.get("overdrive", [])):
                        if 0 <= t - p < 0.85:
                            next_od = f"NOW  {labels[i] if i < len(labels) else ''}"
                            break
                        if p > t:
                            at = (length - p) if length else (p - t)
                            next_od = f"{self._clock(at)}  {labels[i] if i < len(labels) else ''}"
                            break
            else:
                status = "syncing"
                timer = self._clock(length) if length else "-:--"
        title, artist = split_name(song["name"]) if song else ("", "")
        sched = self.sched
        return {
            "ready": self.ready, "running": self.running, "error": self.error,
            "active": self.active, "fps": round(self.fps), "screen": self.screen,
            "detect_ms": round(self.det.last_ms, 1) if self.det else 0,
            "presses": sched.presses if sched else 0, "holds": sched.holds if sched else 0,
            "lifts": sched.lifts if sched else 0,
            "lanes": [bool(l.down) for l in sched.lanes] if sched else [False] * 5,
            "offset_ms": self.cfg["timing"]["offset_ms"],
            "song": {"name": song["name"], "title": title, "artist": artist,
                     "instruments": [i for i in INSTRUMENTS if i in song["paths"]]} if song else None,
            "loading": self.loading, "detected": self.detected,
            "instrument": (chart or {}).get("instrument") or self.instrument,
            "chart_status": status, "timer": timer, "progress": progress,
            "overdrive_auto": bool(self.cfg["charts"].get("use_overdrive")), "next_od": next_od,
            "overdrive_key": self.cfg["charts"].get("overdrive_key", "space"),
            "auto_detect": bool(self.cfg["charts"].get("auto_detect", True)),
            "songs_ready": self.songs_ready, "song_count": len(self.songs),
            "hotkeys": dict(self.cfg["hotkeys"]), "search_requests": self.search_requests,
            "controls": self.controls(),
            "log": logs,
        }

    @staticmethod
    def _clock(seconds):
        seconds = max(0.0, seconds or 0.0)
        return f"{int(seconds // 60)}:{int(seconds % 60):02d}"

    # ------------------------------------------------------------------ run
    def start(self):
        self.thread = threading.Thread(target=self._run_safe, name="stellite", daemon=True)
        self.thread.start()

    def stop(self):
        self.closed = True
        self._restart = False
        self.running = False
        if self.thread is not None:
            self.thread.join(timeout=3)

    def restart(self):
        """Rebuild the note reader (lanes or screen changed). Songs and charts stay loaded."""
        self._restart = True
        self.running = False
        if self.thread is None or not self.thread.is_alive():
            self.start()

    def _run_safe(self):
        while True:
            self._restart = False
            self.error = ""
            try:
                self.run()
            except Exception as e:
                self.error = f"{type(e).__name__}: {e}"
                self.log("Stopped by an error: " + self.error)
                self.log(traceback.format_exc(limit=3))
            if not self._restart:
                break

    def run(self):
        import winio
        from bot import DebugVideo, _save_recording, save_config

        cfg = self.cfg
        os.chdir(APP_DIR)
        lanes = cfg["lanes"] if cfg["lanes"] in (4, 5) else 5
        cfg["lanes"] = lanes
        self.ready = False
        lane_keys = lambda: (cfg["keys"][:5] if lanes == 5 else cfg.get("keys_4_lane", cfg["keys"][:4])[:4])
        keys = lane_keys()
        winio.setup_process()
        sys.setswitchinterval(0.0005)

        if not self._songs_started:
            self._songs_started = True
            threading.Thread(target=self._load_song_list, daemon=True).start()
        if lanes == 4:
            self.log("4 lanes: song charts are expert only (5 lanes), playing from the screen only")

        cam, sw, sh = winio.screen_size(cfg["capture"].get("monitor", 0))
        self.screen = f"{sw}x{sh}"
        geo = Geometry(cfg, sw, sh)
        templates = load_templates(cfg, os.path.join(RES_DIR, "assets"), sh, log=self.log)
        det = NoteDetector(cfg, templates, sh)
        self._kb = winio.Keyboard(keys)
        self._keys_dirty = False
        sched = KeyScheduler(lanes, self._send, cfg)
        planner = Planner(cfg, geo, det, sched)
        self.lanes_now = lanes
        with self.lock:
            self.det, self.sched, self.planner = det, sched, planner
            planner.assist = self.assist if lanes == 5 else None
        s_ = sh / 1080.0
        jb = [int(round(v * s_)) for v in cfg["debug"]["judgment_box"]]
        L, T, R, B = geo.region
        ul, ut = max(0, min(L, jb[0])), max(0, min(T, jb[1]))
        ur, ub = min(sw, max(R, jb[2])), min(sh, max(B, jb[3]))
        ex, ey = L - ul, T - ut
        lab = (max(0, jb[0] - ul), max(0, jb[1] - ut), min(ur, jb[2]) - ul, min(ub, jb[3]) - ut)
        cap = winio.Capture(cam, (ul, ut, ur, ub), clock)
        video = DebugVideo()
        self._overlay_dirty = True
        play = "Caps Lock = play" if self._caps_play() else f"{key_label(self._play_key())} = play / pause"
        self.log(f"Screen {sw}x{sh}, {lanes} lanes, keys {' '.join(key_label(k) for k in keys)}. {play}")

        sched.start()
        cap.start()
        self.running = True
        self.ready = True
        overlay = bool(cfg["debug"].get("overlay_on_start"))
        overlay_open = False
        rec_frames, rec_until = None, 0.0
        fps_t0, fps_n = clock(), 0
        snaps = os.path.join(APP_DIR, "snapshots")
        os.makedirs(snaps, exist_ok=True)
        hot = None
        try:
            while self.running:
                if self._hot_dirty or hot is None:
                    self._hot_dirty = False
                    mapping = {a: k for a, k in cfg["hotkeys"].items() if k and (self.console or a != "exit")}
                    if not self._caps_play():
                        mapping["_play"] = self._play_key()
                    hot = winio.Hotkeys(mapping)
                if self._keys_dirty:
                    self._keys_dirty = False
                    sched.release_all()
                    planner.reset()
                    keys = lane_keys()
                    with self._kb_lock:
                        self._kb = winio.Keyboard(keys)
                if self._overlay_dirty:
                    self._overlay_dirty = False
                    self._set_timer_overlay(cfg["charts"].get("show_overlay", True) and lanes == 5)
                full, t = cap.latest(0.05)
                frame = None if full is None else np.ascontiguousarray(full[ey:ey + geo.h, ex:ex + geo.w])
                fired = hot.poll()
                for action in ([] if self.hotkeys_paused else fired):
                    if action == "_play":
                        self.playing = not self.playing
                    elif action == "exit":
                        self.running = False
                    elif action == "overlay":
                        overlay = not overlay
                    elif action in ("offset_down", "offset_up"):
                        step = 5.0 if winio.Hotkeys.held("shift") else 1.0
                        self.nudge(step if action == "offset_up" else -step)
                    elif action in ("nudge_earlier", "nudge_later"):
                        step = float(cfg["charts"].get("nudge_step_ms", 5.0))
                        self.nudge(step if action == "nudge_later" else -step)
                    elif action in INSTRUMENT_HOTKEYS:
                        self.set_instrument(INSTRUMENT_HOTKEYS[action])
                    elif action == "inst_next":
                        self.next_instrument()
                    elif action == "overdrive_mode":
                        self.set_overdrive_auto(not cfg["charts"].get("use_overdrive", False))
                    elif action == "auto_detect":
                        self.set_auto_detect(not cfg["charts"].get("auto_detect", True))
                    elif action == "song_select":
                        if self.picker is not None:
                            sched.release_all()
                            planner.reset()
                            self.picker()
                        else:
                            self.search_requests += 1
                    elif action == "sync":
                        self.sync_on_go()
                    elif action == "save_config":
                        save_config(cfg)
                        self.log("Saved config.json")
                    elif action == "snapshot" and frame is not None:
                        stamp = time.strftime("%Y%m%d-%H%M%S")
                        cv2.imwrite(os.path.join(snaps, f"{stamp}_raw.png"), frame)
                        cv2.imwrite(os.path.join(snaps, f"{stamp}_overlay.png"), planner.draw(frame, t, keys=keys))
                        self.log(f"Snapshot saved: snapshots/{stamp}_*.png")
                    elif action == "video":
                        if video.active:
                            video.stop(sched.trace, keys)
                            self.log(f"Debug video saved: snapshots/{os.path.basename(video.path)}")
                        else:
                            video.start(os.path.join(snaps, time.strftime("video_%Y%m%d-%H%M%S.mp4")),
                                        float(cfg["debug"].get("video_fps", 60)), clock())
                            self.log("Debug video recording... press again to stop")
                    elif action == "record" and rec_frames is None:
                        rec_frames, rec_until = [], clock() + float(cfg["debug"]["record_seconds"])
                        self.log("Recording...")

                if frame is None:
                    self._tick(clock(), self.active)
                    continue
                fps_n += 1
                if t - fps_t0 >= 1.0:
                    self.fps, fps_n, fps_t0 = fps_n / (t - fps_t0), 0, t

                on = winio.capslock_on() if self._caps_play() else self.playing
                if on != self.active:
                    self.active = on
                    if self.sync is not None and on:
                        self.sync.resume(clock())
                    if self.timer_overlay is not None:
                        self.timer_overlay.set_clickthrough(on)
                    if not on:
                        sched.release_all()
                        planner.reset()
                if self.active:
                    planner.process(frame, t)
                    if sched.presses != getattr(self, "_presses_seen", 0):
                        self._presses_seen = sched.presses
                        self.last_press_t = t
                self._tick(clock(), self.active)

                if rec_frames is not None:
                    rec_frames.append((frame, t))
                    if clock() >= rec_until:
                        _save_recording(rec_frames, cfg, sw, sh)
                        rec_frames = None
                if video.active and video.due(t):
                    img = planner.draw(frame, t, keys=keys,
                                       extra=[f"t={t - video.t0:7.3f}s  {'PLAYING' if self.active else 'paused'}  fps {self.fps:.0f}"])
                    label = cv2.cvtColor(np.ascontiguousarray(full[lab[1]:lab[3], lab[0]:lab[2]]), cv2.COLOR_GRAY2BGR)
                    video.push(img, label, t)
                if overlay:
                    img = planner.draw(frame, t, keys=keys,
                                       extra=[f"{'PLAYING' if self.active else 'paused (caps lock off)'}  capture {self.fps:.0f} fps  "
                                              f"presses {sched.presses}  holds {sched.holds}  lifts {sched.lifts}"])
                    cv2.imshow("Stellite debug overlay", img)
                    if not overlay_open:
                        cv2.moveWindow("Stellite debug overlay", 0, 0)
                        try:
                            cv2.setWindowProperty("Stellite debug overlay", cv2.WND_PROP_TOPMOST, 1)
                        except Exception:
                            pass
                        overlay_open = True
                    cv2.waitKey(1)
                elif overlay_open:
                    cv2.destroyAllWindows()
                    overlay_open = False
                if self.console:
                    self._console_status()
        finally:
            self.running = False
            if video.active:
                video.stop(sched.trace, keys)
                self.log(f"Debug video saved: snapshots/{os.path.basename(video.path)}")
            sched.stop()
            if self.od_release is not None and self.od_kb is not None:
                self.od_kb.send([(0, False)])
            self._set_timer_overlay(False)
            self.active = False
            self.ready = False
            cap.stop()
            winio.teardown_process()
            cv2.destroyAllWindows()
            self.log("Stopped. All keys released.")

    def _send(self, actions):
        with self._kb_lock:
            self._kb.send(actions)

    def _set_timer_overlay(self, on: bool):
        if on and self.timer_overlay is None:
            try:
                import winio
                from overlay import Overlay
                self.timer_overlay = Overlay()
                self.timer_overlay.set_clickthrough(self.active)
            except Exception as e:
                self.log(f"In-game timer overlay unavailable ({e})")
        elif not on and self.timer_overlay is not None:
            try:
                self.timer_overlay.close()
            except Exception:
                pass
            self.timer_overlay = None

    _last_status = 0.0

    def _console_status(self):
        now = clock()
        if now - self._last_status < 0.5:
            return
        self._last_status = now
        s = self.state()
        play = "\033[92mPLAYING\033[0m" if s["active"] else "\033[93mpaused \033[0m"
        song = s["song"]["title"] if s["song"] else "no song"
        sys.stdout.write(f"\r{play} | {song[:22]:22s} {s['chart_status']:8s} {s['timer']:>5s} | "
                         f"{INSTRUMENT_NAMES.get(s['instrument'], s['instrument'])} | OD {'auto' if s['overdrive_auto'] else 'manual'} | "
                         f"{s['fps']:3d} fps | presses {s['presses']:5d} holds {s['holds']:4d} lifts {s['lifts']:4d} | "
                         f"offset {s['offset_ms']:+.0f} ms   ")
        sys.stdout.flush()

    def _tick(self, now, active):
        sync, assist, chart = self.sync, self.assist, self.chart
        if sync is not None:
            if sync.try_lock(now):
                self.log(f"Chart synced: {chart['name']} at {self._clock(sync.song_time(now))}")
            m = sync.check(now, active)
            if m:
                self.log(f"Chart: {m}. The screen keeps playing and the chart lines up again by itself")
            if self.cfg["charts"].get("use_overdrive") and active:
                if self.od_kb is None:
                    import winio
                    self.od_kb = winio.Keyboard([self.cfg["charts"].get("overdrive_key", "space")])
                off = (self.cfg["timing"]["offset_ms"] + self.cfg["charts"].get("overdrive_offset_ms", 0.0)) / 1000.0
                if assist.overdrive_due(now, off) is not None:
                    self.od_kb.send([(0, True)])
                    self.od_release = now + 0.05
                    self.log("Overdrive!")
        if self.od_release is not None and now >= self.od_release:
            self.od_kb.send([(0, False)])
            self.od_release = None
        if self.timer_overlay is not None and now - self.last_overlay > 0.05:
            self.last_overlay = now
            big, small, color, big_color = self._overlay_text(now, active)
            if now < self.msg[1]:
                small, color = self.msg[0], "good"
            self.timer_overlay.show(big, small, color, big_color)

    def _overlay_text(self, now, active):
        chart, sync, assist = self.chart, self.sync, self.assist
        if self.loading:
            return ("-:--", "loading chart", "dim", "dim")
        if sync is None:
            return ("-:--", "looking for the song" if self.cfg["charts"].get("auto_detect", True) else "pick a song",
                    "dim", "dim")
        length = chart["length"]
        if not sync.locked:
            return (self._clock(length) if length else "-:--", "syncing" if active else "paused",
                    "dim", "white" if length else "dim")
        song_t = sync.song_time(now)
        timer = self._clock(length - song_t) if length else "-:--"
        if not active:
            return (timer, "paused", "dim", "white")
        if self.cfg["charts"].get("use_overdrive"):
            labels = chart["overdrive_labels"]
            for i, (press, lo, hi) in enumerate(chart["overdrive"]):
                if 0 <= song_t - press < 0.85:
                    return (timer, f"OD NOW  {labels[i]}", "now", "white")
            up = [(i, p) for i, (p, lo, hi) in enumerate(chart["overdrive"]) if p > song_t]
            if up:
                i, p = up[0]
                at = length - p if length else p - song_t
                return (timer, f"OD {self._clock(at)}  {labels[i]}", "accent", "white")
        ok = assist.healthy()
        return (timer, "chart synced" if ok else "synced (screen leads)", "good" if ok else "accent", "white")
