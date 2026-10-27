; JARVIS installer (Inno Setup 6). Built by packaging/build_windows.ps1.
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

[Setup]
AppId={{8B7A6F3E-5C1D-4E2A-9F40-6D1B2C3A4E57}
AppName=JARVIS
AppVersion={#AppVersion}
AppPublisher=JARVIS
DefaultDirName={autopf}\JARVIS
DefaultGroupName=JARVIS
; Per-user install: no administrator rights, no UAC prompt.
PrivilegesRequired=lowest
OutputDir=..\dist-installer
OutputBaseFilename=JARVIS-Setup-{#AppVersion}
SetupIconFile=jarvis.ico
UninstallDisplayIcon={app}\JARVIS.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "바탕 화면에 JARVIS 아이콘 만들기"; GroupDescription: "추가 작업:"
Name: "autostart"; Description: "Windows를 시작할 때 JARVIS 자동 실행 (트레이에 상주)"; GroupDescription: "추가 작업:"

[Files]
Source: "..\dist\JARVIS\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\JARVIS"; Filename: "{app}\JARVIS.exe"
Name: "{autodesktop}\JARVIS"; Filename: "{app}\JARVIS.exe"; Tasks: desktopicon

[Registry]
; The same per-user Run entry the tray menu's "Windows 시작 시 자동 실행" switch edits.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "JARVIS"; ValueData: """{app}\JARVIS.exe"" --background"; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\JARVIS.exe"; Description: "JARVIS 지금 실행"; Flags: nowait postinstall skipifsilent

[Code]
// Also remove an autostart entry that was switched on later from the tray menu.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', 'JARVIS');
end;
