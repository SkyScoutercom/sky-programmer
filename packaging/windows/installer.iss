; Inno Setup script for the Windows installer: iscc /DAppVersion=0.1.0 packaging\windows\installer.iss
; Installs for the current user, so it needs no administrator rights.

[Setup]
AppName=SKY programmer
AppVersion={#AppVersion}
AppPublisher=SkyScouter
DefaultDirName={autopf}\SKY programmer
DefaultGroupName=SKY programmer
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\dist
OutputBaseFilename=SKY-programmer-setup-{#AppVersion}
SetupIconFile=sky-programmer.ico
UninstallDisplayIcon={app}\SKY programmer.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "..\..\dist\SKY programmer\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\SKY programmer"; Filename: "{app}\SKY programmer.exe"
Name: "{group}\SKY terminal"; Filename: "{cmd}"; Parameters: "/k sky --help"; WorkingDir: "{app}"; IconFilename: "{app}\sky.exe"
Name: "{autodesktop}\SKY programmer"; Filename: "{app}\SKY programmer.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\SKY programmer.exe"; Description: "Start SKY programmer"; Flags: nowait postinstall skipifsilent
