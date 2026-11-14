; Inno Setup script – makes AndySDR-Setup.exe (Start menu + desktop shortcuts + uninstaller)
; the build passes the real version: ISCC /DAppVer=1.5.0 installer.iss
#ifndef AppVer
  #define AppVer "1.5.0"
#endif

[Setup]
AppId={{6C1E2A47-1D3B-4E7A-9C11-A4D5D0B2045A}
AppName=AndySDR
AppVersion={#AppVer}
AppPublisher=AndySDR
DefaultDirName={autopf}\AndySDR
DefaultGroupName=AndySDR
OutputBaseFilename=AndySDR-Setup
SetupIconFile=andysdr\web\andysdr.ico
UninstallDisplayIcon={app}\AndySDR.exe
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequiredOverridesAllowed=dialog
; silent self-update (AndySDR runs this installer itself): close the running program, then start it again
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: checkedonce
Name: "firewall"; Description: "Allow other PCs on my network to use AndySDR (Windows Firewall rule, private networks)"; Flags: checkedonce

[Files]
Source: "dist\AndySDR\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
; Microsoft Visual C++ runtime (needed by SatDump) - installed silently if it is missing
Source: "redist\vc_redist.x64.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall skipifsourcedoesntexist

[Dirs]
Name: "{app}\decoders"; Permissions: users-modify

[Icons]
Name: "{group}\AndySDR"; Filename: "{app}\AndySDR.exe"
Name: "{group}\AndySDR (server only)"; Filename: "{app}\AndySDR.exe"; Parameters: "--server"
Name: "{group}\Decoders folder"; Filename: "{app}\decoders"
Name: "{autodesktop}\AndySDR"; Filename: "{app}\AndySDR.exe"; Tasks: desktopicon

[Run]
Filename: "{tmp}\vc_redist.x64.exe"; Parameters: "/install /quiet /norestart"; StatusMsg: "Installing Visual C++ runtime for the decoders..."; Flags: waituntilterminated skipifdoesntexist
Filename: "netsh"; Parameters: "advfirewall firewall add rule name=""AndySDR"" dir=in action=allow program=""{app}\AndySDR.exe"" profile=private"; Flags: runhidden; Tasks: firewall
Filename: "{app}\AndySDR.exe"; Description: "Start AndySDR now"; Flags: nowait postinstall skipifsilent
; after an in-app update ( /RELAUNCH=1 ) start AndySDR again, with --server / --browser if it was started that way
Filename: "{app}\AndySDR.exe"; Parameters: "{param:LAUNCHARGS|}"; Flags: nowait; Check: RelaunchAfterUpdate

[UninstallRun]
Filename: "netsh"; Parameters: "advfirewall firewall delete rule name=""AndySDR"""; Flags: runhidden; RunOnceId: "fw"

[Code]
function RelaunchAfterUpdate: Boolean;
begin
  Result := ExpandConstant('{param:RELAUNCH|0}') = '1';
end;
