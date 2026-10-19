# Stellite NX - engine (platform independent)
#
# Based on Stellite by Jinx (https://github.com/JinxTheCatto/Stellite), AGPL-3.0.
# Modified 2026-10-17: the detection / timing / input pipeline was rewritten from scratch
# (note tracking, latency compensated scheduling, sustain (hold) notes, replay tooling).
# This file is part of a covered work and is licensed under the GNU AGPL v3, see LICENSE.
#
# Everything in this file is pure Python + numpy + OpenCV so it can be tested off Windows.
# Pipeline per captured frame:
#   gray frame -> NoteDetector (template match, half res) -> Tracker (follows each note
#   down the highway) -> SpeedModel (learns note speed incl. perspective) -> predicted
#   hit time per note -> KeyScheduler (separate thread, presses/releases at exact times)
#   TailScanner looks for the sustain trail above each note head and, while a key is
#   held, follows the trail at the press line to decide exactly when to let go.
# Copyright (C) Jinx (jinxthecat_) and the Stellite contributors. Changes for Stellite NX (C) 2026 Starborne.
# This program is free software: you can redistribute it and/or modify it under the
# terms of the GNU Affero General Public License version 3 (see LICENSE). It comes WITHOUT ANY WARRANTY.

from __future__ import annotations

import math
import threading
import time
from collections import deque

import cv2
import numpy as np

clock = time.perf_counter


# --------------------------------------------------------------------------------------
# Geometry: where the highway is on screen, perspective, lanes
# --------------------------------------------------------------------------------------
class Geometry:
    """All config values are in 1080p reference pixels and get scaled to the real screen.

    The highway is a tilted plane, so lane width shrinks linearly with height:
        width_factor(y) = 1 + taper * (y - press_y)      (== 1 at the press line)
    Note speed follows the same perspective: v(y) = v0 * width_factor(y)^2.
    """

    def __init__(self, cfg: dict, screen_w: int, screen_h: int):
        g = cfg["geometry"]
        self.lanes = int(cfg["lanes"])
        s = screen_h / 1080.0
        self.s = s
        five = self.lanes == 5
        self.lane_w = g["lane_width_5" if five else "lane_width_4"] * s
        cx_screen = screen_w / 2.0 + g["center_offset_5" if five else "center_offset_4"] * s
        press_screen = g["press_line_y"] * s

        top = int(round(press_screen - g["lookahead_px"] * s))
        bottom = int(round(press_screen + g["below_line_px"] * s))
        half = self.lanes * self.lane_w / 2.0 + g["side_margin_px"] * s
        left = int(round(cx_screen - half))
        right = int(round(cx_screen + half))
        top, left = max(0, top), max(0, left)
        bottom, right = min(screen_h, bottom), min(screen_w, right)

        self.region = (left, top, right, bottom)
        self.w = right - left
        self.h = bottom - top
        self.press_y = press_screen - top
        self.cx = cx_screen - left

        self.taper = float(g["taper"]) / s
        self.learn_taper = bool(g.get("learn_taper", True))
        self._taper_samples: deque = deque(maxlen=80)
        self._taper_max = 0.004 / s

    def wf(self, y: float) -> float:
        return max(0.25, 1.0 + self.taper * (y - self.press_y))

    def project_x(self, x: float, y: float) -> float:
        """x position this point will have when it reaches the press line."""
        return self.cx + (x - self.cx) / self.wf(y)

    def unproject_x(self, xp: float, y: float) -> float:
        return self.cx + (xp - self.cx) * self.wf(y)

    def lane_of(self, x: float, y: float):
        xp = self.project_x(x, y)
        i = math.floor((xp - self.cx) / self.lane_w + self.lanes / 2.0)
        return i if 0 <= i < self.lanes else None

    def lane_center(self, lane: int, y: float | None = None) -> float:
        xp = self.cx + (lane - (self.lanes - 1) / 2.0) * self.lane_w
        return xp if y is None else self.unproject_x(xp, y)

    def lane_edges(self, y: float):
        return [self.unproject_x(self.cx + (i - self.lanes / 2.0) * self.lane_w, y) for i in range(self.lanes + 1)]

    def add_taper_sample(self, value: float):
        if not self.learn_taper or not math.isfinite(value):
            return
        self._taper_samples.append(value)
        if len(self._taper_samples) >= 8:
            self.taper = float(np.clip(np.median(self._taper_samples), 0.0, self._taper_max))


# --------------------------------------------------------------------------------------
# Speed model
# --------------------------------------------------------------------------------------
class SpeedModel:
    """Learns v0 (px/s at the press line) from tracked notes; robust median with reset."""

    def __init__(self, geo: Geometry):
        self.geo = geo
        self.samples: deque = deque(maxlen=31)
        self.v0: float | None = None
        self._rejects = 0

    def reset(self):
        self.samples.clear()
        self.v0 = None
        self._rejects = 0

    def add(self, v: float, y: float):
        if not (v > 0 and math.isfinite(v)):
            return
        w = self.geo.wf(y)
        s = v / (w * w)
        if self.v0 is not None and len(self.samples) >= 10 and not (0.6 * self.v0 < s < 1.6 * self.v0):
            self._rejects += 1
            if self._rejects > 20:  # song / scroll speed changed: relearn
                self.samples.clear()
                self._rejects = 0
            else:
                return
        else:
            self._rejects = 0
        self.samples.append(s)
        self.v0 = float(np.median(self.samples))

    def speed(self, y: float):
        if self.v0 is None:
            return None
        w = self.geo.wf(y)
        return self.v0 * w * w

    def travel(self, y0: float, y1: float):
        """Seconds for a note to move from row y0 to row y1 (y1 below y0 -> positive)."""
        if self.v0 is None:
            return None
        t = self.geo.taper
        if abs(t) < 1e-9:
            return (y1 - y0) / self.v0
        return (1.0 / (self.v0 * t)) * (1.0 / self.geo.wf(y0) - 1.0 / self.geo.wf(y1))


# --------------------------------------------------------------------------------------
# Note head detection
# --------------------------------------------------------------------------------------
class Detection:
    __slots__ = ("x", "y", "score", "kind")

    def __init__(self, x, y, score, kind):
        self.x, self.y, self.score, self.kind = x, y, score, kind


class NoteDetector:
    def __init__(self, cfg: dict, templates: dict, screen_h: int = 1080):
        """templates: name -> (gray image at screen resolution, threshold)"""
        d = cfg["detection"]
        # match_scale is relative to 1080p, so 1440p/4K cost the same as 1080p
        self.ms = float(d.get("match_scale", 0.5)) * min(1.0, 1080.0 / screen_h)
        self.items = []
        for name, (img, thr) in templates.items():
            for sc in d.get("template_scales", [1.0]):
                f = self.ms * sc
                t = cv2.resize(img, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
                if t.shape[0] >= 4 and t.shape[1] >= 4:
                    self.items.append((name, t, float(thr)))
        self.lift_min_edge = float(d.get("lift_min_edge", 40.0))
        self.lift_min_std = float(d.get("lift_min_std", 14.0))
        self.lift_names = set(d.get("lift_templates", ["diamond"]))
        lt = next((templates[n][0] for n in self.lift_names if n in templates), None)
        self.lift_h, self.lift_w = lt.shape[:2] if lt is not None else (40, 70)
        ref = templates.get("tile") or next(iter(templates.values()))
        self.head_h, self.head_w = ref[0].shape[:2]
        self.full = {name: img for name, (img, thr) in templates.items()}  # full resolution, for probes
        self.last_ms = 0.0

    def probe(self, gray, x: float, y: float, wf: float):
        """Best note-head and lift-arrow match right around (x, y), no thresholds: used to tell
        what a chart note the normal detector missed actually looks like on screen."""
        head = lift = -1.0
        hw, hh = self.head_w * wf, self.head_h * wf
        x0, x1 = int(max(0, x - 1.1 * hw)), int(min(gray.shape[1], x + 1.1 * hw))
        y0, y1 = int(max(0, y - 1.4 * hh)), int(min(gray.shape[0], y + 1.4 * hh))
        roi = gray[y0:y1, x0:x1]
        for name, img in self.full.items():
            f = wf
            t = cv2.resize(img, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
            if t.shape[0] < 4 or t.shape[1] < 4 or roi.shape[0] < t.shape[0] or roi.shape[1] < t.shape[1]:
                continue
            sc = float(cv2.matchTemplate(roi, t, cv2.TM_CCOEFF_NORMED).max())
            if name in self.lift_names:
                lift = max(lift, sc)
            else:
                head = max(head, sc)
        return head, lift

    def _crisp(self, gray, x, y):
        hw = int(self.lift_w * 0.5)
        hh = int(self.lift_h * 0.5)
        p = gray[max(0, int(y) - hh):int(y) + hh + 1, max(0, int(x) - hw):int(x) + hw + 1]
        if p.size < 50:
            return False
        p = p.astype(np.float32)
        gx = cv2.Sobel(p, cv2.CV_32F, 1, 0)
        gy = cv2.Sobel(p, cv2.CV_32F, 0, 1)
        return float(np.percentile(np.hypot(gx, gy), 95)) >= self.lift_min_edge and float(p.std()) >= self.lift_min_std

    def detect(self, gray: np.ndarray):
        t0 = clock()
        ms = self.ms
        small = cv2.resize(gray, None, fx=ms, fy=ms, interpolation=cv2.INTER_AREA) if ms != 1.0 else gray
        cands = []
        for name, t, thr in self.items:
            th, tw = t.shape
            if small.shape[0] < th or small.shape[1] < tw:
                continue
            r = cv2.matchTemplate(small, t, cv2.TM_CCOEFF_NORMED)
            kx = max(3, int(tw * 0.7) | 1)
            ky = max(3, int(th * 0.7) | 1)
            dil = cv2.dilate(r, cv2.getStructuringElement(cv2.MORPH_RECT, (kx, ky)))
            ys, xs = np.nonzero((r >= thr) & (r >= dil))
            rh = r.shape[0]
            for y, x in zip(ys.tolist(), xs.tolist()):
                b = float(r[y, x])
                dy = 0.0
                if 0 < y < rh - 1:  # sub-pixel refine along the scroll direction
                    a, c = float(r[y - 1, x]), float(r[y + 1, x])
                    den = a - 2.0 * b + c
                    if den < -1e-6:
                        dy = max(-0.5, min(0.5, 0.5 * (a - c) / den))
                cands.append((b, (x + tw / 2.0) / ms, (y + dy + th / 2.0) / ms, name))
        cands.sort(key=lambda c: -c[0])
        out: list[Detection] = []
        gx, gy = 0.5 * self.head_w, 0.5 * self.head_h
        # overlap filtering happens per class, so a lift arrow right behind its note is kept
        for b, x, y, name in cands:
            lift = name in self.lift_names
            same = [o for o in out if (o.kind in self.lift_names) == lift]
            if all(abs(x - o.x) > gx or abs(y - o.y) > gy for o in same):
                out.append(Detection(x, y, b, name))
        # an arrow must have crisp edges: smooth highway lighting gradients can score high too
        out = [o for o in out if o.kind not in self.lift_names or self._crisp(gray, o.x, o.y)]
        # an arrow sitting exactly on a note head is a false match, not a lift
        heads = [o for o in out if o.kind not in self.lift_names]
        # also covers the spot just above a head where it meets its trail (looks arrow-like in overdrive);
        # real arrows sit well behind their note
        out = [o for o in out if o.kind not in self.lift_names or
               all(abs(o.x - h.x) > 0.4 * self.head_w or not (-0.3 * self.head_h < h.y - o.y < 0.9 * self.head_h)
                   for h in heads)]
        self.last_ms = (clock() - t0) * 1000.0
        return out


# --------------------------------------------------------------------------------------
# Sustain trail scanner
# --------------------------------------------------------------------------------------
class TailScanner:
    """Follows a sustain trail upward. A trail row = bright narrow peak near x with dark flanks
    (this rejects beat lines and note heads, which are bright across the whole lane)."""

    def __init__(self, cfg: dict, s: float):
        h = cfg["holds"]
        self.half = max(2, int(round(h["tail_half_width_px"] * s)))
        self.lock = self.half + max(2, int(round(h.get("tail_search_slack_px", 8) * s)))
        self.flank = max(self.half + 3, int(round(h["flank_offset_px"] * s)))
        self.near = self.half + max(3, int(round(4 * s)))  # just outside the trail: must be dark too
        self.fw = max(1, int(round(3 * s)))
        self.bright = float(h["tail_min_brightness"])
        self.contrast = float(h["tail_min_contrast"])
        self.near_contrast = float(h.get("tail_near_contrast", 25.0))
        self.occlude_min = float(h.get("glow_min_brightness", 170.0))  # glow that can hide a trail
        self.max_w = 2 * self.half + max(4, int(round(4 * s)))  # a trail is narrow; heads/beat lines are wide
        self.gap = max(1, int(round(h["max_gap_px"] * s)))

    def _flank(self, gray, r, x):
        w = gray.shape[1]
        a, b = max(0, x - self.fw), min(w, x + self.fw + 1)
        if b <= a:
            return 0.0
        return float(gray[r, a:b].mean())

    def _row(self, gray, r, x, win):
        """Is row r a trail row near x? Returns (ok, trail centre)."""
        w = gray.shape[1]
        xi = int(round(x))
        a, b = max(0, xi - win), min(w, xi + win + 1)
        if b - a < 3:
            return False, x
        row = gray[r, a:b]
        pi = int(row.argmax())
        pk = float(row[pi])
        if pk < self.bright:
            return False, x
        # bright cluster around the peak (includes the dip between the two rails)
        cut = max(0.55 * pk, 0.5 * (pk + float(row.min())))  # relative to the local background
        lo = pi
        while lo > 0 and row[lo - 1] >= cut:
            lo -= 1
        hi = pi
        while hi < len(row) - 1 and row[hi + 1] >= cut:
            hi += 1
        c = a + (lo + hi) / 2.0
        if hi - lo + 1 > self.max_w:
            return False, c
        ci = int(round(c))
        # flanks measured from where the trail actually is, not where we started looking
        near = max(self._flank(gray, r, ci - self.near), self._flank(gray, r, ci + self.near))
        far = max(self._flank(gray, r, ci - self.flank), self._flank(gray, r, ci + self.flank))
        return (pk - near >= self.near_contrast and pk - far >= self.contrast), c

    def _occluded(self, gray, r, x):
        """Row r at x is covered by something bright and wide (glow band, flash): a trail could be
        under it but can't be seen."""
        w = gray.shape[1]
        xi = int(round(x))
        a, b = max(0, xi - self.near), min(w, xi + self.near + 1)
        if b - a < 3:
            return False
        seg = gray[r, a:b]
        lo = float(seg.min())
        return lo >= self.occlude_min and lo >= 0.8 * float(seg.max())

    def covered(self, gray, x, y0, span):
        """Is the stretch just above y0 mostly hidden under a bright glow (so a missing trail
        there means 'can't see it', not 'it ended')?"""
        rows = range(int(y0), max(0, int(y0 - span)), -3)
        n = sum(1 for r in rows if self._occluded(gray, r, x))
        return n >= 0.6 * max(1, len(rows))

    def scan(self, gray: np.ndarray, x0: float, y0: float, skip: int = 0, y_stop: int = 0,
             bridge: int = 0, blockers=(), covered_only: bool = False):
        """Returns (length_px, top_row, reached_region_top, x_at_top).
        skip = rows allowed before the trail is found (to step over a note head).
        bridge = once on a trail, a break up to this long is stepped over when the trail clearly
        carries on above it and no note head / arrow (blockers: (top, bottom) rows) sits in the
        break. Overdrive glow bands and beat flashes cut trails; a real trail end has a note or
        nothing behind it, never more of the same trail."""
        h, w = gray.shape
        x = float(x0)
        r = int(min(h - 1, y0))
        last_good = None
        first_good = None
        good = 0
        bridged = 0
        misses = 0
        allow = max(self.gap, int(skip))
        while r >= y_stop:
            win = self.lock if last_good is None else self.half + 2
            ok, c = self._row(gray, r, x, win)
            if ok:
                x = c if last_good is None else x + max(-1.5, min(1.5, c - x))
                if first_good is None:
                    first_good = r
                last_good = r
                good += 1
                misses = 0
            else:
                misses += 1
                if misses > (allow if last_good is None else self.gap):
                    if last_good is None or bridge <= 0 or good < 4:
                        break
                    lim = max(y_stop, last_good - bridge)
                    if any(bt <= last_good and bb >= lim for bt, bb in blockers):
                        break  # a note head / arrow sits in the break: the trail really ends here
                    cover = 0  # bright band right above the end: the end may be hidden under it
                    # look for the trail carrying on above the break (3 trail rows in a row)
                    run, rr, resume = 0, r - 1, None
                    while rr >= lim:
                        ok2, _ = self._row(gray, rr, x, self.half + 2)
                        run = run + 1 if ok2 else 0
                        if run >= 3:
                            resume = rr + 2
                            break
                        rr -= 1
                    if resume is not None and covered_only:
                        gap_rows = range(last_good - 1, resume, -1)
                        if sum(1 for q in gap_rows if self._occluded(gray, q, x)) < 0.5 * max(1, len(gap_rows)):
                            break  # only step over a break that a bright band visibly covers
                    if resume is None:
                        rr = last_good - 1
                        while rr >= lim and self._occluded(gray, rr, x):
                            cover += 1
                            rr -= 1
                        if cover:
                            last_good -= cover  # let go at the far side of the band (late is safe)
                            bridged += cover
                        break
                    bridged += last_good - resume - 1
                    misses = 0
                    r = resume
                    continue
            r -= 1
        if last_good is None:
            return 0, int(y0), False, x0
        length = first_good - last_good + 1
        if good < 4 or good < 0.6 * (length - bridged):  # a lone bright row is not a trail
            return 0, int(y0), False, x0
        reached = (last_good - y_stop) <= self.gap
        return length, last_good, reached, x


# --------------------------------------------------------------------------------------
# Key scheduler: presses/releases at exact times from its own thread
# --------------------------------------------------------------------------------------
class _Event:
    __slots__ = ("id", "t", "sustain", "x", "done", "t_done", "lift", "link", "tail_end", "chart_end")

    def __init__(self, nid, t, sustain, x, lift=False, link=True, tail_end=None, chart_end=None):
        self.chart_end = chart_end  # release time from the song chart (sustain end), if the chart has it
        self.tail_end = tail_end  # hold: when its trail (as measured on approach) ends, at least
        self.id, self.t, self.sustain, self.x = nid, t, sustain, x
        self.done, self.t_done = False, 0.0
        self.lift = lift  # lift (release) note: the key must be let go exactly at t
        self.link = link  # lift: no other note between it and the held key, so it ends that hold


class _Lane:
    __slots__ = ("down", "holding", "release_at", "pressed_at", "deadline", "x", "events", "sus", "why",
                 "trail_end", "trail_long", "min_end", "pre", "up_at", "chart_end", "screen_end", "tail_min")

    def __init__(self):
        self.why = ""
        self.chart_end = None  # sustain end from the song chart for the note being held
        self.screen_end = None  # where the screen itself last saw the trail end
        self.tail_min = None  # trail length the screen saw on approach (screen evidence only)
        self.up_at = -1.0  # when the key last went up (a re-press needs a real gap the game can see)
        self.min_end = None  # hold: never let go before this (trail length seen on approach)
        self.pre = False  # key went down as a lift pre-press
        self.trail_end = None  # predicted end of the hold trail being played
        self.trail_long = False  # trail currently seen running off the top of the view
        self.sus = False  # current press is a real hold (trail seen)
        self.down = False
        self.holding = False
        self.release_at = None
        self.pressed_at = -1.0
        self.deadline = 0.0
        self.x = 0.0
        self.events: list[_Event] = []


class KeyScheduler:
    """send(actions) receives a list of (lane, is_down) and must emit them as one batch."""

    def __init__(self, lanes: int, send, cfg: dict, threaded: bool = True, log: bool = False):
        tm = cfg["timing"]
        self.n = lanes
        self.send = send
        self.tap = tm["tap_hold_ms"] / 1000.0
        self.gap = tm["min_release_gap_ms"] / 1000.0
        self.min_up = tm.get("min_key_up_ms", 12.0) / 1000.0  # never re-press sooner than this after a release
        self.dedupe = tm["dedupe_ms"] / 1000.0
        self.prepress = tm.get("lift_prepress_ms", 40.0) / 1000.0
        self.linger = tm.get("tap_linger_ms", 1000.0) / 1000.0
        self.lift_min_gap = 0.015
        self.lift_link = cfg["timing"].get("lift_link_ms", 250.0) / 1000.0
        self.hold_timeout = 0.35
        self.lifts = 0
        self.trace: deque = deque(maxlen=200000)  # (time, lane, down, reason) for debug logs
        self.cv = threading.Condition()
        self.lanes = [_Lane() for _ in range(lanes)]
        self.alias: dict[int, _Event] = {}
        self.threaded = threaded
        self.log = [] if log else None
        self.presses = 0
        self.holds = 0
        self._stop = False
        self._thread = None
        self._last_now = -1e9

    # ---- planner side ----
    def plan_press(self, lane: int, nid: int, t: float, sustain: bool, x: float, lift: bool = False,
                   link: bool = True, tail_end=None, chart_end=None):
        """Plan a note press (or, with lift=True, a key release for a lift note)."""
        with self.cv:
            ev = self.alias.get(nid)
            if ev is not None:
                if not ev.done:
                    if ev not in self.lanes[lane].events and not any(ev in s_.events for s_ in self.lanes):
                        self.lanes[lane].events.append(ev)  # its other track was cancelled: still a real note
                    ev.t, ev.sustain, ev.x, ev.link = t, ev.sustain or sustain, x, link
                    if tail_end is not None:
                        ev.tail_end = tail_end
                    if chart_end is not None:
                        ev.chart_end = chart_end
                    for s in self.lanes:  # keep each lane's events in time order
                        if ev in s.events:
                            s.events.sort(key=lambda e: e.t)
                elif sustain and not ev.sustain:
                    ev.sustain = True
                self.cv.notify()
                return
            st = self.lanes[lane]
            if lift:
                # Festival lift arrows always follow a note in the same lane, never closer than ~40 ms
                # (a closer "arrow" is the note head itself lighting up, e.g. in overdrive)
                prior = [e.t for e in st.events if not e.lift and not e.done and e.t < t]
                if st.pressed_at < t and not st.pre:
                    prior.append(st.pressed_at)
                if not prior:
                    return
                last = max(prior)
                held = st.down and st.sus and last == st.pressed_at  # a hold can run long
                if t - last < 0.035 or (t - last > 1.5 and not held):
                    return
            if nid >= 0:
                # the screen found a note the chart already filled in: the screen's version wins
                for e in list(st.events):
                    if e.id < 0 and not e.done and abs(e.t - t) < 0.06:
                        if e.lift == lift:
                            e.id, e.t, e.x, e.link = nid, t, x, link
                            e.sustain = e.sustain or sustain
                            self.alias[nid] = e
                            st.events.sort(key=lambda q: q.t)
                            self.cv.notify()
                            return
                        st.events.remove(e)  # e.g. a guessed lift where the screen sees a note head
            for e in st.events:
                ref = e.t_done if e.done else e.t
                if e.lift == lift and abs(ref - t) < self.dedupe:  # same note seen as a new track -> merge
                    self.alias[nid] = e
                    if not e.done:
                        e.sustain = e.sustain or sustain
                    return
                if lift and not e.lift and abs(ref - t) < self.lift_min_gap:
                    return  # an arrow on top of a note head is not a real lift
            ev = _Event(nid, t, sustain, x, lift, link, tail_end if sustain else None, chart_end)
            st.events.append(ev)
            st.events.sort(key=lambda e: e.t)
            self.alias[nid] = ev
            self.cv.notify()

    def cancel(self, nid: int):
        with self.cv:
            ev = self.alias.pop(nid, None)
            if ev is not None and any(v is ev for v in self.alias.values()):
                return  # another track (same note seen twice) still owns this press
            if ev is not None and not ev.done:
                for st in self.lanes:
                    if ev in st.events:
                        st.events.remove(ev)

    def is_done(self, nid: int) -> bool:
        ev = self.alias.get(nid)
        return ev is not None and ev.done

    def snapshot(self):
        with self.cv:
            # "holding" here means holding for a trail (a key kept down for a lift arrow is not)
            return [(st.down, st.holding and st.sus, st.pressed_at, st.x) for st in self.lanes]

    def upgrade_to_hold(self, lane: int, now: float):
        with self.cv:
            st = self.lanes[lane]
            if st.down and not (st.holding and st.sus):
                st.holding = st.sus = True
                st.min_end = st.chart_end  # the chart knows how long this one is
                st.release_at = None
                st.deadline = now + self.hold_timeout
                self.holds += 1

    def update_hold(self, lane: int, release_at, now: float, long: bool = False):
        with self.cv:
            st = self.lanes[lane]
            if st.down and st.holding:
                if release_at is not None:
                    st.screen_end = release_at
                if st.chart_end is not None and st.sus:
                    # the chart says exactly when this hold ends; the screen only overrides it when it
                    # clearly sees the trail going on well past that point
                    target = st.chart_end
                    if long and target > now + 0.1:
                        long = False  # still a while to go: the trail running off the top is expected
                    use_chart = not (long or (release_at is not None and release_at > target + 0.15))
                    if use_chart:
                        release_at = target
                elif release_at is not None and st.min_end is not None and release_at < st.min_end:
                    release_at = st.min_end  # trail was seen longer than this on approach
                st.trail_end = release_at
                st.trail_long = long
                nxt = next((e for e in st.events if not e.done), None)
                # a pending lift owns the release time only if it sits right at this trail's end
                lift_owns = (nxt is not None and nxt.lift and release_at is not None
                             and abs(nxt.t - release_at) < 0.08)
                if not lift_owns:
                    st.release_at = release_at
                    st.why = "chart-end" if (st.chart_end is not None and release_at == st.chart_end) else "trail-end"
                st.deadline = now + self.hold_timeout
                self.cv.notify()

    def release_all(self):
        with self.cv:
            acts = []
            for i, st in enumerate(self.lanes):
                if st.down:
                    acts.append((i, False, "pause"))
                st.down = st.holding = False
                st.release_at = None
                st.events.clear()
            self.alias.clear()
        if acts:
            self._emit(acts, clock())

    # ---- executor ----
    def _emit(self, acts, now):
        for lane, down, why in acts:
            if self.log is not None:
                self.log.append((now, lane, down))
            self.trace.append((now, lane, down, why))
        self.send([(lane, down) for lane, down, _ in acts])

    def step(self, now: float):
        """Fire everything due at `now`. Returns time of the next thing to do (or None)."""
        now = max(now, self._last_now)  # never step back in time
        self._last_now = now
        acts = []
        wake = None
        with self.cv:
            for i, st in enumerate(self.lanes):
                if st.down and st.holding and st.release_at is None and now >= st.deadline:
                    st.release_at = now  # planner stopped updating (capture stalled)
                    st.why = "stall"
                if st.down and st.release_at is not None and now >= st.release_at:
                    acts.append((i, False, st.why))
                    st.down = st.holding = False
                    st.release_at = None
                    st.up_at = now
                due = [e for e in st.events if not e.done and e.t <= now]
                pressed_now = False
                for e in due:
                    if not e.lift:
                        if st.down and not pressed_now:
                            # key still down when the next note is due: let go now, press after a real gap
                            acts.append((i, False, "repress"))
                            st.down = st.holding = False
                            st.release_at = None
                            st.up_at = now
                        if now - st.up_at < self.min_up - 1e-6:
                            defer = st.up_at + self.min_up  # the game would never see a shorter key-up
                            wake = defer if wake is None else min(wake, defer)
                            break
                    e.done, e.t_done = True, now
                    if e.lift:
                        # only what the screen sees can overrule an arrow (charts can be outdated)
                        screen_says_here = st.screen_end is not None and abs(st.screen_end - e.t) < 0.08
                        if st.down and st.sus and not screen_says_here and (
                                st.trail_long or
                                (st.screen_end is not None and st.screen_end - e.t > 0.08) or
                                (st.tail_min is not None and st.tail_min - e.t > 0.08)):
                            continue  # the trail visibly goes on past this arrow: not this hold's end
                        if st.down and not st.pre and e.t - st.pressed_at < 0.035:
                            continue  # "arrow" right on the note just pressed: its own head, not a lift
                        self.lifts += 1
                        if st.down and not pressed_now:
                            acts.append((i, False, "lift"))  # the lift: let go exactly now
                            st.down = st.holding = False
                            st.release_at = None
                            st.up_at = now
                        elif not st.down:
                            # arrow came too late to pre-press: short press so the release still lands
                            acts.append((i, True, "lift-late-press"))
                            st.down, st.pressed_at, st.holding = True, now, False
                            st.pre, st.min_end = True, None
                            st.release_at = now + 0.012
                            st.why = "lift-late"
                            pressed_now = True
                        continue
                    if pressed_now:
                        continue
                    acts.append((i, True, "hold" if e.sustain else "note"))
                    pressed_now = True
                    st.down, st.pressed_at, st.x = True, now, e.x
                    st.holding = st.sus = e.sustain
                    st.trail_end = None
                    st.trail_long = False
                    st.pre = False
                    st.screen_end = None
                    st.tail_min = e.tail_end if e.sustain else None
                    st.chart_end = e.chart_end
                    st.min_end = (e.chart_end or e.tail_end) if e.sustain else None
                    # taps linger (released early before the next note in this lane) so a lift
                    # arrow that only comes into view later still finds the key held
                    st.release_at = None if e.sustain else now + max(self.tap, self.linger)
                    st.why = "hold-end" if e.sustain else "linger-end"
                    if e.sustain and e.chart_end is not None:
                        st.release_at, st.why = e.chart_end, "chart-end"  # the trail monitor may still extend it
                    st.deadline = now + self.hold_timeout
                    self.presses += 1
                    self.holds += 1 if e.sustain else 0
                # forget old finished events
                if st.events and st.events[0].done and now - st.events[0].t_done > 0.6:
                    st.events = [e for e in st.events if not (e.done and now - e.t_done > 0.6)]
                    live = {id(e) for s in self.lanes for e in s.events}
                    self.alias = {k: v for k, v in self.alias.items() if id(v) in live}
                nxt_ev = next((e for e in st.events if not e.done), None)
                nxt = nxt_ev.t if nxt_ev is not None else None
                if nxt is not None and nxt <= now and not nxt_ev.lift:
                    nxt = max(nxt, st.up_at + self.min_up)  # held back for the key-up gap: wake then
                pre = None
                if nxt_ev is not None and nxt_ev.lift:
                    linked = nxt_ev.link  # planner: no other note between this arrow and the line
                    if st.sus and (st.trail_long or (st.screen_end is not None and abs(nxt_ev.t - st.screen_end) > 0.08)
                                   or (st.tail_min is not None and st.tail_min - nxt_ev.t > 0.08)):
                        linked = False  # the trail says this hold ends elsewhere
                    if st.down and not linked:
                        pass  # arrow belongs to a later note: let this tap release normally
                    elif st.down:
                        # hold the note until its lift arrow, release exactly on it
                        st.holding = True
                        st.release_at = None
                        st.deadline = max(st.deadline, nxt + 0.05)
                        nxt = nxt_ev.t
                    elif now >= nxt - self.prepress and not st.down and now - st.up_at >= self.min_up - 1e-6:
                        # lift with nothing held (its note was already let go): press just before it
                        acts.append((i, True, "lift-prepress"))
                        st.down, st.pressed_at, st.holding, st.sus = True, now, True, False
                        st.pre, st.min_end = True, None
                        st.release_at = None
                        st.deadline = nxt + 0.05
                        self.presses += 1
                    else:
                        pre = max(nxt - self.prepress, st.up_at + self.min_up)
                elif st.down and nxt is not None:
                    lim = nxt - self.gap
                    if st.release_at is None or st.release_at > lim:
                        st.release_at = lim
                        st.why = "before-next"
                for c in (nxt, pre, st.release_at, st.deadline if (st.down and st.holding) else None):
                    if c is not None and (wake is None or c < wake):
                        wake = c
        if acts:
            self._emit(acts, now)
        return wake

    def start(self):
        if self.threaded:
            self._thread = threading.Thread(target=self._run, name="key-scheduler", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop = True
        with self.cv:
            self.cv.notify_all()
        if self._thread:
            self._thread.join(timeout=1)
        self.release_all()

    def _run(self):
        while not self._stop:
            wake = self.step(clock())
            now = clock()
            if wake is None:
                with self.cv:
                    self.cv.wait(0.05)
                continue
            remaining = wake - now
            if remaining > 0.0025:
                with self.cv:
                    self.cv.wait(remaining - 0.002)  # coarse sleep, woken early on new plans
            else:
                while clock() < wake and not self._stop:  # fine spin for the last ~2 ms
                    time.sleep(0)


# --------------------------------------------------------------------------------------
# Tracking + planning
# --------------------------------------------------------------------------------------
class Track:
    __slots__ = ("id", "obs", "hits", "lane", "sustain", "tail_px", "t_press", "last_seen", "kind", "score",
                 "planned", "taper_done", "speed_n", "lift", "smax", "tail_ends", "ests", "sighted", "raw")

    def __init__(self, nid, t, d: Detection, lift: bool = False):
        self.lift = lift
        self.id = nid
        self.obs: deque = deque(maxlen=48)
        self.obs.append((t, d.x, d.y))
        self.hits = 1
        self.lane = None
        self.sustain = False
        self.tail_px = 0.0
        self.t_press = None
        self.last_seen = t
        self.kind = d.kind
        self.score = d.score
        self.smax = d.score
        self.planned = False
        self.taper_done = False
        self.speed_n = 0
        self.tail_ends: deque = deque(maxlen=5)  # predicted trail-end times from the approach scans
        self.ests: deque = deque(maxlen=7)  # predicted press times from the last few frames
        self.sighted = False  # already reported to the chart sync
        self.raw = None  # predicted line crossing without any timing offset

    @property
    def x(self):
        return self.obs[-1][1]

    @property
    def y(self):
        return self.obs[-1][2]

    def velocity(self, n=6):
        if len(self.obs) < 2:
            return None, None
        o = list(self.obs)[-n:]
        ts = np.array([a[0] for a in o])
        if ts[-1] - ts[0] < 1e-3:
            return None, None
        ts = ts - ts.mean()
        den = float((ts * ts).sum())
        vx = float((ts * np.array([a[1] for a in o])).sum() / den)
        vy = float((ts * np.array([a[2] for a in o])).sum() / den)
        return vx, vy


class Planner:
    def __init__(self, cfg: dict, geo: Geometry, detector: NoteDetector, sched: KeyScheduler):
        self.cfg = cfg
        self.geo = geo
        self.det = detector
        self.sched = sched
        self.speed = SpeedModel(geo)
        s = geo.s
        h = cfg["holds"]
        self.holds_on = bool(h.get("enabled", True))
        self.tails = TailScanner(cfg, s)
        self.min_tail = h["min_tail_px"] * s
        self.line_margin = h["press_line_margin_px"] * s
        self.bridge = int(round(h.get("bridge_px", 40) * s))
        self.min_end_slack = h.get("min_end_slack_ms", 30) / 1000.0
        self.release_late = h.get("release_late_ms", 8) / 1000.0
        self.offset = cfg["timing"]["offset_ms"] / 1000.0
        self.lift_offset = cfg["timing"].get("lift_offset_ms", 0.0) / 1000.0
        self.lift_strong = float(cfg["detection"].get("lift_strong", 0.84))
        self.lift_lead = float(cfg["detection"].get("lift_lead_px", 95.0)) * geo.s
        self.min_hits = int(cfg["detection"].get("min_hits", 2))
        self.tracks: dict[int, Track] = {}
        self._next_id = 1
        self.frame_dt = 1 / 240.0
        self._last_t = None
        self._rel_hist = [deque(maxlen=3) for _ in range(geo.lanes)]
        self._hold_miss = [0] * geo.lanes
        self._upgrade_votes = [0] * geo.lanes
        self._floor = [0.0] * geo.lanes  # hold at least until here (trail ran past the top of view)
        self.debug_tails = {}
        self.last_dets = []
        self.assist = None  # chart assist (chartassist.ChartAssist) when a song chart is loaded
        self._fill_id = -1

    def reset(self):
        for tr in self.tracks.values():
            self.sched.cancel(tr.id)
        self.tracks.clear()
        for d in self._rel_hist:
            d.clear()
        self._hold_miss = [0] * self.geo.lanes
        self._last_t = None

    # ---------------------------------------------------------------
    def process(self, gray: np.ndarray, t: float):
        if self._last_t is not None:
            dt = t - self._last_t
            if 0 < dt < 0.1:
                self.frame_dt += 0.05 * (dt - self.frame_dt)
        self._last_t = t
        dets = self.det.detect(gray)
        self.last_dets = dets
        self._associate(dets, t)
        self._learn(t)
        self._plan(gray, t)
        if self.assist is not None:
            self._fill_from_chart(t, gray)
        if self.holds_on:
            self._holds(gray, t)
        self._cleanup(t)

    def _where(self, dt: float):
        """Screen row a note is on when it is dt seconds from the line."""
        v0, tp, py = self.speed.v0, self.geo.taper, self.geo.press_y
        if v0 is None:
            return None
        if abs(tp) < 1e-9:
            return py - v0 * dt
        wf = 1.0 / (1.0 + dt * v0 * tp)
        return py + (wf - 1.0) / tp

    def _fill_from_chart(self, t, gray=None):
        """Chart notes about to arrive that no screen note covers (only while chart and screen agree)."""
        screen = [[] for _ in range(5)]
        for tr in self.tracks.values():
            if tr.lane is not None and tr.raw is not None and tr.lane < 5:
                screen[tr.lane].append(tr.raw)
        last_press = [-1e9] * 5
        for lane, (down, holding, pressed_at, x) in enumerate(self.sched.snapshot()):
            if lane < 5:
                last_press[lane] = pressed_at - self.offset
                if pressed_at > t - 0.2:
                    screen[lane].append(pressed_at - self.offset)
        for lane, j, w, end in self.assist.fill_ins(t, screen):
            chart_end = None if end is None else end + self.offset + self.release_late
            x = self.geo.lane_center(lane)
            lift_like = any(w - 1.5 < s_ < w - 0.035 for s_ in screen[lane] + [last_press[lane]])
            y = self._where(w - t)
            if gray is not None and y is not None and 0 <= y < gray.shape[0]:
                # look at the exact spot the chart says the note is: head or arrow?
                wf = self.geo.wf(y)
                head, arrow = self.det.probe(gray, self.geo.cx + (self.geo.lane_center(lane) - self.geo.cx) * wf, y, wf)
                if max(head, arrow) >= 0.45:  # something is actually there: go by what it looks like
                    if head > arrow + 0.05:
                        lift_like = False
                    elif arrow > head + 0.05:
                        lift_like = True
            if lift_like:
                # right after a note the screen did see in this lane, and it doesn't look like a note
                # head: a chart note the screen missed here is a lift arrow. Let go exactly on it
                self.sched.plan_press(lane, self._fill_id, w + self.offset + self.lift_offset, False, x, lift=True)
            else:
                self.sched.plan_press(lane, self._fill_id, w + self.offset, end is not None, x, chart_end=chart_end)
            self._fill_id -= 1

    # ---------------------------------------------------------------
    def _associate(self, dets, t):
        geo = self.geo
        hw, hh = self.det.head_w, self.det.head_h
        pairs = []
        for tr in self.tracks.values():
            dt = t - tr.obs[-1][0]
            v = self.speed.speed(tr.y)
            vx, vy = tr.velocity()
            if v is None:
                v = vy if (vy is not None and vy > 0) else None
            w = geo.wf(tr.y)
            gx = 0.35 * hw * w
            if v is not None:
                y_pred = tr.y + v * dt
                lo, hi = -0.4 * hh * w, 0.4 * hh * w
            else:
                y_pred = tr.y
                lo, hi = -3.0, 1.6 * hh
            x_pred = tr.x + (vx or 0.0) * dt if v is not None else tr.x
            for j, d in enumerate(dets):
                if (d.kind in self.det.lift_names) != tr.lift:
                    continue  # notes only continue notes, arrows only continue arrows
                dx = d.x - x_pred
                dy = d.y - y_pred
                if abs(dx) <= gx and lo <= dy <= hi:
                    pairs.append(((dx / gx) ** 2 + (dy / max(hi, 1.0)) ** 2, tr.id, j))
        pairs.sort()
        used_t, used_d = set(), set()
        for _, tid, j in pairs:
            if tid in used_t or j in used_d:
                continue
            used_t.add(tid)
            used_d.add(j)
            tr = self.tracks[tid]
            d = dets[j]
            tr.obs.append((t, d.x, d.y))
            tr.hits += 1
            tr.last_seen = t
            tr.kind, tr.score = d.kind, d.score
            tr.smax = max(tr.smax, d.score)
        limit = geo.press_y + 0.25 * hh
        for j, d in enumerate(dets):
            if j in used_d or d.y > limit:
                continue
            if geo.lane_of(d.x, d.y) is None:
                continue
            tr = Track(self._next_id, t, d, lift=d.kind in self.det.lift_names)
            self._next_id += 1
            self.tracks[tr.id] = tr

    def _learn(self, t):
        geo = self.geo
        for tr in self.tracks.values():
            if tr.last_seen != t or tr.hits < 4:
                continue
            o = list(tr.obs)
            span_y = o[-1][2] - o[0][2]
            if span_y < 0.3 * self.det.head_h:
                continue
            if tr.hits - tr.speed_n >= 3:
                tr.speed_n = tr.hits
                vx, vy = tr.velocity(8)
                if vy is not None and vy > 0:
                    ym = float(np.mean([a[2] for a in o[-8:]]))
                    self.speed.add(vy, ym)
            if not tr.taper_done and span_y > 60 * geo.s and len(o) >= 6:
                ys = np.array([a[2] for a in o])
                xs = np.array([a[1] for a in o])
                b, a = np.polyfit(ys, xs, 1)
                u = a + b * geo.press_y - geo.cx
                if abs(u) > 0.9 * geo.lane_w:
                    geo.add_taper_sample(b / u)
                    tr.taper_done = True

    def _plan(self, gray, t):
        geo = self.geo
        hh = self.det.head_h
        for tr in self.tracks.values():
            if tr.last_seen != t:
                continue
            x, y = tr.x, tr.y
            lane = geo.lane_of(x, y)
            if lane is None:
                continue
            tr.lane = lane
            w = geo.wf(y)
            # sustain trail check (trail sits directly above the head)
            if self.holds_on and not tr.lift and y <= geo.press_y + 0.3 * hh and not self.sched.is_done(tr.id):
                y0 = y - 0.5 * hh * w - 1
                if y0 > 2:
                    blk = [(o.y - 0.6 * hh * geo.wf(o.y), o.y + 0.6 * hh * geo.wf(o.y)) for o in self.tracks.values()
                           if o is not tr and o.lane == lane and o.y < y and t - o.last_seen < 0.05]
                    L, top, reached, _ = self.tails.scan(gray, x, y0, skip=int(0.3 * hh * w), bridge=self.bridge,
                                                         blockers=blk, covered_only=True)
                    tr.tail_px = max(tr.tail_px, L)
                    if L >= self.min_tail * w:
                        tr.sustain = True
                        te = self.speed.travel(top, geo.press_y)
                        if te is not None:
                            tr.tail_ends.append(t + te + self.offset)
            if self.sched.is_done(tr.id):
                continue
            travel = self.speed.travel(y, geo.press_y)
            if travel is None:
                if y < geo.press_y:
                    continue
                est = t
            else:
                est = t + travel + self.offset + (self.lift_offset if tr.lift else 0.0)
            # median of the last few frames' estimates: one bad frame (template switch, hit sparkle,
            # overdrive glow) can't yank the press time around near the line
            tr.ests.append(est)
            tr.t_press = float(np.median(tr.ests))
            tr.raw = tr.t_press - self.offset - (self.lift_offset if tr.lift else 0.0)
            moved = tr.obs[-1][2] - tr.obs[0][2]
            if tr.lift and tr.smax < self.lift_strong and tr.obs[0][2] > geo.press_y - self.lift_lead:
                continue  # weak arrow match that only appeared near the line: hit sparkles, not an arrow
            if (self.assist is not None and not tr.sighted and len(tr.ests) >= 4 and tr.hits >= self.min_hits
                    and moved >= 2.0 and tr.raw - t < 0.3):
                tr.sighted = True
                self.assist.sighting(tr.raw, lane)  # keeps the chart lined up with the screen
            if tr.hits >= self.min_hits and moved >= 2.0 and tr.t_press > t - 0.07:
                xp = geo.project_x(x, y)
                lc = geo.lane_center(lane)
                if abs(xp - lc) > 0.3 * geo.lane_w:
                    xp = lc
                link = True
                if tr.lift:  # does another (not yet played) note sit between this arrow and the line?
                    for o in self.tracks.values():
                        if (not o.lift and o.lane == lane and o.y > y and t - o.last_seen < 0.06
                                and not self.sched.is_done(o.id)):
                            link = False
                            break
                tail_end = None
                if tr.sustain and len(tr.tail_ends) >= 2:
                    # hold at least this long: the trail was seen this far on approach (median of the
                    # last few looks, minus a little slack, so one odd frame can't stretch it)
                    tail_end = float(np.median(tr.tail_ends)) - self.min_end_slack
                chart_end = None
                if self.assist is not None:
                    _, end = self.assist.match(lane, tr.raw)  # lifts claim their chart note too
                    if end is not None and not tr.lift:
                        chart_end = end + self.offset + self.release_late
                self.sched.plan_press(lane, tr.id, tr.t_press, tr.sustain, xp, lift=tr.lift, link=link,
                                      tail_end=tail_end, chart_end=chart_end)
                tr.planned = True

    def _holds(self, gray, t):
        geo = self.geo
        hh = self.det.head_h
        snap = self.sched.snapshot()
        self.debug_tails = {}
        y0 = geo.press_y - self.line_margin
        for lane, (down, holding, pressed_at, x) in enumerate(snap):
            if not down:
                self._rel_hist[lane].clear()
                self._hold_miss[lane] = 0
                self._upgrade_votes[lane] = 0
                self._floor[lane] = 0.0
                continue
            fresh = t - pressed_at < max(self.sched.tap, 0.07, 3.5 * self.frame_dt)
            # just pressed: step over the note head; key still down from a tap: the trail must
            # already be crossing the line (a later note's trail sits behind its own head)
            skip = int(1.3 * hh) if (holding or fresh) else self.tails.gap
            # note heads / arrows coming down this lane: a trail break at one of them is a real end
            blockers = []
            for o in self.tracks.values():
                if o.lane == lane and t - o.last_seen < 0.05 and o.y < y0:
                    oh = 0.6 * hh * geo.wf(o.y)
                    blockers.append((o.y - oh, o.y + oh))
            # bridging only while playing a real hold: a lingering tap must show a clean trail to upgrade
            L, top, reached, _ = self.tails.scan(gray, x, y0, skip=skip, bridge=self.bridge if holding else 0,
                                                 blockers=blockers)
            self.debug_tails[lane] = (x, y0, top if L else None, reached)
            # a few bright pixels (note rim, hit sparkle) are not a trail end: need a real stretch of trail
            if L >= self.min_tail * 0.6:
                if not holding:
                    # a tap whose trail was missed on approach: needs 2 frames in a row to upgrade
                    self._upgrade_votes[lane] = self._upgrade_votes[lane] + 1 if L >= self.min_tail else 0
                    if self._upgrade_votes[lane] < 2:
                        continue
                    self.sched.upgrade_to_hold(lane, t)
                self._hold_miss[lane] = 0
                if reached:
                    self._rel_hist[lane].clear()
                    tr0 = self.speed.travel(0.0, geo.press_y)
                    if tr0 is not None:
                        self._floor[lane] = max(self._floor[lane], t + tr0 + self.offset)
                    self.sched.update_hold(lane, None, t, long=True)
                else:
                    tr_t = self.speed.travel(top, geo.press_y)
                    if tr_t is None:
                        tr_t = 0.0
                    rel = t + tr_t + self.offset + self.release_late
                    hist = self._rel_hist[lane]
                    hist.append(rel)
                    # trust a trail end only once two frames agree on it
                    self.sched.update_hold(lane, float(np.median(hist)) if len(hist) >= 2 else None, t)
            elif holding:
                if t - pressed_at < 0.12:
                    self.sched.update_hold(lane, None, t)  # head still covering the line
                    continue
                hist = self._rel_hist[lane]
                if len(hist) >= 2 and float(np.median(hist)) > t - 0.01:
                    # trail end is inside the margin above the line: trust the last prediction
                    self.sched.update_hold(lane, float(np.median(hist)), t)
                    continue
                if len(hist) < 2 and t < self._floor[lane]:
                    self.sched.update_hold(lane, None, t, long=True)  # trail is longer than this; just lost near the line
                    continue
                if self.tails.covered(gray, x, y0, self.bridge):
                    self._hold_miss[lane] = 0
                    self.sched.update_hold(lane, None, t)  # trail hidden under glow: keep holding
                    continue
                self._hold_miss[lane] += 1
                if self._hold_miss[lane] >= 2:
                    self.sched.update_hold(lane, t, t)
                else:
                    self.sched.update_hold(lane, None, t)

    def _cleanup(self, t):
        dead = []
        for tid, tr in self.tracks.items():
            unseen = t - tr.last_seen
            if unseen > 0.08:
                near = tr.t_press is not None and tr.t_press - t < 0.08
                if not near and not self.sched.is_done(tid):
                    self.sched.cancel(tid)
                dead.append(tid)
            elif tr.t_press is not None and t > tr.t_press + 0.25:
                dead.append(tid)
        for tid in dead:
            del self.tracks[tid]

    # ---------------------------------------------------------------
    def draw(self, gray: np.ndarray, t: float, extra: list[str] | None = None, keys=None):
        geo = self.geo
        img = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        H = img.shape[0]
        ys = np.linspace(0, H - 1, 12)
        for i in range(geo.lanes + 1):
            pts = np.array([[geo.lane_edges(y)[i], y] for y in ys], np.int32)
            cv2.polylines(img, [pts], False, (90, 90, 90), 1)
        py = int(round(geo.press_y))
        cv2.line(img, (0, py), (img.shape[1], py), (0, 255, 0), 1)
        hw, hh = self.det.head_w, self.det.head_h
        for d in self.last_dets:
            w = geo.wf(d.y)
            cv2.rectangle(img, (int(d.x - hw * w / 2), int(d.y - hh * w / 2)),
                          (int(d.x + hw * w / 2), int(d.y + hh * w / 2)), (80, 80, 80), 1)
        for tr in self.tracks.values():
            if tr.last_seen != t:
                continue
            w = geo.wf(tr.y)
            col = (80, 80, 255) if tr.lift else ((255, 255, 0) if tr.sustain else (0, 220, 255))
            p1 = (int(tr.x - hw * w / 2), int(tr.y - hh * w / 2))
            p2 = (int(tr.x + hw * w / 2), int(tr.y + hh * w / 2))
            cv2.rectangle(img, p1, p2, col, 2 if tr.planned else 1)
            ms = "" if tr.t_press is None else f"{(tr.t_press - t) * 1000:.0f}ms"
            cv2.putText(img, f"{tr.kind[:2]} {tr.score:.2f} {ms}", (p1[0], max(10, p1[1] - 3)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.38, col, 1, cv2.LINE_AA)
        for lane, (x, y0, top, reached) in self.debug_tails.items():
            if top is not None:
                cv2.line(img, (int(x), int(y0)), (int(x), int(top)), (255, 0, 255), 3)
        snap = self.sched.snapshot()
        bw = img.shape[1] / geo.lanes
        for i, (down, holding, _, _) in enumerate(snap):
            col = (0, 140, 255) if holding else ((0, 255, 0) if down else (60, 60, 60))
            cv2.rectangle(img, (int(i * bw + 4), H - 14), (int((i + 1) * bw - 4), H - 2), col, -1)
            if keys:
                cv2.putText(img, keys[i].upper(), (int(i * bw + bw / 2 - 5), H - 4), cv2.FONT_HERSHEY_SIMPLEX,
                            0.4, (0, 0, 0), 1, cv2.LINE_AA)
        v0 = "--" if self.speed.v0 is None else f"{self.speed.v0:.0f}"
        lines = [f"speed {v0}px/s  taper {geo.taper * geo.s * 1000:.2f}e-3  det {self.det.last_ms:.1f}ms  "
                 f"fps {1 / max(self.frame_dt, 1e-4):.0f}  offset {self.offset * 1000:+.0f}ms"]
        lines += extra or []
        for k, s in enumerate(lines):
            cv2.putText(img, s, (6, 14 + 15 * k), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 0, 0), 3, cv2.LINE_AA)
            cv2.putText(img, s, (6, 14 + 15 * k), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (255, 255, 255), 1, cv2.LINE_AA)
        return img


# --------------------------------------------------------------------------------------
# Templates
# --------------------------------------------------------------------------------------
def load_templates(cfg: dict, assets_dir: str, screen_h: int, log=print):
    """Loads match images for this resolution. If there is no folder for this height,
    the closest folder is scaled automatically (so any resolution works)."""
    import os

    folders = []
    for name in os.listdir(assets_dir):
        if name.isdigit() and os.path.isdir(os.path.join(assets_dir, name)):
            folders.append(int(name))
    if not folders:
        raise FileNotFoundError("No match image folders (like assets/1080) found")
    exact = screen_h in folders
    src = screen_h if exact else min(folders, key=lambda f: (abs(f - screen_h), -f))
    scale = screen_h / src
    if not exact:
        log(f"No match images for {screen_h}p, scaling the {src}p set by {scale:.3f}")
    out = {}
    for name, thr in cfg["detection"]["templates"].items():
        if not thr:
            continue
        path = os.path.join(assets_dir, str(src), f"{name}.png")
        if not os.path.exists(path):
            # fall back to any folder that has it
            for f in sorted(folders, key=lambda f: abs(f - screen_h)):
                p = os.path.join(assets_dir, str(f), f"{name}.png")
                if os.path.exists(p):
                    path, sc = p, screen_h / f
                    break
            else:
                log(f"  (skipping '{name}': no image)")
                continue
        else:
            sc = scale
        img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            continue
        if abs(sc - 1.0) > 1e-3:
            img = cv2.resize(img, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA if sc < 1 else cv2.INTER_CUBIC)
        out[name] = (img, float(thr))
    if not out:
        raise FileNotFoundError("No usable match images were loaded")
    return out
