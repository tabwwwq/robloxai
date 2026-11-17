@echo off
rem Guide Bot launcher. Runs the bot with Python: Windows Smart App Control blocks
rem the unsigned GuideBot.exe, but python.exe is signed and always allowed.
rem Keep this file ASCII-only: cmd.exe breaks on non-ASCII text when the file
rem is downloaded from GitHub with Unix line endings.
title Guide Bot
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 goto nopython

python -c "import cv2, mss, numpy" >nul 2>nul
if errorlevel 1 goto install
goto run

:install
echo First start: installing libraries, please wait...
python -m pip install --quiet -r requirements.txt
if errorlevel 1 goto pipfail

:run
python guide_bot.py %*
pause
exit /b 0

:nopython
echo Python was not found.
echo Install Python 3.10 or newer from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" in the installer.
pause
exit /b 1

:pipfail
echo Could not install the libraries. Check your internet connection and try again.
pause
exit /b 1
