; CampusReconnect standard per-user Windows installer.
#ifndef MyAppVersion
  #define MyAppVersion "0.9.7"
#endif

#define MyAppName "校园网自动重连助手"
#define MyAppPublisher "lcq666666"
#define MyAppExeName "CampusReconnect.exe"

[Setup]
AppId={{B0F4A1F2-6F8E-4A95-8C1C-9C0B0D6F4F2A}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\CampusReconnect
DefaultGroupName={#MyAppName}
OutputDir=..\dist
OutputBaseFilename=CampusReconnectSetup
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64
UninstallDisplayIcon={app}\{#MyAppExeName}
DisableProgramGroupPage=yes
; User data and logs live below %LOCALAPPDATA%\CampusReconnect and are preserved.

[Files]
Source: "..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："; Flags: unchecked

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "CampusReconnectTray"; \
    ValueData: "{app}\{#MyAppExeName} --background"; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#MyAppExeName}"; Parameters: "--background"; \
    Description: "启动校园网自动重连助手"; Flags: nowait postinstall
