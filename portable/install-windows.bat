@echo off
chcp 65001 >nul
title JARVIS
setlocal

rem 1) uv (Python manager)
set "UV=%USERPROFILE%\.local\bin\uv.exe"
if not exist "%UV%" (
  echo [JARVIS] Installing uv...
  powershell -NoProfile -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"
)
if not exist "%UV%" (
  echo [JARVIS] uv install failed. Send this screen to Claude.
  pause & exit /b 1
)

rem 2) JARVIS code
set "APP=%USERPROFILE%\javice\javice"
if not exist "%APP%\pyproject.toml" set "APP=%USERPROFILE%\javice"
if exist "%APP%\pyproject.toml" goto run

set "GIT=git"
where git >nul 2>nul || set "GIT=C:\Program Files\Git\cmd\git.exe"
if not exist "%GIT%" if not "%GIT%"=="git" (
  echo [JARVIS] Installing Git...
  winget install --id Git.Git -e --source winget --accept-source-agreements --accept-package-agreements
)
echo [JARVIS] Downloading JARVIS...
"%GIT%" clone -b claude/jarvis-mvp-u8vsg1 https://github.com/chanmini584-ux/javice.git "%USERPROFILE%\javice"
set "APP=%USERPROFILE%\javice"
if not exist "%APP%\pyproject.toml" (
  echo [JARVIS] Download failed. Send this screen to Claude.
  pause & exit /b 1
)

:run
cd /d "%APP%"
echo [JARVIS] Starting from %CD%  (first run takes 1-2 min)
"%UV%" run --python 3.12 --extra web jarvis serve --open
echo.
echo [JARVIS] Stopped. Press any key to close.
pause >nul
