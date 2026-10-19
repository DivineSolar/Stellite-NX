# Stellite NX - automatic song detection
# Part of Stellite NX, a modified version of Stellite by Jinx (https://github.com/JinxTheCatto/Stellite).
# Added 2026-10-18 (not part of the original Stellite).
#
# Reads the song title off the screen (in song, Up Next, playlists, the results screen) with the OCR
# built into Windows (through the small winocr package) and finds it in the fnfpaths song list, so the
# right chart is loaded without typing the name.
# Copyright (C) 2026 Starborne. Part of a work based on Stellite by Jinx; free software under
# the terms of the GNU Affero General Public License version 3 (see LICENSE). It comes WITHOUT ANY WARRANTY.

import re
import time
import threading
import unicodedata
from difflib import SequenceMatcher

import numpy as np

ANCHORS = ("up next", "next up", "now playing", "playing next", "next song", "currently playing")


def normalize(text):
    text = unicodedata.normalize("NFKD", str(text).lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("&", " and ")
    text = re.sub(r"\(.*?\)|\[.*?\]", " ", text)  # (feat. ...), (Live), [Remix] are often cut off on screen
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def similar(a, b, floor = 0.0):
    if a == b:
        return 1.0
    m = SequenceMatcher(None, a, b)
    # cheap upper bounds first: thousands of songs get checked against every line of text
    if m.real_quick_ratio() < floor or m.quick_ratio() < floor:
        return 0.0
    return m.ratio()


def grams(text):
    padded = f" {text} "
    return {padded[i:i + 3] for i in range(len(padded) - 2)}


class SongMatcher:
    """Finds songs in lines of screen text. Indexed by letter triples, so a whole screen takes a few ms
    instead of comparing every line with every song."""

    def __init__(self, songs):
        self.entries = []
        for song in songs:
            name = song["name"]
            title, artist = (name.rsplit(" by ", 1) + [""])[:2] if " by " in name else (name, "")
            self.entries.append((song, normalize(title), normalize(artist), normalize(name)))
        # "(Molten Mix)", "(Live)": dropped when matching, used to tell versions of a song apart
        self.extra = []
        for song, *_ in self.entries:
            title = song["name"].rsplit(" by ", 1)[0]
            self.extra.append(normalize(" ".join(a or b for a, b in re.findall(r"\((.*?)\)|\[(.*?)\]", title))))
        self.by_title, self.by_full = {}, {}
        self.title_index, self.full_index = {}, {}
        self.title_grams, self.full_grams = [], []
        for i, (song, title, artist, full) in enumerate(self.entries):
            self.by_title.setdefault(title, []).append(i)
            self.by_full.setdefault(full, []).append(i)
            tg, fg = grams(title), grams(full)
            self.title_grams.append(len(tg))
            self.full_grams.append(len(fg))
            for g in tg:
                self.title_index.setdefault(g, []).append(i)
            for g in fg:
                self.full_index.setdefault(g, []).append(i)

    def _fuzzy(self, text, index, sizes, keep = 6):
        """Songs whose title (or full name) shares most letter triples with text: [(i, dice)]."""
        tg = grams(text)
        counts = {}
        for g in tg:
            for i in index.get(g, ()):
                counts[i] = counts.get(i, 0) + 1
        n = len(tg)
        scored = [(i, 2.0 * c / (n + sizes[i])) for i, c in counts.items()]
        scored = [x for x in scored if x[1] >= 0.45]
        scored.sort(key=lambda x: -x[1])
        return scored[:keep]

    def _candidates(self, text):
        out = {}
        for i in self.by_full.get(text, ()):
            out[i] = 3.0
        for i in self.by_title.get(text, ()):
            out.setdefault(i, 1.0)
        if len(text) >= 8:
            for i, _ in self._fuzzy(text, self.full_index, self.full_grams):
                full = self.entries[i][3]
                if len(full) >= 8 and similar(text, full, 0.9) >= 0.9:
                    out[i] = 3.0
        if len(text) >= 4:  # an OCR slip can make a 6 letter title look 5 long
            for i, _ in self._fuzzy(text, self.title_index, self.title_grams):
                title = self.entries[i][1]
                if len(title) > 5 and i not in out:
                    sc = similar(text, title, 0.88)
                    if sc >= 0.88:
                        out[i] = sc
        return out

    def find(self, lines):
        """lines: [(text, x, y, w, h)] from OCR. Returns (song, score) or (None, reason).
        score >= 2 means the artist (or the whole "Title by Artist") was seen too: that's a sure match."""
        raw = [(re.sub(r"[^a-z0-9]+", " ", unicodedata.normalize("NFKD", str(t).lower())), x, y, w, h) for t, x, y, w, h in lines]
        lines = [(normalize(t), x, y, w, h) for t, x, y, w, h in lines]
        keep = [i for i, l in enumerate(lines) if l[0] and len(l[0]) <= 90]
        lines, raw = [lines[i] for i in keep], [raw[i][0] for i in keep]
        hits = {}
        for li, (text, x, y, w, h) in enumerate(lines):
            for i, s in self._candidates(text).items():
                song, title, artist, full = self.entries[i]
                score = s
                if s < 3.0:
                    # the artist right next to the title (a line above/below, or on the same row)
                    near = [l for j, l in enumerate(lines) if j != li and
                            abs(l[2] - y) <= 3.5 * max(h, l[4]) and abs(l[1] - x) <= 4 * max(w, l[3]) + 200]
                    if artist and any(l[0] == artist or (len(artist) >= 6 and artist in l[0]) or
                                      similar(l[0], artist, 0.85) >= 0.85 for l in near):
                        score = 2.0 + s
                    elif len(title) < 10 or len(self.by_title.get(title, ())) > 1:
                        continue  # short or shared titles ("One", "Animals") need the artist next to them
                extra = self.extra[i]
                if extra:  # a version like "(Molten Mix)": a bit more if it's on screen, a bit less if not
                    around = " ".join(raw[j] for j, l in enumerate(lines) if j == li or
                                      (abs(l[2] - y) <= 2.5 * max(h, l[4]) and abs(l[1] - x) <= 2 * max(w, l[3]) + 100))
                    words = around.split()
                    seen = sum(any(similar(w, v, 0.75) >= 0.75 for v in words) for w in extra.split())
                    score += 0.05 if seen * 2 > len(extra.split()) else -0.05
                key = song["name"]
                if score > hits.get(key, (0, None))[0]:
                    hits[key] = (score, song, (x, y))
        if not hits:
            return None, "no song title on screen"
        best = sorted(hits.values(), key=lambda h: -h[0])
        strong = [h for h in best if h[0] >= 1.9] or best
        if len(strong) > 1 and strong[0][0] - strong[1][0] >= 0.08 and strong[0][1]["name"].rsplit(" by ", 1)[0].split("(")[0].strip().lower() == \
                strong[1][1]["name"].rsplit(" by ", 1)[0].split("(")[0].strip().lower():
            return strong[0][1], strong[0][0]  # two versions of one song: the version text decided it
        if len(strong) == 1:
            return strong[0][1], strong[0][0]
        # several songs on screen (a playlist or the song list): take the one by "Up Next"/"Now Playing"
        anchors = [(x, y) for text, x, y, w, h in lines if any(a in text for a in ANCHORS)]
        if anchors:
            ax, ay = anchors[0]
            pick = min(strong, key=lambda h: abs(h[2][1] - ay) + 0.3 * abs(h[2][0] - ax))
            return pick[1], min(pick[0], 1.9)  # picked between several: confirm with a second look
        return None, f"{len(strong)} songs on screen"


def grab_screen():
    """Whole screen as a BGR image, through GDI (works with windowed fullscreen)."""
    import win32con
    import win32gui
    import win32ui
    import win32api
    width, height = win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1)
    desktop = win32gui.GetDesktopWindow()
    desktop_dc = win32gui.GetWindowDC(desktop)
    source = win32ui.CreateDCFromHandle(desktop_dc)
    memory = source.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(source, width, height)
    memory.SelectObject(bitmap)
    memory.BitBlt((0, 0), (width, height), source, (0, 0), win32con.SRCCOPY)
    image = np.frombuffer(bitmap.GetBitmapBits(True), dtype=np.uint8).reshape(height, width, 4)[:, :, :3].copy()
    memory.DeleteDC()
    source.DeleteDC()
    win32gui.ReleaseDC(desktop, desktop_dc)
    win32gui.DeleteObject(bitmap.GetHandle())
    return image


def read_screen():
    """[(text, x, y, w, h)] for every line of text on screen, in screen pixels."""
    import cv2
    import winocr
    image = grab_screen()
    scale = min(1.0, 1920 / image.shape[1])  # Windows OCR has a size limit, 1080p width reads fine
    if scale < 1.0:
        image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    result = winocr.recognize_cv2_sync(image, "en")
    lines = []
    for line in result.get("lines", []) or []:
        words = line.get("words", []) or []
        rects = [w.get("bounding_rect", {}) for w in words if isinstance(w, dict)]
        if not rects:
            continue
        x0 = min(r.get("x", 0) for r in rects)
        y0 = min(r.get("y", 0) for r in rects)
        x1 = max(r.get("x", 0) + r.get("width", 0) for r in rects)
        y1 = max(r.get("y", 0) + r.get("height", 0) for r in rects)
        lines.append((line.get("text", ""), (x0 + x1) / 2 / scale, (y0 + y1) / 2 / scale,
                      (x1 - x0) / scale, (y1 - y0) / scale))
    return lines


class AutoSongDetector:
    """Reads the screen over and over while no chart is playing. A sure match (title with its artist, or
    "Title by Artist") is reported on the first look; a title alone has to be seen twice in a row."""

    def __init__(self, songs, found, should_look, log, interval = 0.25):
        self.matcher = SongMatcher(songs)
        self.found = found
        self.should_look = should_look  # False, True, or "slow" (a chart is loaded and playing)
        self.log = log
        self.interval = interval
        self.running = True
        self.last = None
        self.reported = None
        self.read_ms = 0.0
        self.looks = 0
        self.thread = threading.Thread(target=self.run, daemon=True, name="song-detect")

    def start(self):
        try:
            import winocr  # noqa: F401
        except Exception:
            self.log("Song auto detection needs winocr (run setup.bat again). Use the song search until then")
            return False
        self.thread.start()
        return True

    def run(self):
        failures = 0
        while self.running:
            look = self.should_look()
            if not look:
                self.last = None
                time.sleep(0.1)
                continue
            t0 = time.perf_counter()
            try:
                lines = read_screen()
                song, score = self.matcher.find(lines)
                failures = 0
            except Exception as e:
                failures += 1
                if failures == 3:
                    self.log(f"Song auto detection is not working ({str(e)}). Use the song search instead")
                time.sleep(1.0)
                continue
            took = time.perf_counter() - t0
            self.read_ms = 0.8 * self.read_ms + 0.2 * took * 1000 if self.looks else took * 1000
            self.looks += 1
            if self.looks == 3:
                self.log(f"Song detection reads the screen in {self.read_ms:.0f} ms")
            if song is not None:
                name = song["name"]
                sure = isinstance(score, float) and score >= 2.0
                if name != self.reported and (sure or self.last == name):
                    self.reported = name
                    self.found(song)
                self.last = name
            else:
                self.last = None
            wait = self.interval if look != "slow" else max(1.0, self.interval)
            time.sleep(max(0.02, wait - took))

    def picked(self, song):
        # picked by hand: keep it until a different song shows up on screen
        self.reported = song["name"]
        self.last = song["name"]

    def forget(self):
        # after a manual pick or a finished song, the same song can be reported again
        self.reported = None
        self.last = None
