# Build JARVIS-Setup-<version>.exe on Windows.
#   pip install -e ".[web,desktop]" pyinstaller
#   powershell -File packaging/build_windows.ps1
# Needs Inno Setup 6 (https://jrsoftware.org/isinfo.php); installed with Chocolatey if missing.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

python packaging/make_icon.py
if ($LASTEXITCODE -ne 0) { throw "icon build failed" }

pyinstaller packaging/jarvis.spec --noconfirm --clean --distpath dist --workpath build
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
if (-not (Test-Path "dist/JARVIS/JARVIS.exe")) { throw "dist/JARVIS/JARVIS.exe was not produced" }

$version = (python -c "import jarvis; print(jarvis.__version__)").Trim()
$candidates = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles}\Inno Setup 6\ISCC.exe",
    "${env:LOCALAPPDATA}\Programs\Inno Setup 6\ISCC.exe"
)
$iscc = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) {
    choco install innosetup -y --no-progress
    $iscc = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $iscc) { throw "Inno Setup (ISCC.exe) not found" }

& $iscc "/DAppVersion=$version" packaging/installer.iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
Get-ChildItem dist-installer\*.exe | ForEach-Object { Write-Host "built: $($_.FullName)" }
