# Stellite NX - chart assist
# Based on Stellite v2 by Jinx (AGPL-3.0). Chart reading and sync (charts.py) come from Stellite v2,
# which bases them on Festival OD Coach by Aknownthreat (GPL-3.0). Song charts from fnfpaths.
#
# The screen engine stays in charge: it sees every note, lift arrow and trail ~0.2 s before it
# reaches the line. The chart (lined up with what the screen sees) adds what the screen can't
# be sure of:
#   - exact sustain ends for holds the screen sees (overdrive glow, double holds, glow bands)
#   - presses for chart notes the screen somehow didn't pick up, but only while the chart and the
#     screen clearly agree (fnfpaths charts can be outdated in places, e.g. since the lift rework)
#   - the song timer and, if wanted, overdrive activation on the fnfpaths path
# Copyright (C) Jinx (jinxthecat_) and the Stellite contributors. Changes for Stellite NX (C) 2026 Starborne.
# This program is free software: you can redistribute it and/or modify it under the
# terms of the GNU Affero General Public License version 3 (see LICENSE). It comes WITHOUT ANY WARRANTY.

from __future__ import annotations

import numpy as np


class ChartAssist:
    def __init__(self, chart: dict, sync, cfg: dict):
        c = cfg["charts"]
        self.chart = chart
        self.sync = sync
        self.match_tol = c.get("match_tolerance_ms", 35.0) / 1000.0
        self.fill_in = bool(c.get("fill_in", True))
        self.fill_lead = c.get("fill_in_lead_ms", 120.0) / 1000.0
        self.health_needed = float(c.get("fill_in_agreement", 0.9))
        notes = chart["notes"]
        self.lane_t = []
        self.lane_len = []
        self.lane_idx = []
        for lane in range(5):
            idx = [i for i, n in enumerate(notes) if n[1] == lane]
            idx.sort(key=lambda i: notes[i][0])
            self.lane_t.append(np.array([notes[i][0] for i in idx], dtype=np.float64))
            self.lane_len.append(np.array([notes[i][2] for i in idx], dtype=np.float64))
            self.lane_idx.append(idx)
        self.agree = []  # recent screen notes: did they sit on a chart note?
        self.claimed = set()  # chart notes a screen note (or a fill-in) already took
        self.fill_count = 0
        self.overdrive_done = set()
        self._ver = None

    def _fresh(self):
        # the sync jumped (song restarted, paused, re-locked): old claims no longer mean anything
        if self.sync.version != self._ver:
            self._ver = self.sync.version
            self.claimed.clear()
            self.agree.clear()

    # ---- screen -> chart ----
    def sighting(self, t_cross: float, lane: int):
        """A screen note will cross the line at t_cross (no timing offset). Feeds the sync."""
        self.sync.add_sighting(t_cross, lane)
        self._fresh()
        if self.sync.locked:
            j = self._nearest(lane, t_cross)
            # "agrees" means within 20 ms: fill-ins are only trusted while the chart sits that tight
            ok = j is not None and abs(self.sync.wall_time(self.lane_t[lane][j]) - t_cross) <= 0.02
            self.agree.append(ok)
            if len(self.agree) > 24:
                self.agree.pop(0)

    def _nearest(self, lane: int, t_cross: float):
        if not self.sync.locked or lane >= 5:
            return None
        times = self.lane_t[lane]
        if times.size == 0:
            return None
        s = self.sync.song_time(t_cross)
        i = int(np.searchsorted(times, s))
        best = None
        for j in (i - 1, i):
            if 0 <= j < times.size:
                d = abs(self.sync.wall_time(times[j]) - t_cross)
                if d <= self.match_tol and (best is None or d < best[1]):
                    best = (j, d)
        return None if best is None else best[0]

    def match(self, lane: int, t_cross: float):
        """Chart note under a screen note: (chart note id, sustain end wall time or None)."""
        self._fresh()
        j = self._nearest(lane, t_cross)
        if j is None:
            return None, None
        self.claimed.add((lane, j))
        length = self.lane_len[lane][j]
        end = self.sync.wall_time(self.lane_t[lane][j] + length) if length > 0 else None
        return (lane, j), end

    def healthy(self) -> bool:
        return (self.sync.locked and len(self.agree) >= 8
                and sum(self.agree) >= self.health_needed * len(self.agree))

    # ---- chart -> keys ----
    def fill_ins(self, now: float, screen_times: list[list[float]]):
        """Chart notes about to reach the line that no screen note covers.
        screen_times[lane] = predicted crossings of screen notes in that lane."""
        self._fresh()
        if not (self.fill_in and self.healthy()):
            return []
        out = []
        s0 = self.sync.song_time(now)
        s1 = self.sync.song_time(now + self.fill_lead)
        for lane in range(5):
            times = self.lane_t[lane]
            a, b = np.searchsorted(times, [s0 + 0.03, s1])
            for j in range(int(a), int(b)):
                if (lane, j) in self.claimed:
                    continue
                w = self.sync.wall_time(times[j])
                if any(abs(w - st) < 0.06 for st in screen_times[lane]):
                    continue
                self.claimed.add((lane, j))
                self.fill_count += 1
                length = self.lane_len[lane][j]
                end = self.sync.wall_time(times[j] + length) if length > 0 else None
                out.append((lane, j, w, end))
        return out

    def overdrive_due(self, now: float, offset: float):
        """Index of an overdrive activation that is due now (fnfpaths path), or None."""
        if not self.sync.locked:
            return None
        s = self.sync.song_time(now - offset)
        for i, (press, lo, hi) in enumerate(self.chart["overdrive"]):
            if i not in self.overdrive_done and press <= s < press + 0.1:
                self.overdrive_done.add(i)
                return i
        return None

    def reset_song_state(self):
        self.agree.clear()
        self.claimed.clear()
        self.overdrive_done.clear()
