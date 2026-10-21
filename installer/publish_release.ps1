# PaperWB 发布脚本 —— 打 tag + 上传 GitHub Release（更新推送的数据来源）
#
# 应用内「检查更新」读的是 https://api.github.com/repos/Efdix/PaperWB/releases/latest ：
#   tag_name  → 版本号（须与 src/utils/version.py 的 APP_VERSION 一致，脚本会校验）
#   body      → 更新说明（本脚本用 -NotesFile 的内容）
#   assets    → 两个安装包（应用按完整版/精简版自动选对应包，并读 digest 校验 sha256）
#
# 用法（先跑 build_installer.ps1 出包）：
#   installer\publish_release.ps1 -NotesFile CHANGELOG-0.3.0.md
#   installer\publish_release.ps1 -NotesFile notes.md -Draft      # 先传草稿，手动确认后发布
#
# 前置：gh CLI 已登录（gh auth login）。

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$NotesFile,          # 更新说明 Markdown（会成为应用里显示的更新说明）
    [switch]$Draft,              # 传为草稿 Release（不触发 latest，稳妥发布用）
    [switch]$SkipTag,            # 跳过打 tag（tag 已存在时）
    [switch]$AllowDirty,         # 明确允许在工作区脏时发布（不推荐）
    [string]$Repo = "Efdix/PaperWB"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

function Die($msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }
function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

# ---- 1. 版本号校验：以 src/utils/version.py 为唯一来源 ----
Step "Read version from src\utils\version.py"
$VerLine = Select-String -Path (Join-Path $Root "src\utils\version.py") `
    -Pattern 'APP_VERSION\s*=\s*"([^"]+)"'
if (-not $VerLine) { Die "cannot read APP_VERSION from src\utils\version.py" }
$Ver = $VerLine[0].Matches[0].Groups[1].Value
$Tag = "v$Ver"
Write-Host "  version = $Ver  tag = $Tag"

# ---- 2. 安装包就位 ----
$Full = Join-Path $PSScriptRoot "Output\PaperWB-Setup-$Ver.exe"
$Lite = Join-Path $PSScriptRoot "Output\PaperWB-Setup-$Ver-lite.exe"
foreach ($f in @($Full, $Lite)) {
    if (-not (Test-Path $f)) {
        Die "installer missing: $f`n 先运行 installer\build_installer.ps1 出包"
    }
}
if (-not (Test-Path $NotesFile)) { Die "notes file not found: $NotesFile" }

$ManifestPath = Join-Path $PSScriptRoot "Output\release-$Ver.json"
if (-not (Test-Path $ManifestPath)) {
    Die "release manifest missing: $ManifestPath`n 先运行 installer\build_installer.ps1 出包"
}
$Manifest = Get-Content -Raw -Encoding UTF8 $ManifestPath | ConvertFrom-Json
$Expected = @{}
foreach ($kind in @("full", "lite")) {
    $asset = $Manifest.assets.$kind
    if (-not $asset -or -not $asset.name -or -not $asset.sha256) {
        Die "release manifest missing assets.$kind"
    }
    $assetPath = if ($kind -eq "full") { $Full } else { $Lite }
    $Expected[$asset.name] = @{
        Path = $assetPath
        Size = [int64]$asset.size
        Sha  = ([string]$asset.sha256).ToLower()
    }
}
Step "Verify local installers against release-$Ver.json"
foreach ($name in $Expected.Keys) {
    $e = $Expected[$name]
    $actual = (Get-FileHash -Path $e.Path -Algorithm SHA256).Hash.ToLower()
    $size = (Get-Item $e.Path).Length
    if ($actual -ne $e.Sha) { Die "sha256 mismatch for ${name}: local=$actual manifest=$($e.Sha)" }
    if ($size -ne $e.Size) { Die "size mismatch for ${name}: local=$size manifest=$($e.Size)" }
    Write-Host ("  OK {0}  {1:N1} MB  {2}" -f $name, ($size / 1MB), $actual)
}

# ---- 3. gh 可用性 ----
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Die "GitHub CLI (gh) not found. 安装: winget install -e --id GitHub.cli ，然后 gh auth login"
}
& gh auth status 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Die "gh not authenticated. 运行: gh auth login" }

# ---- 4. 工作区干净度检查（tag 要指向已提交的版本号）----
Step "Check git working tree"
$Dirty = & git status --porcelain
if ($Dirty) {
    Write-Host "  工作区有未提交改动：" -ForegroundColor Yellow
    $Dirty | ForEach-Object { Write-Host "    $_" -ForegroundColor Yellow }
    if (-not $AllowDirty) {
        Die "working tree dirty。请先提交，或显式传 -AllowDirty（不推荐，tag 会指向旧代码）"
    }
}
$HeadVerLine = & git show "HEAD:src/utils/version.py" |
    Select-String -Pattern 'APP_VERSION\s*=\s*"([^"]+)"'
if (-not $HeadVerLine) { Die "cannot read APP_VERSION from HEAD" }
$HeadVer = $HeadVerLine[0].Matches[0].Groups[1].Value
if ($HeadVer -ne $Ver) {
    Die "HEAD 的 APP_VERSION=$HeadVer，工作区=$Ver。请先提交版本号再发布"
}

# ---- 5. 打 tag 并推送 ----
if (-not $SkipTag) {
    Step "Tag $Tag and push"
    $exists = & git tag -l $Tag
    if ($exists) {
        $tagCommit = & git rev-list -n 1 $Tag
        $headCommit = & git rev-parse HEAD
        if ($tagCommit -ne $headCommit) {
            Die "tag $Tag 已存在但指向 $tagCommit，不是当前 HEAD $headCommit。请删除旧 tag 或升版本号"
        }
        Write-Host "  tag $Tag 已存在且指向 HEAD，跳过创建"
    } else {
        $msg = "PaperWB $Ver"
        & git tag -a $Tag -m $msg
        if ($LASTEXITCODE -ne 0) { Die "git tag failed" }
        Write-Host "  created tag $Tag"
    }
    & git push origin $Tag
    if ($LASTEXITCODE -ne 0) { Die "tag 推送失败；不要继续创建 Release（否则用户检查不到更新）" }
}

# ---- 6. 创建 Release 并上传两个安装包（顺序：先 lite 后 full，full 大传得久）----
Step "Create GitHub Release $Tag (uploading ~1.1 GB total, may take a while)"
$ghArgs = @(
    "release", "create", $Tag,
    "--repo", $Repo,
    "--title", "PaperWB $Ver",
    "--notes-file", $NotesFile,
    $Lite, $Full
)
if ($Draft) { $ghArgs += "--draft" }
& gh @ghArgs
if ($LASTEXITCODE -ne 0) { Die "gh release create failed" }

# ---- 7. 回读校验：应用就是按这个接口取版本的 ----
Step "Verify via API (this is what the app reads)"
Start-Sleep -Seconds 3
$json = & gh api "repos/$Repo/releases/tags/$Tag" 2>&1 | Out-String
try {
    $rel = $json | ConvertFrom-Json
} catch {
    Die "API 回读解析失败：$json"
}
Write-Host "  release tag: $($rel.tag_name)"
$Uploaded = @{}
foreach ($a in $rel.assets) { $Uploaded[$a.name] = $a }
foreach ($name in $Expected.Keys) {
    if (-not $Uploaded.ContainsKey($name)) { Die "API 上找不到 asset: $name" }
    $a = $Uploaded[$name]
    $e = $Expected[$name]
    if ([int64]$a.size -ne $e.Size) {
        Die "asset $name 体积不符：API=$($a.size) 本地=$($e.Size)"
    }
    if ($a.digest -and ([string]$a.digest).StartsWith("sha256:")) {
        $remoteSha = ([string]$a.digest).Substring(7).ToLower()
        if ($remoteSha -ne $e.Sha) {
            Die "asset $name 的 API digest 与本地 sha256 不一致：remote=$remoteSha local=$($e.Sha)"
        }
        Write-Host ("    OK {0}  {1:N1} MB  sha256:{2}" -f $name, ($a.size / 1MB), $remoteSha)
    } else {
        Write-Host ("    警告：{0} 没有 digest，无法核对哈希" -f $name) -ForegroundColor Yellow
    }
}
if (-not $Draft) {
    $latestJson = & gh api "repos/$Repo/releases/latest" 2>&1 | Out-String
    try {
        $latest = $latestJson | ConvertFrom-Json
        if ($latest.tag_name -ne $Tag) {
            Write-Host "  提示：latest 仍是 $($latest.tag_name)（发布可能尚未生效）" -ForegroundColor Yellow
        } else {
            Write-Host "  latest tag 已指向 $Tag"
        }
    } catch {
        Write-Host "  latest 回读失败（不影响已完成的 Release）：$latestJson" -ForegroundColor Yellow
    }
} else {
    Write-Host "  这是草稿 Release，不会出现在 releases/latest；确认后到 GitHub 手动发布" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "DONE: $Repo/releases/tag/$Tag" -ForegroundColor Green
Write-Host "已安装的旧版本应用会在下次启动（约 15 秒后）检查到这个更新。" -ForegroundColor Green
Write-Host "自建更新源用户：把 Output\release-$Ver.json 的 assets[].url 填成 CDN 地址后上传。" -ForegroundColor Green
