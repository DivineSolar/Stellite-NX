# Stellite NX - the app: a Fortnite Festival style window around the player
#
# Based on Stellite by Jinx (https://github.com/JinxTheCatto/Stellite), AGPL-3.0. Credit to Jinx and
# Stellite must be kept. Song charts from fnfpaths; chart reading and the in-game timer overlay are
# Stellite v2's, based on Festival OD Coach by Aknownthreat (GPL-3.0).
# Copyright (C) 2026 Starborne. Part of a work based on Stellite by Jinx; free software under
# the terms of the GNU Affero General Public License version 3 (see LICENSE). It comes WITHOUT ANY WARRANTY.

import os
import sys

if sys.stdout is None:  # Stellite.exe has no console
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = sys.stdout

from bot import CONFIG_PATH, load_config, save_config  # noqa: E402
from core import APP_DIR, HOTKEY_ACTIONS, RES_DIR, SOURCE_URL, UPSTREAM_URL, VERSION, Stellite  # noqa: E402


class Api:
    """What the window can ask the player to do (called from ui/index.html)."""

    def __init__(self, player):
        self._p = player

    def get_state(self):
        return self._p.state()

    def get_meta(self):
        return {"actions": HOTKEY_ACTIONS}

    def search(self, query):
        return self._p.search(str(query))

    def pick(self, name):
        return self._p.set_song(str(name))

    def set_instrument(self, instrument):
        return self._p.set_instrument(str(instrument))

    def set_overdrive_auto(self, on):
        return self._p.set_overdrive_auto(bool(on))

    def set_auto_detect(self, on):
        return self._p.set_auto_detect(bool(on))

    def nudge(self, ms):
        return self._p.nudge(float(ms))

    def sync(self):
        self._p.sync_on_go()
        return True

    def set_hotkey(self, action, key):
        return self._p.set_hotkey(str(action), str(key))

    def set_lane_key(self, lanes, lane, key):
        return self._p.set_lane_key(int(lanes), int(lane), str(key))

    def set_play_key(self, key):
        return self._p.set_play_key(str(key))

    def set_overdrive_key(self, key):
        return self._p.set_overdrive_key(str(key))

    def reset_keys(self):
        return self._p.reset_keys()

    def set_lanes(self, lanes):
        return self._p.set_lanes(int(lanes))

    def set_monitor(self, monitor):
        return self._p.set_monitor(int(monitor))

    def set_setting(self, name, value):
        return self._p.set_setting(str(name), value)

    def get_about(self):
        text = ""
        for folder in (RES_DIR, APP_DIR):
            try:
                with open(os.path.join(folder, "LICENSE"), "r", encoding="utf-8") as f:
                    text = f.read()
                break
            except OSError:
                continue
        return {"version": VERSION, "source_url": SOURCE_URL, "upstream_url": UPSTREAM_URL,
                "license": text or "The LICENSE file is missing. The license is the GNU AGPL v3: https://www.gnu.org/licenses/agpl-3.0.html"}

    def open_url(self, url):
        url = str(url)
        if not url.startswith(("https://", "http://")):
            return False
        import webbrowser
        webbrowser.open(url)
        return True

    def pause_hotkeys(self, paused):
        self._p.hotkeys_paused = bool(paused)  # while a key is being rebound in the window
        return True


def main():
    import webview

    os.chdir(APP_DIR)
    try:
        cfg = load_config()
    except SystemExit:  # config.json can't be read: start on the defaults and leave the file alone
        import copy
        from bot import DEFAULTS
        cfg = copy.deepcopy(DEFAULTS)
        cfg["_config_broken"] = True
    if not cfg.get("_config_broken") and not os.path.exists(CONFIG_PATH):
        save_config(cfg)  # first start: write config.json next to the app
    broken = cfg.pop("_config_broken", False)
    player = Stellite(cfg)
    if broken:
        player.read_only = True
        player.log("config.json could not be read, running on default settings (changes are not saved)")
    player.start()
    webview.create_window("Stellite", url=os.path.join(RES_DIR, "ui", "index.html"), js_api=Api(player),
                          width=1240, height=820, min_size=(1080, 720), background_color="#120a2e")
    webview.start()
    player.stop()


def crashed():
    # no console in the app: put the error in a file and on screen instead of vanishing
    import traceback
    text = traceback.format_exc()
    path = os.path.join(APP_DIR, "stellite_error.log")
    try:
        with open(path, "w") as f:
            f.write(text)
    except Exception:
        pass
    try:
        import ctypes
        last = text.strip().splitlines()[-1]
        ctypes.windll.user32.MessageBoxW(None, f"Stellite stopped:\n\n{last}\n\nDetails are in {path}", "Stellite", 0x10)
    except Exception:
        print(text)


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()  # the chart reader runs in a second process of Stellite.exe
    try:
        main()
    except Exception:
        crashed()
