@echo off
rem JARVIS portable launcher (Windows). Double-click, or run: start-windows.bat [--host 0.0.0.0] [--listen]
setlocal
set "DIR=%~dp0"
set "DIR=%DIR:~0,-1%"
set "JARVIS_HOME=%DIR%\data"
set "UV_CACHE_DIR=%DIR%\.cache\uv"
set "UV_LINK_MODE=copy"
set "UV_PYTHON_INSTALL_DIR=%LOCALAPPDATA%\jarvis\python"
set "UV_PROJECT_ENVIRONMENT=%LOCALAPPDATA%\jarvis\venv"
if not defined JARVIS_EXTRAS if exist "%DIR%\data\extras.txt" set /p JARVIS_EXTRAS=<"%DIR%\data\extras.txt"
if not defined JARVIS_EXTRAS set "JARVIS_EXTRAS=web"
set "EXTRA_ARGS="
for %%e in (%JARVIS_EXTRAS:,= %) do call set "EXTRA_ARGS=%%EXTRA_ARGS%% --extra %%e"

set "UV=%DIR%\bin\windows\uv.exe"
if not exist "%UV%" for /f "delims=" %%u in ('where uv 2^>nul') do set "UV=%%u"
if exist "%UV%" (
  "%UV%" run --project "%DIR%\app" %EXTRA_ARGS% --python 3.12 jarvis serve %*
  goto :end
)

where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
%PY% --version >nul 2>nul || (echo Python 3.10+ or uv is required. Put uv.exe in bin\windows or install Python. & pause & goto :end)
if not exist "%UV_PROJECT_ENVIRONMENT%\Scripts\jarvis.exe" (
  %PY% -m venv "%UV_PROJECT_ENVIRONMENT%"
  "%UV_PROJECT_ENVIRONMENT%\Scripts\pip" install -q "%DIR%\app[%JARVIS_EXTRAS%]"
)
"%UV_PROJECT_ENVIRONMENT%\Scripts\jarvis.exe" serve %*
:end
pause
endlocal
