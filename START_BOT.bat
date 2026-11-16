@echo off
rem Запуск Guide Bot через Python. Windows (Smart App Control) блокирует неподписанный
rem GuideBot.exe, а этот способ работает всегда: python.exe подписан.
chcp 65001 >nul
title Guide Bot
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python не найден. Установи Python 3.10+ с https://www.python.org/downloads/
    echo и поставь галочку "Add python.exe to PATH".
    pause
    exit /b 1
)

python -c "import cv2, mss, numpy" >nul 2>nul
if errorlevel 1 (
    echo Первый запуск: ставлю нужные библиотеки...
    python -m pip install --quiet -r requirements.txt
    if errorlevel 1 (
        echo Не удалось установить библиотеки. Проверь интернет и попробуй ещё раз.
        pause
        exit /b 1
    )
)

python guide_bot.py %*
pause
