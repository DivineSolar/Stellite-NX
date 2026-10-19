# Stellite NX

By **Starborne**. **An unofficial, modified version of [Stellite](https://github.com/JinxTheCatto/Stellite)** by Jinx (jinxthecat_),
the Fortnite Festival autoplayer. Stellite NX is not made or endorsed by Jinx or theyhating and is not
affiliated with Epic Games.
For the original project, its Discord and support, go to [Jinx's repository](https://github.com/JinxTheCatto/Stellite).
Problems with Stellite NX are not Jinx's to fix, so please report them here, not on Jinx's Discord.

Licensed under the **GNU Affero General Public License v3** (see `LICENSE`), like the original. Credits are in
`NOTICE.md`, and what changed from the original is in `CHANGES.md`.

> **Like the original, this project is for educational purposes only.** Using an autoplayer in Fortnite can break
> Epic Games' rules and get an account banned. If you use it, that risk is yours.

## Download and run
- **Stellite-NX-…-windows.zip**: unzip it and run `Stellite.exe`. No Python needed. It needs Windows 10 or 11.
  Windows SmartScreen or antivirus may warn about it, because the exe isn't signed and it sends key presses
  to the game. The full source code is in the source zip inside it, so you can check it or build it yourself.
- **Stellite-NX-…-source.zip**: the source code. Run `setup.bat`, then `start_app.bat`, or `build_exe.bat` to
  make `Stellite.exe` yourself.

## The app
`Stellite.exe` (or `start_app.bat`) opens the Stellite window: a live highway showing which lanes are held, the
song that's playing with its timer and chart sync, song search, the six instruments, overdrive Manual / Auto,
timing nudges and an activity feed. Everything is set up in the app and saved to `config.json` straight away:
- **Controls > Your keys**: the lane keys you play Festival with (5 or 4 lanes; click a lane, press its key; picking a
  key another lane has swaps the two), the play key (Caps Lock, or any key that switches playing on and off) and the
  key Fortnite uses for overdrive. Reset puts every key and hotkey back to the defaults.
- **Controls > Hotkeys**: click an action, press the new key (Backspace clears it, Esc cancels).
- **Settings**: in-game song timer, chart fill-ins, automatic overdrive timing, how often to look for the song, and
  which screen Fortnite is on.

A key does one job: lane, play and overdrive keys can't also be hotkeys (Stellite's own key presses would set them off).
Taking a hotkey's key for a lane removes that hotkey and says so in the feed.

**Songs are picked automatically.** While no song is playing, Stellite reads the screen 4 times a second (Up Next,
playlists, the song list, the song itself) with the text reader built into Windows and finds the song on fnfpaths.
A title with its artist next to it counts on the first look; a title alone has to show up twice. Charts are read in
a separate process (playing never waits on it) and kept in `data/charts`, and on first start Stellite gets every
chart for your instrument ready in the background (about one a second, paused while you play), so a detected song
loads in a few milliseconds. The song's other instruments are made ready as soon as it's detected. Picking a song by hand still works, and the detector won't
undo it until a different song shows up. F3 or the switch in the app turns auto detect off.

To make the exe, run `build_exe.bat` once. It installs what's needed, builds `Stellite.exe` and copies it into this
folder, where it uses the same `config.json`, `data` and `snapshots`. It can also go in its own folder for someone
else: it writes a fresh `config.json` there on first start. It needs Windows 10/11 (the window is drawn by Edge
WebView2, which Windows already has). `start.bat` still runs the console version.

## How it plays
The screen engine plays every note. It sees each note, lift arrow and hold trail about 0.2 s before it reaches the
line, learns the note speed, and presses at the predicted moment.

When you pick the song, Stellite also reads its chart from fnfpaths and lines it up with what the screen sees
(a few notes in). From then on the chart sharpens the screen:
- **Holds** the screen sees are let go exactly where the chart's sustain ends, even when overdrive glow or
  two holds side by side make the trail hard to follow
- **Notes the screen missed** get pressed from the chart, but only while the chart and the screen agree.
  A missed note right after another one in the same lane is treated as a lift arrow (let go on it, then press again)
- **Song timer** overlay, counting down like the one in game

fnfpaths charts can be outdated in places (some songs changed with the lift note rework). Where the chart and the
screen disagree, the screen wins: lift arrows always come from the screen, and a sustain in the chart only counts
when the screen also sees a trail. If the chart stops matching, Stellite keeps playing from the screen and lines the
chart up again by itself.

## Setup
1. Python 3.10+.
2. Run `setup.bat` (installs `assets/requirements.txt`), then `build_exe.bat` for `Stellite.exe`, or use `start_app.bat`.
3. Fortnite in **windowed fullscreen**, low graphics.
4. Open Stellite, choose your instrument (Num 1-6). The song is found by itself, or search it in the app.
5. **Caps Lock on = playing**, Caps Lock off = paused (all keys released).

## Hotkeys
Defaults, all changeable in the app (or under `hotkeys` in `config.json`).

| Key | Action |
|---|---|
| Caps Lock | Play / pause |
| Num 1-6 | Lead, Bass, Drums, Vocals, Pro Lead, Pro Bass |
| F1 | Next instrument |
| F2 | Overdrive Manual / Auto |
| F3 | Song auto detect on / off |
| `[` | Pick a different song (brings the search up in the app; in the console, click it to type) |
| `]` | Sync the chart on GO. Only needed if the overlay keeps saying "syncing" |
| `-` / `=` | Notes 5 ms earlier / later (saved by itself) |
| F9 / F10 | Notes 1 ms earlier / later (F11 saves) |
| P | Exit (console version only, close the window in the app) |
| F8 | Debug overlay on/off |
| F5 | Start/stop a debug video of a whole song (`snapshots/video_*`, with a CSV of every key press and why) |
| F7 / F6 | Snapshot / short recording for tuning |

## Overlay
Top right, never takes focus. Turn caps lock off to drag it. Big number = song timer. Small line:
`syncing` (lining the chart up), `chart synced` (chart and screen agree), `synced (screen leads)` (chart is loose
here, screen plays alone), or the overdrive countdown if `use_overdrive` is on.

## Config (`config.json`, pixel values are at 1080p and scale automatically)
- `timing.offset_ms`: positive = later. `-`/`=` and F9/F10 change it.
- `timing.min_release_gap_ms` / `min_key_up_ms`: a key is let go this long before the next note in its lane and
  always stays up at least `min_key_up_ms` (shorter key-ups are invisible to the game).
- `timing.tap_linger_ms`: after a tap the key stays down until the next note in that lane, so far-off lift arrows find it held.
- `timing.lift_offset_ms`: extra shift for lift releases only.
- `charts.enabled`: song charts on/off. `charts.instrument`: the instrument you play.
- `charts.auto_detect`, `detect_every_s`: read the song off the screen, and how often (needs `winocr`).
- `charts.prepare_all`: get every chart for your instrument ready ahead of time.
- `charts.fill_in`: press chart notes the screen missed (only while chart and screen agree).
- `charts.use_overdrive`, `overdrive_key`, `overdrive_offset_ms`: overdrive on the fnfpaths path (off by default).
- `charts.show_overlay`: the song timer overlay.
- `holds.*`, `detection.*`, `geometry.*`: screen engine tuning, see the comments in `bot.py`.

Charts are expert only (5 lanes). On 4 lanes, or songs not on fnfpaths yet, Stellite plays from the screen alone,
which handles holds and lifts by itself.

## Limitations
- If Fortnite's Lift Notes Connectors or Good hit feedback make notes hard to see, turn them off in
  Settings > Game > Festival (same advice as Stellite v2).
- Don't copy the original Stellite over this folder: it would replace the new engine.

## Making a release
1. Put the link where people can get the source (your release page or GitHub fork) in `SOURCE_URL` in `core.py`,
   and the version in `VERSION`.
2. Run `make_release.bat`. It builds `Stellite.exe` from exactly this source and makes `release\Stellite-NX-<version>-windows.zip`
   (the exe, `LICENSE`, `NOTICE.md`, `CHANGES.md`, this README, the licenses of the libraries inside the exe, and the
   source zip) plus `release\Stellite-NX-<version>-source.zip`.
3. Publish both zips together. Under the AGPL, everyone who gets the exe must be able to get this source, and
   every change has to stay under the AGPL with the credits kept.

## Credits
- **Stellite** by Jinx (jinxthecat_): https://github.com/JinxTheCatto/Stellite
- **Overdrive** (song charts, overdrive paths and timing) by Jinx and theyhating
- **Stellite NX** by Starborne: the app, automatic song detection and the lift (diamond) note fix
- Chart reading and the in-game overlay: based on [Festival OD Coach](https://github.com/Aknownthreat/-fortnite-festival-overdrive-timer)
  by Aknownthreat, made for Stellite (GPL-3.0, combined with the AGPL-3.0)
- Song charts and overdrive paths: [fnfpaths](https://fnfpaths.github.io), drawn with [CHOpt](https://github.com/GenericMadScientist/CHOpt)
- Song lengths: Epic's public Festival track list
- Libraries: see `NOTICE.md`

Not affiliated with Epic Games. Fortnite is a trademark of Epic Games, Inc.
