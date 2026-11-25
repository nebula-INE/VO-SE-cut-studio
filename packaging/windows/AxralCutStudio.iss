#define AppVersion GetEnv("AXRAL_PACKAGE_VERSION")
#if AppVersion == ""
  #define AppVersion "0.1.0"
#endif

[Setup]
AppId={{A1D0DAB4-5C11-4D72-93AB-3E0C73B6A1F8}
AppName=Axral Cut Studio
AppVersion={#AppVersion}
AppPublisher=Axral
DefaultDirName={localappdata}\\Programs\Axral Cut Studio
DefaultGroupName=Axral Cut Studio
OutputDir=..\..\release
OutputBaseFilename=AxralCutStudio-Setup-{#AppVersion}-win-x64
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
PrivilegesRequired=lowest
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
UninstallDisplayIcon={app}\AxralCutStudio.exe
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "japanese"; MessagesFile: "compiler:Languages\Japanese.isl"

[Tasks]
Name: "desktopicon"; Description: "デスクトップにショートカットを作成"; GroupDescription: "追加オプション:"; Flags: unchecked

[Files]
Source: "..\..\dist\AxralCutStudio\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Axral Cut Studio"; Filename: "{app}\AxralCutStudio.exe"
Name: "{autodesktop}\Axral Cut Studio"; Filename: "{app}\AxralCutStudio.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\AxralCutStudio.exe"; Description: "Axral Cut Studioを起動"; Flags: postinstall nowait skipifsilent
