@echo off
cd /d "%~dp0"
if not exist bot.pid (
    echo bot.pid not found - bot is not running
    exit /b 1
)
set /p PID=<bot.pid
taskkill /F /PID %PID%
del bot.pid
echo Bot stopped PID %PID%
