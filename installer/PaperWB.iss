; PaperWB 安装向导脚本（Inno Setup 6.4+，中文语言文件需 6.4.0+ 版本）
;
; 由 installer/build_installer.ps1 调用编译：
;   ISCC /DMyAppVersion=<版本号> installer\PaperWB.iss
; 产物: installer\Output\PaperWB-Setup-<版本号>.exe
;
; 前置产物：
;   ..\dist\PaperWB\*            PyInstaller onedir 输出（build_installer.ps1 自动构建）
;   models_cache\hub\*           预置 Docling 模型（installer/stage_models.py 生成，约 505 MB）
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
chinesesimplified.MainDesc=PaperWB 主程序（必装）
chinesesimplified.ModelsDesc=预置离线解析模型（约 500 MB，推荐；取消后首次解析 PDF 时联网下载）
chinesesimplified.ModelsGroup=解析模型
chinesesimplified.LaunchNow=立即运行 PaperWB(&L)
chinesesimplified.SelftestCheckbox=运行安装自检（验证核心组件与离线模型，约 1 分钟）
chinesesimplified.SelftestRunning=正在运行安装自检，请稍候……
chinesesimplified.SelftestPass=安装自检通过，PaperWB 已就绪！
chinesesimplified.SelftestFailFmt=安装自检未通过（退出码 %d）。
chinesesimplified.SelftestLogHint=请把以下日志发给开发者远程排查：
chinesesimplified.UninstallConfigQuestion=是否同时删除配置与日志（含 API Key）？选"否"保留，下次安装无需重新配置。
english.MainDesc=PaperWB main program (required)
english.ModelsDesc=Bundle offline parsing models (~500 MB, recommended; uncheck to download on first parse)
english.ModelsGroup=Parsing models
english.LaunchNow=Launch PaperWB(&L)
english.SelftestCheckbox=Run installation self-test (verify core components, ~1 min)
english.SelftestRunning=Running installation self-test, please wait...
english.SelftestPass=Self-test passed. PaperWB is ready!
english.SelftestFailFmt=Self-test failed (exit code %d).
english.SelftestLogHint=Please send the following logs to the developer:
english.UninstallConfigQuestion=Also delete settings and logs (including API keys)? Choose No to keep them for the next installation.

[Components]
Name: "main"; Description: "{cm:MainDesc}"; Flags: fixed
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

// 前置声明：InitializeWizard 中先引用，过程体在下方定义
procedure NextClickHook(Sender: TObject); forward;

procedure InitializeWizard();
begin
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
