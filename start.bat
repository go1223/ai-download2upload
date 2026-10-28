@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo [错误] 没有找到 Python。请先安装 Python 3.9 或更高版本。
  echo 下载地址: https://www.python.org/downloads/
  echo 安装时请勾选 "Add python.exe to PATH"。
  echo.
  pause
  exit /b 1
)

python -c "import flask, requests, yt_dlp, Crypto" >nul 2>nul
if errorlevel 1 (
  echo.
  echo [首次运行] 正在安装 Python 依赖，请稍候...
  python -m pip install --disable-pip-version-check -r requirements.txt
  if errorlevel 1 (
    echo.
    echo [错误] 依赖安装失败。可尝试手动执行：
    echo   python -m pip install --user -r requirements.txt
    echo.
    pause
    exit /b 1
  )
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
  if exist "%~dp0bin\ffmpeg.exe" (
    set "FFMPEG_PATH=%~dp0bin\ffmpeg.exe"
  ) else (
    echo.
    echo [警告] 没有找到 ffmpeg，MP4 合并和 MP3 转码会失败。
    echo 安装方法: winget install Gyan.FFmpeg
    echo 或把 ffmpeg.exe 放到项目的 bin\ 目录。
    echo.
  )
)

if not defined HOST set "HOST=127.0.0.1"
if not defined PORT set "PORT=8765"
if not defined OPEN_BROWSER set "OPEN_BROWSER=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=1"

echo.
echo 正在启动声取 MediaDock...
python app.py
if errorlevel 1 (
  echo.
  echo 服务异常退出。请查看上方错误信息。
  pause
)
