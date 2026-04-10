@echo off
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo Python not found! Download from https://www.python.org/downloads/
    pause
    exit /b 1
)
echo Installing dependencies...
python -m ensurepip --upgrade
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
echo Done.
pause
