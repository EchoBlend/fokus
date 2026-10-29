#define MyAppName "Fokus"
; Die Nummer steht nur in VERSION in fokus.py, build.ps1 reicht sie per /DMyAppVersion=... herein.
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0-dev"
#endif
#define MyAppExe "Fokus.exe"

[Setup]
AppId={{5C1F7A2E-9D3B-4E8A-B6C4-7F2E1A9D0303}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=Echoblend
; Laufende App erkennen (Name wie in fokus.py App.__init__) und erst schliessen lassen
AppMutex=Echoblend.Fokus
DefaultDirName={autopf}\{#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist\installer
OutputBaseFilename={#MyAppName}-Setup-{#MyAppVersion}
SetupIconFile=..\assets\fokus.ico
UninstallDisplayIcon={app}\{#MyAppExe}
UninstallDisplayName={#MyAppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes

[Languages]
Name: "german"; MessagesFile: "compiler:Languages\German.isl"

[Messages]
german.FinishedLabel=Fokus ist installiert.%n%nSpotify-Steuerung einmalig einrichten:%n1. In Chrome chrome://extensions öffnen und oben rechts den Entwicklermodus einschalten.%n2. „Entpackte Erweiterung laden“ und diesen Ordner wählen:%n   {app}\chrome-ext%n3. Den Spotify-Tab neu laden.

[Tasks]
Name: "startupicon"; Description: "Beim Windows-Start automatisch starten"; GroupDescription: "{cm:AdditionalIcons}"
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\{#MyAppExe}"; DestDir: "{app}"; Flags: ignoreversion
; Chrome laedt die Erweiterung aus diesem Ordner; bei Updates neu laden genuegt
Source: "..\chrome-ext\*"; DestDir: "{app}\chrome-ext"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"
Name: "{autoprograms}\Fokus – Chrome-Erweiterung (Ordner)"; Filename: "{app}\chrome-ext"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Tasks: desktopicon
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Tasks: startupicon

[Run]
Filename: "{app}\{#MyAppExe}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
Filename: "{app}\chrome-ext"; Description: "Erweiterungsordner öffnen"; Flags: shellexec postinstall skipifsilent unchecked
