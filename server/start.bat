@echo off
chcp 65001 >nul

echo [1/3] Checking Python...
python --version
if %errorlevel% neq 0 (
    echo Python not found
    pause
    exit /b 1
)

echo.
echo [2/3] Installing dependencies...
pip install -r "%~dp0requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple
if %errorlevel% neq 0 (
    pip install -r "%~dp0requirements.txt"
)

echo.
echo [3/3] Starting FastAPI...
echo   http://localhost:3003
echo   API docs: http://localhost:3003/docs
echo.

cd /d "%~dp0"
python main.py

pause
