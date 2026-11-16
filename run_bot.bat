@echo off
chcp 65001 >nul
cd /d "%~dp0"
python guide_bot.py %*
pause
