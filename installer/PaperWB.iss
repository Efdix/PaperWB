; PaperWB 安装向导脚本（Inno Setup 7；兼容 6.4+。中文语言文件需 6.4.0+ 版本）
;
; 由 installer/build_installer.ps1 调用编译：
;   ISCC /DMyAppVersion=<版本号> installer\PaperWB.iss          → 预置模型完整版
;   ISCC /DMyAppVersion=<版本号> /DLite installer\PaperWB.iss   → 精简版（不预置模型，
;                                                                 用户首次解析时应用联网下载）
; 产物: installer\Output\PaperWB-Setup-<版本号>[-lite].exe
;
; 前置产物：
;   ..\dist\PaperWB\*            PyInstaller onedir 输出（build_installer.ps1 自动构建，
;                                打包前已清理运行时残留 config.json/logs/data）
;   models_cache\hub\*           预置 Docling 模型（installer/stage_models.py 生成，约 505 MB）
;   ..\PaperWB.jpg               应用图标源图（make_icon.py 自动裁切）
;   ..\assets\PaperWB.ico        应用图标（installer/make_icon.py 生成）
;   ..\LICENSE                   MIT 许可证
;
; 安装布局（应用侧 src/core/docling_parser.py 按 <安装目录>\models\hub 检测）：
;   {app}\PaperWB.exe + {app}\_internal\ + {app}\models\hub\models--*
; 便携化布局：{app}\config.json（安装时写入 data_root）、{app}\logs\、{app}\data\（数据根目录，
; 向导可选其他位置）均为运行时产物，不在安装日志内，卸载时按用户选择清理。

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
; 精简版（/DLite）：不预置模型，产物名加 -lite 后缀
#ifdef Lite
OutputBaseFilename=PaperWB-Setup-{#MyAppVersion}-lite
#else
OutputBaseFilename=PaperWB-Setup-{#MyAppVersion}
#endif
Compression=lzma2/max
SolidCompression=yes
; 排查用户安装问题时可让用户发回 %TEMP%\Setup Log*.txt
SetupLogging=yes

[Languages]
Name: "chinesesimplified"; MessagesFile: "lang\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
chinesesimplified.ModelsDesc=预置离线解析模型（约 500 MB，推荐；取消后首次解析 PDF 时联网下载）
chinesesimplified.TypeFull=完整安装（含可选组件）
chinesesimplified.TypeCompact=精简安装
chinesesimplified.TypeCustom=自定义（自行勾选组件）
chinesesimplified.ModelsGroup=解析模型
chinesesimplified.DataDirTitle=数据与缓存目录
chinesesimplified.DataDirDesc=论文、阅读缓存、写作知识库和草稿将集中保存在该目录，后续可在应用设置中更改。
chinesesimplified.LaunchNow=立即运行 PaperWB(&L)
; 自检文案：完整版提及离线模型，精简版（/DLite）只提核心组件
#ifdef Lite
chinesesimplified.SelftestCheckbox=运行安装自检（验证核心组件，约 1 分钟）
#else
chinesesimplified.SelftestCheckbox=运行安装自检（验证核心组件与离线模型，约 1 分钟）
#endif
chinesesimplified.SelftestRunning=正在运行安装自检，请稍候……
chinesesimplified.SelftestPass=安装自检通过，PaperWB 已就绪！
chinesesimplified.SelftestFailFmt=安装自检未通过（退出码 %d）。
chinesesimplified.SelftestLogHint=请把以下日志发给开发者远程排查：
chinesesimplified.UninstallConfigQuestion=是否同时删除配置与日志（含 API Key）？选"否"保留，下次安装无需重新配置。
english.ModelsDesc=Bundle offline parsing models (~500 MB, recommended; uncheck to download on first parse)
english.TypeFull=Full installation (includes optional components)
english.TypeCompact=Compact installation
english.TypeCustom=Custom (choose components yourself)
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

; 完整版才注册"预置离线解析模型"组件。必须显式给 [Types] 并把组件挂上
; full/compact：无 [Types] 节时 Inno 的默认组件选择不含该组件，静默安装会
; 整体跳过模型（实测），组件页用户手滑去勾的语义用 custom 类型保留
[Types]
Name: "full"; Description: "{cm:TypeFull}"
Name: "compact"; Description: "{cm:TypeCompact}"
Name: "custom"; Description: "{cm:TypeCustom}"; Flags: iscustom

#ifndef Lite
[Components]
Name: "models"; Description: "{cm:ModelsDesc}"; Types: full compact
#endif

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\PaperWB\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
#ifndef Lite
Source: "models_cache\hub\*"; DestDir: "{app}\models\hub"; Components: models; Flags: ignoreversion recursesubdirs createallsubdirs
#endif

[Icons]
; 快捷方式带 AppUserModelID（Inno 6 [Icons] 参数，非 [Setup] 指令），
; 与 main.py 的 SetCurrentProcessExplicitAppUserModelID 保持一致，
; 保证任务栏/固定快捷方式与运行中窗口始终归组到同一应用图标
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; AppUserModelID: "Efdix.PaperWB"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon; AppUserModelID: "Efdix.PaperWB"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchNow}"; Flags: nowait postinstall skipifsilent

[Code]
var
  SelftestCheckbox: TNewCheckbox;
  OriginalNextClick: TNotifyEvent;
  DataDirPage: TInputDirWizardPage;

// 前置声明：InitializeWizard 中先引用，过程体在下方定义
procedure NextClickHook(Sender: TObject); forward;

// ---- 配置文件路径（便携模式：{app}\config.json；回退旧版 %APPDATA%\PaperWB\） ----

// 目录可写探测（Inno 7 无内置 DirIsWritable，用试探文件兼容 6.4+/7）
function DirWritable(const Dir: String): Boolean;
var
  TestFile: String;
begin
  Result := False;
  if not DirExists(Dir) then
    if not ForceDirectories(Dir) then
      exit;
  TestFile := AddBackslash(Dir) + 'paperwb_write_probe.tmp';
  Result := SaveStringToFile(TestFile, 'probe', False);
  if Result then
    DeleteFile(TestFile);
end;

function GetPortableConfigPath(): String;
begin
  Result := ExpandConstant('{app}\config.json');
end;

function GetAppDataConfigPath(): String;
begin
  Result := ExpandConstant('{userappdata}\PaperWB\config.json');
end;

// 从指定 config.json 解析 data_root（未设置返回空串）
function ReadDataRootFrom(const ConfigPath: String): String;
var
  AnsiContent: AnsiString;
  Content: String;
  KeyPos, ValueStart, ValueEnd: Integer;
begin
  Result := '';
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

// 读 %APPDATA% 旧版配置的 data_root。不展开 {app}，InitializeWizard 阶段可安全调用
//（{app} 常量要到走过安装位置页才初始化，向导初始化时展开会直接抛内部错误）
function GetAppDataDataRoot(): String;
begin
  Result := ReadDataRootFrom(GetAppDataConfigPath());
end;

// 读 data_root：{app}\config.json（便携，现行为主）优先，其次 %APPDATA% 旧版位置。
// 只在 {app} 已初始化的时机调用（数据目录页显示后/ssPostInstall/卸载）
function GetConfiguredDataRoot(): String;
begin
  Result := ReadDataRootFrom(GetPortableConfigPath());
  if Result = '' then
    Result := ReadDataRootFrom(GetAppDataConfigPath());
end;

// 把 data_root 写入配置文件（保留其它字段）。
// {app} 可写 → 写 {app}\config.json（升级安装时先从 %APPDATA% 旧配置整体拷入，
// 保住 API Key）；{app} 不可写（如装进 Program Files）→ 退 %APPDATA%
procedure SetConfiguredDataRoot(const DataRoot: String);
var
  ConfigPath: String;
  AnsiContent: AnsiString;
  Content: String;
  KeyPos, ValueStart, ValueEnd: Integer;
  Escaped: String;
begin
  if DirWritable(ExpandConstant('{app}')) then
  begin
    ConfigPath := GetPortableConfigPath();
    if (not FileExists(ConfigPath)) and FileExists(GetAppDataConfigPath()) then
      CopyFile(GetAppDataConfigPath(), ConfigPath, False);
  end
  else
    ConfigPath := GetAppDataConfigPath();
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
  // 数据与缓存目录选择页（位于安装位置页之后）。默认值不能在这里展开 {app}
  // （InitializeWizard 阶段 {app} 尚未初始化，会抛 "expand the app constant
  // before it was initialized"），留空后由 CurPageChanged 在到达本页时填充。
  DataDirPage := CreateInputDirPage(wpSelectDir,
    ExpandConstant('{cm:DataDirTitle}'),
    ExpandConstant('{cm:DataDirDesc}'),
    '数据目录：', False, '');
  DataDirPage.Add('');
  // 只读 %APPDATA% 旧版配置做升级预填（不展开 {app}）；便携配置到 CurPageChanged 再读
  DataDirPage.Values[0] := GetAppDataDataRoot();

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

// 到达数据目录页时定默认值：此刻 {app} 已随安装位置页提交而初始化。
// 优先级：便携配置（升级重装，现行为主）> %APPDATA% 旧配置预填 > 默认 {app}\data；
// 装进不可写目录则退 %LOCALAPPDATA%
procedure CurPageChanged(CurPageID: Integer);
var
  Portable: String;
begin
  if CurPageID = DataDirPage.ID then
  begin
    Portable := GetConfiguredDataRoot();
    if Portable <> '' then
      DataDirPage.Values[0] := Portable
    else if DataDirPage.Values[0] = '' then
    begin
      if DirWritable(ExpandConstant('{app}')) then
        DataDirPage.Values[0] := AddBackslash(ExpandConstant('{app}')) + 'data'
      else
        DataDirPage.Values[0] := ExpandConstant('{localappdata}\PaperWB\data');
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    // 静默安装（/VERYSILENT）不显示向导页，值仍为空 → 按默认语义落 {app}\data
    if DataDirPage.Values[0] = '' then
      DataDirPage.Values[0] := ExpandConstant('{app}\data');
    SetConfiguredDataRoot(DataDirPage.Values[0]);
  end;
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
             ExpandConstant('{app}\logs\paperwb_selftest.log') + #13#10 +
             ExpandConstant('{app}\logs\error.log');
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
var
  DataRoot: String;
begin
  // 卸载后询问是否连配置一起清（默认保留 API Key，重装免配置）。
  // 静默卸载（/VERYSILENT）不弹框：直接按安全默认保留配置。
  if (CurUninstallStep = usPostUninstall) and (not UninstallSilent()) then
  begin
    if MsgBox(ExpandConstant('{cm:UninstallConfigQuestion}'), mbConfirmation,
              MB_YESNO or MB_DEFBUTTON2) = IDYES then
    begin
      // 先读 data_root 再删配置（config.json 是运行时产物，不在安装日志内，
      // 卸载后仍留在 {app}，需手动清）。日志同样随程序走。
      DataRoot := GetConfiguredDataRoot();
      DelTree(GetPortableConfigPath(), False, True, True);       // {app}\config.json
      DelTree(ExpandConstant('{app}\logs'), True, True, True);   // {app}\logs\
      DelTree(ExpandConstant('{app}\data'), True, True, True);   // 默认数据根 {app}\data\
      DelTree(ExpandConstant('{userappdata}\PaperWB\logs'), True, True, True);
      DelTree(GetAppDataConfigPath(), False, True, True);        // 旧版/回退位置
      // 用户自定义的数据根：只删应用自建的 .paperwb 与 library，不动其他文件
      if DataRoot <> '' then
      begin
        DelTree(AddBackslash(DataRoot) + '.paperwb', True, True, True);
        DelTree(AddBackslash(DataRoot) + 'library', True, True, True);
      end;
    end;
  end;
end;
