; Inno Setup script for GLEAPP  (https://jrsoftware.org/isdl.php)
;   python packaging\build.py installer      (passes /DAppVer from gleapp\__init__.py)
; Expects a one-folder PyInstaller build at dist\GLEAPP\

#define AppName "GLEAPP"
#ifndef AppVer
  #error Pass the version in with /DAppVer=<version>. packaging\build.py reads it from gleapp\__init__.py and does this for you.
#endif
#define AppPublisher "charpy4n6"
#define AppExe "GLEAPP.exe"

[Setup]
AppId={{9F3B7C21-4D8E-46A2-9B15-2E7A6C0D5F41}
AppName={#AppName}
AppVersion={#AppVer}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=GLEAPP-Setup-{#AppVer}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile=gleapp.ico
UninstallDisplayIcon={app}\{#AppExe}
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequiredOverridesAllowed=dialog
#ifdef SignToolName
; Signs the installer and its uninstaller at compile time with the Sign Tool configured
; under this name in Inno Setup (Tools > Configure Sign Tools). build.py installer
; --sign-tool <name> passes it in. GLEAPP.exe inside must already be signed.
SignTool={#SignToolName}
SignedUninstaller=yes
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"

[Files]
Source: "..\dist\GLEAPP\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

; GLEAPP needs the Edge WebView2 runtime (present by default on Win10 21H2+/Win11).
; If you must support older machines, bundle the Evergreen Bootstrapper here:
; Source: "MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall
; [Run] Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"
