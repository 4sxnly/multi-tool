; Inno Setup script — produces build\Output\MultiTool-Setup-<version>.exe
;   ISCC.exe /DAppVersion=0.2.0 build\installer.iss
;
; Per-user install into %LOCALAPPDATA%\Programs\Multi-Tool:
;   * no admin prompt
;   * plugins\ and bin\ stay writable, so the app can add plugins and update yt-dlp itself
; Everything the app needs (Python runtime, libraries, ffmpeg, yt-dlp, deno) is inside this installer.

#define AppName "Multi-Tool"
#define AppExe "MultiTool.exe"
#ifndef AppVersion
  #define AppVersion "0.2.0"
#endif
#define IconFile AddBackslash(SourcePath) + "..\assets\icon.ico"

[Setup]
AppId={{7E3C1B52-9A4D-4F0E-B6C2-3D8F51A2C9E4}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=Output
OutputBaseFilename=MultiTool-Setup-{#AppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
#if FileExists(IconFile)
SetupIconFile={#IconFile}
#endif

[Types]
Name: "full"; Description: "All tools"
Name: "custom"; Description: "Choose tools"; Flags: iscustom

[Components]
Name: "core"; Description: "{#AppName} app + bundled engines (required)"; Types: full custom; Flags: fixed
Name: "tools"; Description: "Tools"; Types: full
Name: "tools\media"; Description: "Media Downloader — YouTube, TikTok, X and more"; Types: full
Name: "tools\demo"; Description: "Drop Zone demo (for plugin developers)"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "..\dist\MultiTool\*"; DestDir: "{app}"; Components: core; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\bin\*"; DestDir: "{app}\bin"; Components: core; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\plugins\media_downloader.py"; DestDir: "{app}\plugins"; Components: tools\media; Flags: ignoreversion skipifsourcedoesntexist
Source: "..\plugins\dropzone_demo.py"; DestDir: "{app}\plugins"; Components: tools\demo; Flags: ignoreversion skipifsourcedoesntexist

[Dirs]
Name: "{app}\plugins"
Name: "{app}\bin"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; yt-dlp self-updates leave a backup next to the exe
Type: files; Name: "{app}\bin\*.old"
Type: filesandordirs; Name: "{app}\plugins\__pycache__"
