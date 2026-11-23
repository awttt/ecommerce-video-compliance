; 御镜视频审核系统 - Windows 安装包（Inno Setup）
; 由 scripts\build_installer.ps1 调用，一般不用手动编译。
; 升级安装会覆盖旧版本：AppId 固定，安装目录记在上一次的 {app} 里，数据目录不在这里。

#define MyAppName "御镜视频审核系统"
#define MyAppExeName "VideoCompliance.exe"
#ifndef MyAppVersion
  #define MyAppVersion "0.1.2"
#endif
#define MyLangFile AddBackslash(SourcePath) + "ChineseSimplified.isl"

[Setup]
; 这个 GUID 是升级识别的唯一依据，发布后不要改，否则新旧版本会装成两份。
AppId={{4C9E2B71-8D3A-4F62-9A57-2E6C0B1D5F83}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
; 全新安装的默认目录由 GetDefaultDir 决定：优先 D: 到 Z: 里第一个固定磁盘。
; 升级时 UsePreviousAppDir 仍带出上次的目录，页面上可以改。
DefaultDirName={code:GetDefaultDir}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
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
  DRIVE_FIXED = 3;

var
  AskedDelete: Boolean;
  WantDelete: Boolean;

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

function GetDriveTypeW(Root: String): Cardinal;
  external 'GetDriveTypeW@kernel32.dll stdcall';

function GetDefaultDir(Param: String): String;
var
  Letter: Integer;
  Root: String;
begin
  for Letter := Ord('D') to Ord('Z') do
  begin
    Root := Chr(Letter) + ':\';
    if GetDriveTypeW(Root) = DRIVE_FIXED then
    begin
      Result := Root + 'VideoCompliance';
      Exit;
    end;
  end;
  { 这台电脑只有 C 盘时，仍给一个可写目录，页面上可以改。 }
  Result := ExpandConstant('{localappdata}\Programs\VideoCompliance');
end;

function AskDeleteInstaller(): Boolean;
var
  Form: TSetupForm;
  Prompt: TNewStaticText;
  DeleteButton, KeepButton: TNewButton;
begin
  Result := False;
  { 6.6 起宽高在创建时传入，由安装程序自己缩放，之后不能再改。 }
  Form := CreateCustomForm(460, 168, True, True);
  try
    Form.Caption := '安装完成';
    Form.Position := poScreenCenter;
    Prompt := TNewStaticText.Create(Form);
    Prompt.Parent := Form;
    Prompt.Left := ScaleX(16);
    Prompt.Top := ScaleY(16);
    Prompt.Width := Form.ClientWidth - ScaleX(32);
    Prompt.Height := ScaleY(88);
    Prompt.AutoSize := False;
    Prompt.WordWrap := True;
    Prompt.Caption := '安装已完成。要删除安装包吗？' + #13#10 + #13#10 +
      '点「删除」会在安装程序关闭后删掉这个文件：' + #13#10 +
      ExpandConstant('{srcexe}');
    KeepButton := TNewButton.Create(Form);
    KeepButton.Parent := Form;
    KeepButton.Caption := '保留';
    KeepButton.ModalResult := mrNo;
    KeepButton.Cancel := True;
    KeepButton.Width := ScaleX(90);
    KeepButton.Left := Form.ClientWidth - ScaleX(110);
    KeepButton.Top := Form.ClientHeight - ScaleY(46);
    DeleteButton := TNewButton.Create(Form);
    DeleteButton.Parent := Form;
    DeleteButton.Caption := '删除';
    DeleteButton.ModalResult := mrYes;
    DeleteButton.Width := ScaleX(90);
    DeleteButton.Left := KeepButton.Left - ScaleX(100);
    DeleteButton.Top := KeepButton.Top;
    Result := Form.ShowModal() = mrYes;
  finally
    Form.Free;
  end;
end;

procedure ScheduleInstallerDelete();
var
  ScriptPath, Target, Body: String;
  ResultCode: Integer;
begin
  Target := ExpandConstant('{srcexe}');
  ScriptPath := ExpandConstant('{%TEMP}\evc-delete-setup.cmd');
  Body :=
    '@echo off' + #13#10 +
    'set /a N=0' + #13#10 +
    ':again' + #13#10 +
    'del /f /q "' + Target + '"' + #13#10 +
    'if not exist "' + Target + '" goto done' + #13#10 +
    'set /a N+=1' + #13#10 +
    'if %N% GEQ 30 goto done' + #13#10 +
    'ping -n 2 127.0.0.1 >nul' + #13#10 +
    'goto again' + #13#10 +
    ':done' + #13#10 +
    'del /f /q "%~f0"' + #13#10;
  if SaveStringToFile(ScriptPath, Body, False) then
    Exec(ExpandConstant('{cmd}'), '/c "' + ScriptPath + '"', '', SW_HIDE, ewNoWait, ResultCode);
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if AskedDelete or WizardSilent or (CurPageID <> wpFinished) then
    Exit;
  AskedDelete := True;
  WantDelete := AskDeleteInstaller();
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssDone) and WantDelete then
    ScheduleInstallerDelete();
end;
