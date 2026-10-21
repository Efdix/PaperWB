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
    Write-Host "  警告：工作区有未提交改动：" -ForegroundColor Yellow
    $Dirty | ForEach-Object { Write-Host "    $_" -ForegroundColor Yellow }
    Write-Host "  tag 会指向最后一次提交；若版本号尚未提交，请先 commit。" -ForegroundColor Yellow
}

# ---- 5. 打 tag 并推送 ----
if (-not $SkipTag) {
    Step "Tag $Tag and push"
    $exists = & git tag -l $Tag
    if ($exists) {
        Write-Host "  tag $Tag 已存在，跳过创建" -ForegroundColor Yellow
    } else {
        $msg = "PaperWB $Ver"
        & git tag -a $Tag -m $msg
        if ($LASTEXITCODE -ne 0) { Die "git tag failed" }
        Write-Host "  created tag $Tag"
    }
    & git push origin $Tag
    if ($LASTEXITCODE -ne 0) { Write-Host "  tag 推送失败（可能已存在于远端），继续" -ForegroundColor Yellow }
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
$json = & gh api "repos/$Repo/releases/latest" 2>&1 | Out-String
try {
    $rel = $json | ConvertFrom-Json
    Write-Host "  latest tag : $($rel.tag_name)"
    Write-Host "  assets     :"
    foreach ($a in $rel.assets) {
        $sha = if ($a.digest) { $a.digest } else { "(no digest)" }
        Write-Host ("    {0}  {1:N1} MB  {2}" -f $a.name, ($a.size / 1MB), $sha)
    }
    if ($rel.tag_name -ne $Tag) {
        Write-Host "  提示：latest 仍是 $($rel.tag_name)（草稿不会成为 latest，或发布尚未生效）" -ForegroundColor Yellow
    }
} catch {
    Write-Host "  API 回读解析失败（不影响发布）：$json" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "DONE: $Repo/releases/tag/$Tag" -ForegroundColor Green
Write-Host "已安装的旧版本应用会在下次启动（约 15 秒后）检查到这个更新。" -ForegroundColor Green
Write-Host "自建更新源用户：把 Output\release-$Ver.json 的 assets[].url 填成 CDN 地址后上传。" -ForegroundColor Green
