@echo off
rem Stellite NX - builds Stellite.exe from this folder (part of the source, GNU AGPL v3, see LICENSE)
title Building Stellite.exe
cd /d "%~dp0"
echo Installing what Stellite and the build need...
python -m pip install -r assets\requirements.txt
if errorlevel 1 goto failed
python -m pip install --upgrade pyinstaller
if errorlevel 1 goto failed
echo.
echo Building Stellite.exe (takes a minute or two)...
python -m PyInstaller --noconfirm --clean --onefile --windowed --name Stellite --icon "ui\stellite.ico" ^
  --add-data "assets;assets" --add-data "ui;ui" --add-data "LICENSE;." --add-data "NOTICE.md;." ^
  --hidden-import core --hidden-import bot --hidden-import engine --hidden-import charts ^
  --hidden-import chartassist --hidden-import songdetect --hidden-import overlay --hidden-import winio ^
  --hidden-import clr ^
  --collect-all bettercam --collect-submodules comtypes --collect-all webview ^
  --collect-all winocr --collect-all winrt ^
  app.py
if errorlevel 1 goto failed
copy /y "dist\Stellite.exe" "Stellite.exe" >nul
echo.
echo ------------------------------------------------------------------------
echo Done: Stellite.exe is in this folder (a copy is in the dist folder).
echo It uses the config.json, data and snapshots next to it, so keep it here,
echo or put it in its own folder to share it: it makes its own config.json there.
echo To share it, use make_release.bat: the exe has to go out with its source.
echo ------------------------------------------------------------------------
if /i "%~1"=="nopause" exit /b 0
pause
exit /b 0

:failed
echo.
echo Build failed, see the messages above.
if /i not "%~1"=="nopause" pause
exit /b 1
