@echo off
chcp 65001 >nul
title JARVIS setup
setlocal

rem Installs or updates JARVIS, puts a JARVIS icon on the desktop and in the Start menu, then opens it.
rem Run it again any time to update.

rem 1) uv (installs Python for JARVIS without touching the system Python)
set "UV=%USERPROFILE%\.local\bin\uv.exe"
if not exist "%UV%" (
  echo [JARVIS] Installing uv...
  powershell -NoProfile -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"
)
if not exist "%UV%" (
  echo [JARVIS] uv install failed. Send this screen to Claude.
  pause & exit /b 1
)

rem 2) Git
set "GIT=git"
where git >nul 2>nul || set "GIT=C:\Program Files\Git\cmd\git.exe"
if not "%GIT%"=="git" if not exist "%GIT%" (
  echo [JARVIS] Installing Git...
  winget install --id Git.Git -e --source winget --accept-source-agreements --accept-package-agreements
)

rem 3) JARVIS code: update an existing copy, or download it
set "APP=%USERPROFILE%\javice\javice"
if not exist "%APP%\pyproject.toml" set "APP=%USERPROFILE%\javice"
if exist "%APP%\pyproject.toml" (
  echo [JARVIS] Updating...
  "%GIT%" -C "%APP%" fetch -q origin main && "%GIT%" -C "%APP%" checkout -q -B main origin/main || echo [JARVIS] Could not update; keeping the current version.
) else (
  echo [JARVIS] Downloading JARVIS...
  "%GIT%" clone -q -b main https://github.com/chanmini584-ux/javice.git "%APP%"
)
if not exist "%APP%\pyproject.toml" (
  echo [JARVIS] Download failed. Send this screen to Claude.
  pause & exit /b 1
)

rem 4) Install the desktop app (first time: 1-2 min)
cd /d "%APP%"
echo [JARVIS] Installing the app...
"%UV%" sync -q --python 3.12 --extra web --extra desktop
if not exist "%APP%\.venv\Scripts\jarvis-desktop.exe" (
  echo [JARVIS] App install failed. Send this screen to Claude.
  pause & exit /b 1
)

rem 5) Icon + shortcuts (desktop and Start menu)
"%APP%\.venv\Scripts\python.exe" -c "from jarvis.desktop.tray import save_ico; save_ico(r'%APP%\jarvis.ico')"
set "JARVIS_APP=%APP%"
powershell -NoProfile -ExecutionPolicy Bypass -c "$w=New-Object -ComObject WScript.Shell; $a=$env:JARVIS_APP; foreach($d in @([Environment]::GetFolderPath('Desktop'),[Environment]::GetFolderPath('Programs'))){ $l=$w.CreateShortcut((Join-Path $d 'JARVIS.lnk')); $l.TargetPath=(Join-Path $a '.venv\Scripts\jarvis-desktop.exe'); $l.WorkingDirectory=$a; $l.IconLocation=(Join-Path $a 'jarvis.ico'); $l.Description='JARVIS'; $l.Save() }"

rem 6) Open it. From now on, use the JARVIS icon.
echo [JARVIS] Done. Opening JARVIS... From now on just use the JARVIS icon on the desktop.
start "" "%APP%\.venv\Scripts\jarvis-desktop.exe"
timeout /t 5 >nul
