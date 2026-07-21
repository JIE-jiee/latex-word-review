; Inno Setup 7 recipe for a per-user Windows installation.
; SourceDir must be the exact PyInstaller onedir also used by the portable ZIP.

#ifndef AppVersion
  #error AppVersion must be supplied by scripts/build-windows.ps1
#endif
#ifndef SourceDir
  #error SourceDir must be supplied by scripts/build-windows.ps1
#endif
#ifndef OutputDir
  #error OutputDir must be supplied by scripts/build-windows.ps1
#endif

#define AppName "LaTeX Word Review"
#define AppPublisher "LaTeX Word Review contributors"
#define AppExeName "LatexWordReview.exe"
#define AppUrl "https://github.com/JIE-jiee/latex-word-review"

[Setup]
AppId={{FA0A89F6-357A-4C99-879A-A49B4576BB77}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppUrl}
AppSupportURL={#AppUrl}/issues
AppUpdatesURL={#AppUrl}/releases
DefaultDirName={localappdata}\Programs\LatexWordReview
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
AllowNoIcons=yes
OutputDir={#OutputDir}
OutputBaseFilename=latex-word-review-{#AppVersion}-windows-x64-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes
CloseApplications=no
RestartApplications=no
ChangesAssociations=no
ChangesEnvironment=no
UsePreviousAppDir=yes
UsePreviousGroup=yes
Uninstallable=yes
CreateUninstallRegKey=yes
UninstallDisplayName={#AppName} {#AppVersion}
UninstallDisplayIcon={app}\{#AppExeName}
LicenseFile={#SourceDir}\_internal\LICENSE
InfoAfterFile={#SourceDir}\_internal\THIRD_PARTY_NOTICES.md

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Tasks]
Name: "desktopicon"; Description: "在桌面创建 LaTeX Word Review 快捷方式"; Flags: checkedonce

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "立即启动 LaTeX Word Review"; Flags: nowait postinstall skipifsilent
