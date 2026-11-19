; Inno Setup script — per-user install, no admin rights needed.
#define Name "Steelworks Media Optimiser"
#define Version "1.0.1"
#ifndef Src
  #define Src "..\build\pyinstaller\dist\Steelworks Media Optimiser"
#endif

[Setup]
AppId={{6C4B2E7A-3F1D-4C2B-9A55-5D0E8B7C1F20}
AppName={#Name}
AppVersion={#Version}
AppPublisher=Steelworks
DefaultDirName={localappdata}\Programs\{#Name}
DefaultGroupName={#Name}
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=Steelworks-Media-Optimiser-{#Version}-Windows-Setup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#Name}.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes
ArchitecturesInstallIn64BitMode=x64compatible

[Files]
Source: "{#Src}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#Name}"; Filename: "{app}\{#Name}.exe"
Name: "{autoprograms}\{#Name} - Figma plugin folder"; Filename: "{app}\_internal\plugin"

[Run]
Filename: "{app}\{#Name}.exe"; Description: "Open {#Name}"; Flags: nowait postinstall skipifsilent
