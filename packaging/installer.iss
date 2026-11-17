; 御镜视频素材审核系统 - Windows 安装包（Inno Setup）
; 由 scripts\build_installer.ps1 调用，一般不用手动编译。
; 升级安装会覆盖旧版本：AppId 固定，安装目录记在上一次的 {app} 里，数据目录不在这里。

#define MyAppName "御镜视频素材审核系统"
#define MyAppExeName "VideoCompliance.exe"
#ifndef MyAppVersion
  #define MyAppVersion "0.1.0"
#endif
#define MyLangFile AddBackslash(SourcePath) + "ChineseSimplified.isl"

[Setup]
; 这个 GUID 是升级识别的唯一依据，发布后不要改，否则新旧版本会装成两份。
AppId={{4C9E2B71-8D3A-4F62-9A57-2E6C0B1D5F83}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
DefaultDirName={localappdata}\Programs\VideoCompliance
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; 不显示目录选择页，升级时沿用上一次的安装位置。
DisableDirPage=yes
UsePreviousAppDir=yes
UsePreviousTasks=yes
PrivilegesRequired=lowest
MinVersion=10.0
OutputDir=..\dist
OutputBaseFilename=VideoCompliance-{#MyAppVersion}-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
AllowNoIcons=yes
; 安装前自动关掉正在运行的程序，否则 exe 被占用无法覆盖。
CloseApplications=yes
RestartApplications=no
SetupLogging=yes
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
SetupIconFile=app.ico

[Languages]
#if FileExists(MyLangFile)
Name: "cn"; MessagesFile: "ChineseSimplified.isl"
#else
Name: "en"; MessagesFile: "compiler:Default.isl"
#endif

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: checkedonce

[InstallDelete]
; 清掉旧的运行库目录，避免残留的 pyc、旧前端文件被新版本误加载。
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\VideoCompliance\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; skipifsilent：静默升级时不自动拉起窗口，由调用方决定何时启动。
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
const
  WebView2Key = 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  WebView2KeyMachine = 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

function WebView2Missing(): Boolean;
var
  Version: String;
begin
  Result := True;
  if RegQueryStringValue(HKCU, WebView2Key, 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0') then
    Result := False
  else if RegQueryStringValue(HKLM64, WebView2KeyMachine, 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0') then
    Result := False
  else if RegQueryStringValue(HKLM, WebView2Key, 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0') then
    Result := False;
end;

function InitializeSetup(): Boolean;
var
  ErrorCode: Integer;
begin
  Result := True;
  if (not WizardSilent) and WebView2Missing() then
  begin
    if MsgBox('未检测到 Microsoft Edge WebView2 运行库，程序需要它才能显示界面。' + #13#10 + #13#10 +
              '点击「是」打开微软官方下载页，装好后再运行本安装包。', mbConfirmation, MB_YESNO) = IDYES then
    begin
      ShellExec('open', 'https://go.microsoft.com/fwlink/p/?LinkId=2124703', '', '', SW_SHOWNORMAL, ewNoWait, ErrorCode);
      Result := False;
    end;
  end;
end;
