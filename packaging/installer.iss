; Inno Setup — установщик rentbot (Windows).
; Собирать:  "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\installer.iss
; (перед этим собрать приложение: .venv/Scripts/pyinstaller packaging/rentbot.spec)

#define AppName "rentbot"
#define AppVersion "1.0.0"
#define AppPublisher "rentbot"
#define AppExe "rentbot.exe"

[Setup]
AppId={{B6F3B2A1-9C4E-4E2A-9E7D-RENTBOT00001}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=installer_out
OutputBaseFilename=rentbot-Setup
SetupIconFile=rentbot.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
PrivilegesRequired=admin

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce

[Files]
; Вся onedir-сборка PyInstaller (rentbot.exe + _internal с web, sidecar, зависимостями)
Source: "dist\rentbot\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Удалить {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; пользовательская БД в %LOCALAPPDATA%\rentbot НЕ удаляется (данные владельца) — удалять вручную
