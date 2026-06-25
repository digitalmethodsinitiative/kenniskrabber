; Version is passed in from CI via ISCC /DMyAppVersion=<tag>. The fallback
; below is only used for local/manual compiles.
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0-dev"
#endif

[Setup]
AppName=Kenniskrabber
AppVersion={#MyAppVersion}
AppPublisher=Sal Hagen
DefaultDirName={autopf}\Kenniskrabber
DefaultGroupName=Kenniskrabber
OutputDir=.\InstallerOutput
OutputBaseFilename=Kenniskrabber_Windows_Setup
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64
DisableProgramGroupPage=yes
SetupIconFile=assets\icon.ico

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop icon"; GroupDescription: "Additional icons:"

[Files]
Source: "dist\Kenniskrabber\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Kenniskrabber"; Filename: "{app}\Kenniskrabber.exe"
Name: "{autodesktop}\Kenniskrabber"; Filename: "{app}\Kenniskrabber.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Kenniskrabber.exe"; Description: "Launch Kenniskrabber"; Flags: nowait postinstall skipifsilent