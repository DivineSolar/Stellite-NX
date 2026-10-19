# Notices

## Stellite NX
Stellite NX, by **Starborne**, is a modified version of **Stellite** by **Jinx** (jinxthecat_,
https://github.com/JinxTheCatto/Stellite). Stellite's overdrive features were built by Jinx with **theyhating**.
Starborne made the Stellite NX app, automatic song detection and the lift (diamond) note fix.

- Stellite: Copyright (C) Jinx and the Stellite contributors. Overdrive: Jinx and theyhating.
- Changes for Stellite NX: Copyright (C) 2026 Starborne. What changed and when is listed in `CHANGES.md`,
  and each changed file says so at the top.

This program is free software: you can redistribute it and/or modify it under the terms of the GNU Affero General
Public License, version 3, as published by the Free Software Foundation (see `LICENSE`).
This program is distributed WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
FOR A PARTICULAR PURPOSE. See the GNU Affero General Public License for more details.

Stellite NX is not made or endorsed by Jinx or theyhating. It is not affiliated with, endorsed by or sponsored by Epic Games, Inc.
Fortnite is a trademark of Epic Games, Inc. Like the original Stellite, this project is for educational purposes only.

## Corresponding source
The complete source code of this version, including the scripts used to build `Stellite.exe` (`build_exe.bat`,
`make_release.bat`, `tools/`), is in `Stellite-NX-<version>-source.zip`, which is published and shipped together
with the exe. The in-app About screen says where to find it.

## Parts by others
- **Festival OD Coach** by Aknownthreat (https://github.com/Aknownthreat/-fortnite-festival-overdrive-timer),
  Copyright (C) 2026 Aknownthreat, GPL-3.0. Stellite's chart reading (`charts.py`) and in-game overlay
  (`overlay.py`) are based on it. GPL-3.0 code may be combined with AGPL-3.0 code (GPL-3.0 section 13).
- **Note images** in `assets/` come from the original Stellite.
- **Song charts and overdrive paths** are downloaded at run time from **fnfpaths** (https://fnfpaths.github.io),
  drawn with **CHOpt** (https://github.com/GenericMadScientist/CHOpt). They are not included in this release.
- **Song lengths** are downloaded at run time from Epic's public Festival track list.

## Libraries inside Stellite.exe
`Stellite.exe` is built with PyInstaller and contains Python and these libraries. Their full license texts are in
`THIRD_PARTY_LICENSES.txt` in the Windows zip (collected from the installed packages when the release was built).

| Library | License |
|---|---|
| Python | PSF License |
| NumPy | BSD-3-Clause (and the licenses of the libraries it bundles) |
| opencv-python | Apache-2.0; its wheel bundles third-party code, including FFmpeg under the LGPL-2.1 (see THIRD_PARTY_LICENSES.txt) |
| BetterCam | MIT |
| comtypes | MIT |
| colorama | BSD-3-Clause |
| pywin32 | PSF-2.0 |
| winocr | MIT |
| winrt (PyWinRT) packages | MIT |
| pywebview and its dependencies (pythonnet, clr_loader, bottle, proxy_tools, ...) | BSD-3-Clause, MIT and others, see THIRD_PARTY_LICENSES.txt |
| PyInstaller bootloader | GPL-2.0 with the bootloader exception |

Windows' own text reader (Windows.Media.Ocr) and Microsoft Edge WebView2 are part of Windows and are not included.
