@echo off
title CertBot Launcher

echo Changing directory to the script's location...
cd /d %~dp0

echo.
echo Starting the bot (bot.py)...
echo To stop the bot, press Ctrl+C in this window.
echo.

python bot.py

echo.
echo The bot script has finished or was stopped.
pause