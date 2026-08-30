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
#   5. locate ISCC.exe (Inno Setup 6) and compile installer\PaperWB.iss
# Output: installer\Output\PaperWB-Setup-<version>.exe
#
# Switches:
#   -SkipBuild      reuse existing dist\ (skip step 2)
#   -SkipModels     reuse existing installer\models_cache (skip step 3)
#   -SkipSelftest   skip step 4 (not recommended for release)

param(
    [string]$PythonExe = "",
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
        # 3) fall back to the known default env location
        $Candidates += "D:\science\miniforge\envs\PaperWB\python.exe"
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
        Step "Selftest dist build$(if ($SamplePdf) { " with sample: $SamplePdf" })"
        if ($SamplePdf) {
            & "dist\PaperWB\PaperWB.exe" --selftest $SamplePdf
        } else {
            Write-Host "no sample pdf found under test\ - running module-level selftest only"
            & "dist\PaperWB\PaperWB.exe" --selftest
        }
        $Ec = $LASTEXITCODE
        $Log = Join-Path $env:TEMP "paperwb_selftest.log"
        if (Test-Path $Log) {
            Write-Host "----- $Log -----"
            Get-Content $Log | ForEach-Object {
                if ($_.StartsWith("[FAIL]")) { Write-Host $_ -ForegroundColor Red }
                else { Write-Host $_ }
            }
        }
        if ($Ec -ne 0) { Die "selftest failed (exit $Ec) - see log above" }
    } else {
        Write-Host "skipped (-SkipSelftest)"
    }

    # ---------- 4. locate Inno Setup ----------
    Step "Locate Inno Setup compiler (ISCC.exe)"
    $IsccCandidates = @(
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
        "D:\tools\Inno Setup 6\ISCC.exe"
    )
    $Iscc = $IsccCandidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    if (-not $Iscc) {
        Die ("Inno Setup 6 not found. Install it (one time), then re-run with -SkipBuild -SkipModels -SkipSelftest:`n" +
             "  winget install -e --id JRSoftware.InnoSetup`n" +
             "  or download from https://jrsoftware.org/isdl.php")
    }
    Write-Host "ISCC: $Iscc"

    # ---------- 5. compile installer ----------
    $Ver = (Select-String -Path main.py -Pattern 'setApplicationVersion\("([^"]+)"\)')[0].
           Matches[0].Groups[1].Value
    Step "Compile installer (version $Ver, LZMA2 compression may take a while)"
    & $Iscc "/DMyAppVersion=$Ver" (Join-Path $PSScriptRoot "PaperWB.iss")
    if ($LASTEXITCODE -ne 0) { Die "ISCC failed (exit $LASTEXITCODE)" }

    $Out = Join-Path $PSScriptRoot "Output\PaperWB-Setup-$Ver.exe"
    if (-not (Test-Path $Out)) { Die "installer output missing: $Out" }
    $Mb = [math]::Round((Get-Item $Out).Length / 1MB)
    $Sw.Stop()
    Write-Host ""
    Write-Host ("DONE in {0:mm} min: {1} ({2} MB)" -f $Sw.Elapsed, $Out, $Mb) -ForegroundColor Green
    exit 0
} finally {
    Pop-Location
}
