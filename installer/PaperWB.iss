; PaperWB 安装向导脚本（Inno Setup 6.4+，中文语言文件需 6.4.0+ 版本）
;
; 由 installer/build_installer.ps1 调用编译：
;   ISCC /DMyAppVersion=<版本号> installer\PaperWB.iss
; 产物: installer\Output\PaperWB-Setup-<版本号>.exe
;
; 前置产物：
;   ..\dist\PaperWB\*            PyInstaller onedir 输出（build_installer.ps1 自动构建）
;   models_cache\hub\*           预置 Docling 模型（installer/stage_models.py 生成，约 505 MB）
;   ..\PaperWB.jpg               应用图标源图（make_icon.py 自动裁切）
;   ..\assets\PaperWB.ico        应用图标（installer/make_icon.py 生成）
;   ..\LICENSE                   MIT 许可证
;
; 安装布局（应用侧 src/core/docling_parser.py 按 <安装目录>\models\hub 检测）：
;   {app}\PaperWB.exe + {app}\_internal\ + {app}\models\hub\models--*

#define MyAppName "PaperWB"
#define MyAppNameZh "PaperWB — AI 论文解读助手"
#ifndef MyAppVersion
#define MyAppVersion "1.0.0"
#endif
#define MyAppPublisher "Efdix"
#define MyAppExeName "PaperWB.exe"
#define MyAppId "{{95713130-07B5-49A9-8E3F-06A57FBC24A0}"

[Setup]
AppId={#MyAppId}
AppName={#MyAppNameZh}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
UninstallDisplayName={#MyAppNameZh}
; 默认当前用户安装（免管理员、目录可写=模型缓存可写），向导可选"所有用户"
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DisableProgramGroupPage=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\LICENSE
SetupIconFile=..\assets\PaperWB.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
OutputDir=Output
OutputBaseFilename=PaperWB-Setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
; 排查用户安装问题时可让用户发回 %TEMP%\Setup Log*.txt
SetupLogging=yes

[Languages]
Name: "chinesesimplified"; MessagesFile: "lang\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
chinesesimplified.ModelsDesc=预置离线解析模型（约 500 MB，推荐；取消后首次解析 PDF 时联网下载）
chinesesimplified.ModelsGroup=解析模型
chinesesimplified.DataDirTitle=数据与缓存目录
chinesesimplified.DataDirDesc=论文、阅读缓存、写作知识库和草稿将集中保存在该目录，后续可在应用设置中更改。
chinesesimplified.LaunchNow=立即运行 PaperWB(&L)
chinesesimplified.SelftestCheckbox=运行安装自检（验证核心组件与离线模型，约 1 分钟）
chinesesimplified.SelftestRunning=正在运行安装自检，请稍候……
chinesesimplified.SelftestPass=安装自检通过，PaperWB 已就绪！
chinesesimplified.SelftestFailFmt=安装自检未通过（退出码 %d）。
chinesesimplified.SelftestLogHint=请把以下日志发给开发者远程排查：
chinesesimplified.UninstallConfigQuestion=是否同时删除配置与日志（含 API Key）？选"否"保留，下次安装无需重新配置。
english.ModelsDesc=Bundle offline parsing models (~500 MB, recommended; uncheck to download on first parse)
english.ModelsGroup=Parsing models
english.DataDirTitle=Data & cache directory
english.DataDirDesc=Papers, reading caches, writing knowledge base and drafts will be stored here. Can be changed later in app settings.
english.LaunchNow=Launch PaperWB(&L)
english.SelftestCheckbox=Run installation self-test (verify core components, ~1 min)
english.SelftestRunning=Running installation self-test, please wait...
english.SelftestPass=Self-test passed. PaperWB is ready!
english.SelftestFailFmt=Self-test failed (exit code %d).
english.SelftestLogHint=Please send the following logs to the developer:
english.UninstallConfigQuestion=Also delete settings and logs (including API keys)? Choose No to keep them for the next installation.

[Components]
Name: "models"; Description: "{cm:ModelsDesc}"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\PaperWB\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "models_cache\hub\*"; DestDir: "{app}\models\hub"; Components: models; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchNow}"; Flags: nowait postinstall skipifsilent

[Code]
var
  SelftestCheckbox: TNewCheckbox;
  OriginalNextClick: TNotifyEvent;
  DataDirPage: TInputDirWizardPage;

// 前置声明：InitializeWizard 中先引用，过程体在下方定义
procedure NextClickHook(Sender: TObject); forward;

// 读取 %APPDATA%\PaperWB\config.json 中的 data_root 字段（未设置返回空串）
function GetConfiguredDataRoot(): String;
var
  ConfigPath: String;
  AnsiContent: AnsiString;
  Content: String;
  KeyPos, ValueStart, ValueEnd: Integer;
begin
  Result := '';
  ConfigPath := ExpandConstant('{userappdata}\PaperWB\config.json');
  if not FileExists(ConfigPath) then
    exit;
  if not LoadStringFromFile(ConfigPath, AnsiContent) then
    exit;
  Content := Utf8Decode(AnsiContent);
  KeyPos := Pos('"data_root"', Content);
  if KeyPos = 0 then
    exit;
  ValueStart := Pos('"', Copy(Content, KeyPos + 11, MaxInt));
  if ValueStart = 0 then
    exit;
  ValueStart := KeyPos + 10 + ValueStart;
  ValueEnd := Pos('"', Copy(Content, ValueStart + 1, MaxInt));
  if ValueEnd = 0 then
    exit;
  Result := Copy(Content, ValueStart + 1, ValueEnd - 1);
end;

// 把 data_root 写入 %APPDATA%\PaperWB\config.json（保留其它字段）
procedure SetConfiguredDataRoot(const DataRoot: String);
var
  ConfigPath: String;
  AnsiContent: AnsiString;
  Content: String;
  KeyPos, ValueStart, ValueEnd: Integer;
  Escaped: String;
begin
  ConfigPath := ExpandConstant('{userappdata}\PaperWB\config.json');
  Escaped := DataRoot;
  StringChangeEx(Escaped, '\', '\\', True);
  StringChangeEx(Escaped, '"', '\"', True);
  if not FileExists(ConfigPath) then
  begin
    Content := '{' + #13#10 + '  "data_root": "' + Escaped + '"' + #13#10 + '}' + #13#10;
    SaveStringToFile(ConfigPath, Utf8Encode(Content), False);
    exit;
  end;
  if not LoadStringFromFile(ConfigPath, AnsiContent) then
    exit;
  Content := Utf8Decode(AnsiContent);
  KeyPos := Pos('"data_root"', Content);
  if KeyPos = 0 then
  begin
    // 无该字段：在文件末尾补一行（文件以 } 结尾）
    Content := TrimRight(Content);
    if Copy(Content, Length(Content), 1) = '}' then
      Content := Copy(Content, 1, Length(Content) - 1) + ',' + #13#10 +
                 '  "data_root": "' + Escaped + '"' + #13#10 + '}';
    SaveStringToFile(ConfigPath, Utf8Encode(Content), False);
    exit;
  end;
  ValueStart := Pos('"', Copy(Content, KeyPos + 11, MaxInt));
  if ValueStart = 0 then
    exit;
  ValueStart := KeyPos + 10 + ValueStart;
  ValueEnd := Pos('"', Copy(Content, ValueStart + 1, MaxInt));
  if ValueEnd = 0 then
    exit;
  Content := Copy(Content, 1, ValueStart) + Escaped + Copy(Content, ValueStart + ValueEnd, MaxInt);
  SaveStringToFile(ConfigPath, Utf8Encode(Content), False);
end;

procedure InitializeWizard();
begin
  // 数据与缓存目录选择页（位于安装位置页之后）
  DataDirPage := CreateInputDirPage(wpSelectDir,
    ExpandConstant('{cm:DataDirTitle}'),
    ExpandConstant('{cm:DataDirDesc}'),
    '数据目录：', False, '');
  DataDirPage.Add('');
  DataDirPage.Values[0] := GetConfiguredDataRoot();
  if DataDirPage.Values[0] = '' then
    DataDirPage.Values[0] := ExpandConstant('{userdocs}\PaperWB_Data');

  // 完成页追加「运行安装自检」勾选框（位于 RunList 下方）
  SelftestCheckbox := TNewCheckbox.Create(WizardForm);
  SelftestCheckbox.Parent := WizardForm.FinishedPage;
  SelftestCheckbox.Caption := ExpandConstant('{cm:SelftestCheckbox}');
  SelftestCheckbox.Left := WizardForm.RunList.Left;
  SelftestCheckbox.Top := WizardForm.RunList.Top + WizardForm.RunList.Height + ScaleY(6);
  SelftestCheckbox.Width := WizardForm.FinishedPage.Width - SelftestCheckbox.Left - ScaleX(8);
  SelftestCheckbox.Checked := False;
  SelftestCheckbox.Visible := True;

  // 拦截「完成」点击：先按勾选执行自检，再走原逻辑（关闭向导/启动应用）
  OriginalNextClick := WizardForm.NextButton.OnClick;
  WizardForm.NextButton.OnClick := @NextClickHook;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    SetConfiguredDataRoot(DataDirPage.Values[0]);
end;

procedure RunSelftest();
var
  ResultCode: Integer;
  Msg: String;
begin
  SelftestCheckbox.Caption := ExpandConstant('{cm:SelftestRunning}');
  SelftestCheckbox.Enabled := False;
  WizardForm.Repaint();
  if Exec(ExpandConstant('{app}\{#MyAppExeName}'), '--selftest',
          ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    if ResultCode = 0 then
      MsgBox(ExpandConstant('{cm:SelftestPass}'), mbInformation, MB_OK)
    else
    begin
      Msg := Format(ExpandConstant('{cm:SelftestFailFmt}'), [ResultCode]) + #13#10#13#10 +
             ExpandConstant('{cm:SelftestLogHint}') + #13#10 +
             '%TEMP%\paperwb_selftest.log' + #13#10 +
             '%APPDATA%\PaperWB\error.log';
      MsgBox(Msg, mbError, MB_OK);
    end;
  end
  else
    MsgBox(SysErrorMessage(ResultCode), mbError, MB_OK);
end;

procedure NextClickHook(Sender: TObject);
begin
  if (WizardForm.CurPageID = wpFinished) and SelftestCheckbox.Checked then
    RunSelftest();
  OriginalNextClick(Sender);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  // 卸载后询问是否连配置一起清（默认保留 API Key，重装免配置）。
  // 静默卸载（/VERYSILENT）不弹框：直接按安全默认保留配置。
  if (CurUninstallStep = usPostUninstall) and (not UninstallSilent()) then
  begin
    if MsgBox(ExpandConstant('{cm:UninstallConfigQuestion}'), mbConfirmation,
              MB_YESNO or MB_DEFBUTTON2) = IDYES then
      DelTree(ExpandConstant('{userappdata}\PaperWB'), True, True, True);
  end;
end;
