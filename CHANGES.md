# Changes from the original Stellite

Stellite NX starts from **Stellite** by Jinx (https://github.com/JinxTheCatto/Stellite), including the Stellite v2 song
chart and overdrive release by Jinx and theyhating. These are the changes made for Stellite NX. Each changed file also says so in its header.

Starborne's main additions: the Stellite NX app (with your own keys and hotkeys), automatic song detection and the
lift (diamond) note fix. The list below covers every change, as the license asks.

## 2026-10-17
- `engine.py` (new): the screen reading and timing were rewritten: note tracking across frames, note speed learning,
  predicted press times, a key scheduler (minimum key-up time, release gaps), hold notes followed along their trails
  (also through overdrive glow and side-by-side holds), lift notes released on time, a debug overlay, debug videos
  and a replay tool.
- `winio.py` (new): Windows key presses with scan codes, hotkeys, timer resolution and screen capture.
- `bot.py`: rebuilt around the new engine, with `config.json` defaults and migration from the original config format.

## 2026-10-18
- Merged with Stellite v2: song charts from fnfpaths (`charts.py`), chart sync and the in-game song timer
  (`overlay.py`, unchanged from Stellite v2).
- `chartassist.py` (new): the chart helps the screen with exact sustain ends and notes the screen missed, only while
  the two agree.
- `songdetect.py` (new): finds the song on screen (Up Next, playlists, in song) with Windows' text reader.
  Fast indexed matching; a title with its artist loads on the first look.
- `charts.py`: saved charts are used right away and refreshed in the background, charts are read once and saved,
  reading runs in separate processes, and every chart for your instrument can be made ready ahead of time.
- `core.py` (new): the player as one class shared by the app and the console version, with instrument, overdrive,
  song detection and timing controls.
- `app.py` and `ui/` (new): the Stellite NX app window (pywebview), with lane keys, play key, overdrive key, hotkeys and
  settings changeable in the app, and an About screen with the license and credits.
- Overdrive can be pressed automatically on the fnfpaths path (off by default).
- `build_exe.bat`, `make_release.bat`, `tools/make_release.py` (new): build `Stellite.exe` and the release zips.
- `README.md`, `NOTICE.md`, `CHANGES.md`: rewritten / new. `update.bat` was removed (it pulled the original repo over
  this one).
