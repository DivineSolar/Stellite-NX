@echo off
rem Stellite NX - builds Stellite.exe and the release zips (part of the source, GNU AGPL v3, see LICENSE)
title Making a Stellite NX release
cd /d "%~dp0"
call build_exe.bat nopause
if errorlevel 1 goto failed
echo.
echo Packing the release...
python tools\make_release.py
if errorlevel 1 goto failed
echo.
echo ------------------------------------------------------------------------
echo The release is in the release folder. Publish BOTH zips together:
echo   Stellite-NX-*-windows.zip  (the exe, with the license, notices and source)
echo   Stellite-NX-*-source.zip   (the source code)
echo Keep the credits to Jinx, and link the original: https://github.com/JinxTheCatto/Stellite
echo ------------------------------------------------------------------------
pause
exit /b 0

:failed
echo.
echo Making the release failed, see the messages above.
pause
exit /b 1
