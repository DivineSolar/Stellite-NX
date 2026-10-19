@echo off
rem Stellite NX (GNU AGPL v3, see LICENSE)
Title Downloading Modules...
cd /d "%~dp0"
python -m pip install -r assets/requirements.txt
echo ------------------------------------------------------------------------
echo Script reached end. Start the app with start_app.bat (or build Stellite.exe with build_exe.bat).
echo start.bat still runs the console version. If there is an error above, fix it first.
pause
