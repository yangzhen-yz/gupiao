@echo off
chcp 65001 >nul

echo [1/4] Building frontend...
cd /d "%~dp0..\client-vue"
if not exist "node_modules\" (
    echo Installing frontend dependencies...
    pnpm install
)
pnpm run build
if %errorlevel% neq 0 (
    echo Frontend build failed!
    pause
    exit /b 1
)

echo.
echo [2/4] Checking Python...
python --version
if %errorlevel% neq 0 (
    echo Python not found
    pause
    exit /b 1
)

echo.
echo [3/4] Installing Python dependencies...
pip install -r "%~dp0requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple
if %errorlevel% neq 0 (
    pip install -r "%~dp0requirements.txt"
)

echo.
echo [4/4] Starting FastAPI...
echo   http://localhost:3003
echo   API docs: http://localhost:3003/docs
echo.

cd /d "%~dp0"
python run.py

pause
