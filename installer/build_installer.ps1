# PaperWB installer one-shot build script
# ---------------------------------------------------------------
# Usage (from repo root or anywhere):
#   powershell -ExecutionPolicy Bypass -File installer\build_installer.ps1
#
# Steps:
#   1. ensure PyInstaller in the conda env (pip install if missing)
#   2. pyinstaller --noconfirm --clean PaperWB.spec   -> dist\PaperWB\
#   3. python installer\stage_models.py               -> installer\models_cache\hub\
#   4. dist\PaperWB\PaperWB.exe --selftest <sample pdf>  (acceptance gate)
#   5. locate ISCC.exe (Inno Setup 7, 兼容 6.4+) and compile installer\PaperWB.iss
#      5a. clean portable runtime artifacts (config.json/logs/data) from dist
#      5b. compile full + lite (/DLite, no bundled models)
# Output: installer\Output\PaperWB-Setup-<version>.exe + PaperWB-Setup-<version>-lite.exe
#
# Switches:
#   -SkipBuild      reuse existing dist\ (skip step 2)
#   -SkipModels     reuse existing installer\models_cache (skip step 3)
#   -SkipSelftest   skip step 4 (not recommended for release)

param(
    [string]$PythonExe = "",
    [string]$Iscc = "",
    [string]$SamplePdf = "",
    [switch]$SkipBuild,
    [switch]$SkipModels,
    [switch]$SkipSelftest
)

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
Push-Location $Repo
$Sw = [System.Diagnostics.Stopwatch]::StartNew()

function Die([string]$Msg) {
    Write-Host "[FAIL] $Msg" -ForegroundColor Red
    exit 1
}
function Step([string]$Msg) {
    Write-Host "`n==> $Msg" -ForegroundColor Cyan
}
function RunSelftestExe([string]$Pdf) {
    # windowed exe（console=False）：PowerShell `&` 对 GUI 程序不等待，
    # $LASTEXITCODE 残留旧值导致验收门形同虚设，还会让后续清理/ISCC 与仍在
    # 运行的自检进程竞态（faulthandler.log 句柄未释放 → ISCC sharing violation）。
    # 必须 Start-Process -Wait 拿真实退出码。
    $SelftestArgs = @("--selftest")
    if ($Pdf) { $SelftestArgs += ('"' + $Pdf + '"') }
    $Proc = Start-Process -FilePath "dist\PaperWB\PaperWB.exe" `
        -ArgumentList $SelftestArgs -Wait -PassThru -NoNewWindow
    return $Proc.ExitCode
}

try {
    # ---------- 0. environment ----------
    Step "Check python environment"
    if (-not $PythonExe) {
        $Candidates = @()
        # 1) explicit env var PAPERWB_PYTHON wins (immune to profile re-init)
        if ($env:PAPERWB_PYTHON) { $Candidates += $env:PAPERWB_PYTHON }
        # 2) running inside `conda activate PaperWB` -> use the active env.
        #    (`powershell -File` re-runs the user profile, whose conda init
        #    resets CONDA_PREFIX to base, so require the env name to match)
        if ($env:CONDA_PREFIX -and $env:CONDA_DEFAULT_ENV -eq "PaperWB") {
            $Candidates += (Join-Path $env:CONDA_PREFIX "python.exe")
        }
        # 3) discover the PaperWB env via conda (no hardcoded install paths)
        $Conda = Get-Command conda -ErrorAction SilentlyContinue
        if ($Conda) {
            try {
                $EnvList = & conda env list --json 2>$null | ConvertFrom-Json
                $Candidates += $EnvList.envs |
                    Where-Object { (Split-Path $_ -Leaf) -eq "PaperWB" } |
                    ForEach-Object { Join-Path $_ "python.exe" }
            } catch {
                Write-Host "conda env discovery failed, falling back to common locations" -ForegroundColor Yellow
            }
        }
        # 4) fall back to common conda install locations (user-scope, no drive letters)
        $CommonRoots = @(
            (Join-Path $HOME "miniforge3"),
            (Join-Path $HOME "miniconda3"),
            (Join-Path $HOME "anaconda3"),
            (Join-Path $env:ProgramData "miniforge3"),
            (Join-Path $env:ProgramData "miniconda3"),
            (Join-Path $env:ProgramData "anaconda3")
        )
        $Candidates += $CommonRoots | ForEach-Object { Join-Path $_ "envs\PaperWB\python.exe" }
        $PythonExe = $Candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    }
    if (-not $PythonExe -or -not (Test-Path $PythonExe)) { Die "python not found; pass -PythonExe or activate the PaperWB env first" }
    Write-Host "python: $PythonExe"

    # the two PowerShell launches (icon step re-runs conda hooks) can clobber
    # inherited env vars, so pin every child call to the resolved interpreter
    $env:PAPERWB_PYTHON = $PythonExe

    try {
        & $PythonExe -c "import PyInstaller" 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "no PyInstaller" }
        Write-Host "PyInstaller: present"
    } catch {
        Step "Install PyInstaller (tsinghua mirror)"
        & $PythonExe -m pip install pyinstaller -i https://pypi.tuna.tsinghua.edu.cn/simple
        if ($LASTEXITCODE -ne 0) { Die "pip install pyinstaller failed" }
    }

    # ---------- 0.5. application icon ----------
    Step "Generate application icon from PaperWB.jpg"
    & $PythonExe installer\make_icon.py
    if ($LASTEXITCODE -ne 0) { Die "icon generation failed" }
    if (-not (Test-Path "assets\PaperWB.ico")) {
        Die "assets\PaperWB.ico missing after icon generation"
    }

    # ---------- 1. pyinstaller build ----------
    if (-not $SkipBuild) {
        Step "PyInstaller build (onedir, 15-40 min for torch-sized app)"
        & $PythonExe -m PyInstaller --noconfirm --clean PaperWB.spec
        if ($LASTEXITCODE -ne 0) { Die "pyinstaller failed (exit $LASTEXITCODE)" }
    } else {
        Write-Host "skipped (-SkipBuild)"
    }
    if (-not (Test-Path "dist\PaperWB\PaperWB.exe")) {
        Die "dist\PaperWB\PaperWB.exe missing - run without -SkipBuild first"
    }

    # ---------- 2. stage bundled models ----------
    if (-not $SkipModels) {
        Step "Stage Docling models (~505 MB)"
        & $PythonExe installer\stage_models.py
        if ($LASTEXITCODE -ne 0) { Die "stage_models failed" }
    } else {
        Write-Host "skipped (-SkipModels)"
    }

    # ---------- 3. dist selftest acceptance ----------
    if (-not $SkipSelftest) {
        if ($SamplePdf -eq "" -or -not (Test-Path $SamplePdf)) {
            $found = Get-ChildItem -Path "test" -Filter *.pdf -File -ErrorAction SilentlyContinue |
                     Select-Object -First 1
            $SamplePdf = if ($found) { $found.FullName } else { "" }
        }
        # 指向本机已预热的模型缓存（models/hf_cache/hub），自检不重新下载 500MB 模型
        $HubCache = Join-Path $Repo "models\hf_cache\hub"
        if (Test-Path $HubCache) { $env:HF_HUB_CACHE = $HubCache }
        Step "Selftest dist build$(if ($SamplePdf) { " with sample: $SamplePdf" })"
        $Ec = RunSelftestExe $SamplePdf
        # 便携化后自检日志在 exe 同级 logs/ 下；%TEMP% 兼容旧包排查
        $Log = "dist\PaperWB\logs\paperwb_selftest.log"
        if (-not (Test-Path $Log)) {
            $Log = Join-Path $env:TEMP "paperwb_selftest.log"
        }
        if (Test-Path $Log) {
            Write-Host "----- $Log -----"
            Get-Content $Log | ForEach-Object {
                if ($_.StartsWith("[FAIL]")) { Write-Host $_ -ForegroundColor Red }
                else { Write-Host $_ }
            }
        }
        if ($Ec -ne 0) { Die "selftest failed (exit $Ec) - see log above" }

        # ---- 3b. 完整版安装布局离线自检（bundled models 模拟） ----
        # dist 本身没有 models/hub，正常自检走在线缓存路径，测不到安装版
        # HF_HUB_OFFLINE=1 的离线加载。把 staged 模型拷进 dist 触发 bundled
        # 检测（docling_parser 重定向 HF_HUB_CACHE 并强制离线），确保离线 refs
        # 齐全（docling 按 tag 请求模型仓，staging 必须带 refs/<tag>）。
        $StagedHub = "installer\models_cache\hub"
        if (Test-Path $StagedHub) {
            Step "Selftest bundled-models offline layout (simulates installed full version)"
            if (Test-Path "dist\PaperWB\models") { Remove-Item "dist\PaperWB\models" -Recurse -Force }
            Copy-Item $StagedHub "dist\PaperWB\models\hub" -Recurse -Force
            # 清掉上一步注入的 HF 环境变量：bundled 检测用 setdefault 重定向，
            # 显式 env 会压过它，导致测的仍是外部缓存而非预置布局
            Remove-Item Env:HF_HUB_CACHE -ErrorAction SilentlyContinue
            Remove-Item Env:HF_HUB_OFFLINE -ErrorAction SilentlyContinue
            $EcOffline = RunSelftestExe $SamplePdf
            $LogOffline = "dist\PaperWB\logs\paperwb_selftest.log"
            if (Test-Path $LogOffline) {
                Write-Host "----- $LogOffline -----"
                Get-Content $LogOffline | ForEach-Object {
                    if ($_.StartsWith("[FAIL]")) { Write-Host $_ -ForegroundColor Red }
                    else { Write-Host $_ }
                }
            }
            Remove-Item "dist\PaperWB\models" -Recurse -Force
            if ($EcOffline -ne 0) {
                Die "bundled-models offline selftest failed (exit $EcOffline) - staging refs/layout broken"
            }
        } else {
            Write-Host "installer\models_cache\hub missing - skip offline selftest (stage_models first)"
        }
    } else {
        Write-Host "skipped (-SkipSelftest)"
    }

    # ---------- 4. locate Inno Setup ----------
    Step "Locate Inno Setup compiler (ISCC.exe)"
    $IsccCandidates = @()
    # 1) ISCC on PATH (e.g. installed with "Add to PATH" or a portable copy)
    $IsccOnPath = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($IsccOnPath) { $IsccCandidates += $IsccOnPath.Source }
    # 2) Inno Setup uninstall registry entry (both 32/64-bit views, matches 6.x and 7.x)
    $UninstallRoots = @(
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
        "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    )
    foreach ($Root in $UninstallRoots) {
        Get-ChildItem $Root -ErrorAction SilentlyContinue | ForEach-Object {
            $Entry = Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue
            if ($Entry.DisplayName -like "Inno Setup*" -and $Entry.InstallLocation) {
                $IsccCandidates += (Join-Path $Entry.InstallLocation "ISCC.exe")
            }
        }
    }
    # 3) common install locations (Inno Setup 7 优先，兼容 6；user-scope, no drive letters)
    $IsccCandidates += @(
        (Join-Path ${env:ProgramFiles} "Inno Setup 7\ISCC.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 7\ISCC.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 7\ISCC.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe")
    )
    $Iscc = $IsccCandidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    if (-not $Iscc) {
        Die ("Inno Setup (6.4+ / 7) not found. Install it (one time), then re-run with -SkipBuild -SkipModels -SkipSelftest:`n" +
             "  winget install -e --id JRSoftware.InnoSetup.7`n" +
             "  or download from https://jrsoftware.org/isdl.php`n" +
             "  (or pass the ISCC.exe path via -Iscc)")
    }
    Write-Host "ISCC: $Iscc"

    # ---------- 4.5 clean portable runtime artifacts from dist ----------
    # 自检会在 exe 同级生成 config.json/logs/data（便携化布局），不能打进安装包；
    # models/hub 属安装器组件（ISS 从 models_cache 单独收），dist 内临时拷贝同样剔除
    Step "Clean portable runtime artifacts from dist"
    foreach ($Rt in @("dist\PaperWB\config.json", "dist\PaperWB\logs", "dist\PaperWB\data",
                      "dist\PaperWB\models", "dist\PaperWB\.paperwb_write_probe")) {
        if (Test-Path $Rt) { Remove-Item $Rt -Recurse -Force }
    }

    # ---------- 5. compile installers (full + lite) ----------
    $Ver = (Select-String -Path main.py -Pattern 'setApplicationVersion\("([^"]+)"\)')[0].
           Matches[0].Groups[1].Value
    Step "Compile full installer (version $Ver, LZMA2 compression may take a while)"
    & $Iscc "/DMyAppVersion=$Ver" (Join-Path $PSScriptRoot "PaperWB.iss")
    if ($LASTEXITCODE -ne 0) { Die "ISCC failed (exit $LASTEXITCODE)" }

    $Out = Join-Path $PSScriptRoot "Output\PaperWB-Setup-$Ver.exe"
    if (-not (Test-Path $Out)) { Die "installer output missing: $Out" }
    $Mb = [math]::Round((Get-Item $Out).Length / 1MB)

    Step "Compile lite installer (no bundled models)"
    & $Iscc "/DMyAppVersion=$Ver" "/DLite" (Join-Path $PSScriptRoot "PaperWB.iss")
    if ($LASTEXITCODE -ne 0) { Die "ISCC (lite) failed (exit $LASTEXITCODE)" }
    $OutLite = Join-Path $PSScriptRoot "Output\PaperWB-Setup-$Ver-lite.exe"
    if (-not (Test-Path $OutLite)) { Die "lite installer output missing: $OutLite" }
    $MbLite = [math]::Round((Get-Item $OutLite).Length / 1MB)

    $Sw.Stop()
    Write-Host ""
    Write-Host ("DONE in {0:mm} min:" -f $Sw.Elapsed) -ForegroundColor Green
    Write-Host ("  full: {0} ({1} MB)" -f $Out, $Mb) -ForegroundColor Green
    Write-Host ("  lite: {0} ({1} MB)" -f $OutLite, $MbLite) -ForegroundColor Green
    exit 0
} finally {
    Pop-Location
}
