@echo off
cd /d "%~dp0"
if exist bot.pid (
    set /p OLDPID=<bot.pid
    tasklist /FI "PID eq %OLDPID%" 2>nul | find "%OLDPID%" >nul
    if not errorlevel 1 (
        echo Bot already running PID %OLDPID%
        exit /b 0
    )
)
powershell -Command "$p = Start-Process python -ArgumentList 'monitor.py' -RedirectStandardOutput monitor.out -RedirectStandardError monitor.err -WindowStyle Hidden -PassThru; $p.Id | Out-File -Encoding ascii bot.pid"
set /p PID=<bot.pid
echo Bot started PID %PID%
