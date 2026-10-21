"""更新推送核心：检查新版本 → 下载安装包 → 校验 → 用户确认后覆盖安装。

设计要点：

- **默认更新源 = GitHub Releases**（``Efdix/PaperWB``）：``tag_name`` 当版本号、
  ``body`` 当更新说明、``assets`` 当下载地址，零额外维护成本；也可在设置里换成
  自建静态 JSON 清单（``update_check.source = "custom"``）。
- **按安装形态选包**：完整版安装包预置了离线模型（``models/hub``），精简版没有。
  用 ``docling_parser.bundled_models_hub_dir()`` 判定当前形态，避免给完整版用户
  下成精简版（丢掉离线解析能力）。开发模式（非打包）不提供自动安装，只提示。
- **网络口径与全项目一致**：标准库 ``urllib`` + ``retry_urlopen`` + 自标识 UA，
  不引入第三方 HTTP 库。
- **失败绝不抛到 UI**：所有异常收敛为 ``UpdateError``（带人话原因）或静默降级，
  更新检查是附属功能，出问题不能影响正常使用。
- **数据安全**：安装包走 Inno 覆盖安装，安装器的 ``data_root`` 保留逻辑负责不
  动用户数据（见 ``installer/PaperWB.iss`` 的 ssPostInstall）；下载包在 sha256
  校验通过前不落正式文件名，校验失败一律删除，绝不执行来路不明的文件。

无头自检（``--selftest``）在创建 QApplication 之前就返回，不会触达本模块。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from PySide6.QtCore import QThread, Signal

from ..utils.config import get_updates_dir
from ..utils.version import (
    compare_versions,
    current_version,
    format_size,
    is_newer,
    user_agent,
)
from .pubmed_searcher import retry_urlopen

# ---- 常量 ----

GITHUB_REPO = "Efdix/PaperWB"
GITHUB_API_LATEST = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
GITHUB_PAGE = f"https://github.com/{GITHUB_REPO}/releases"

CHECK_TIMEOUT = 20.0        # 版本清单请求超时
DOWNLOAD_TIMEOUT = 60.0     # 下载连接/读取超时（大文件按块读取，单块超时即中断）
CHUNK_SIZE = 256 * 1024     # 下载分块大小（256 KB）
PROGRESS_STEPS = 200        # 进度信号最多发射次数（避免大文件刷爆事件循环）

KIND_FULL = "full"
KIND_LITE = "lite"
KIND_DEV = "dev"


class UpdateError(Exception):
    """更新检查/下载失败（消息面向用户，已是人话）。"""


class NoReleaseError(UpdateError):
    """更新源尚无任何已发布版本（GitHub 上未发布 Release 时返回 404）。

    自动检查把它当作"暂无更新"静默处理——项目尚未发版时天天在状态栏报错
    纯属噪声；手动检查仍会明确告知（用户主动问了就该知道原因）。
    """


@dataclass
class UpdateInfo:
    """一次可用更新的全部信息。"""

    version: str = ""
    notes: str = ""
    page_url: str = GITHUB_PAGE
    published_at: str = ""
    kind: str = KIND_FULL
    asset_name: str = ""
    asset_url: str = ""
    asset_size: int = 0
    sha256: str = ""
    source: str = "github"

    @property
    def has_asset(self) -> bool:
        """是否拿到了可下载的安装包（自定义源可能只给发布页链接）。"""
        return bool(self.asset_url)

    @property
    def size_text(self) -> str:
        return format_size(self.asset_size) if self.asset_size else "未知"

    @property
    def kind_text(self) -> str:
        return {"full": "完整版（含离线模型）", "lite": "精简版"}.get(self.kind, "未知")


# ---- 安装形态判定 ----

_KIND_CACHE: str | None = None


def install_kind() -> str:
    """当前安装形态：``full``（有预置模型）/ ``lite``（无预置模型）/ ``dev``（源码运行）。

    完整版与精简版的差别只有 ``models/hub`` 预置模型目录，故以它为准。
    开发模式不参与自动安装——源码树里没有可被覆盖的 exe。
    """
    global _KIND_CACHE
    if _KIND_CACHE is not None:
        return _KIND_CACHE
    if not getattr(sys, "frozen", False):
        _KIND_CACHE = KIND_DEV
        return _KIND_CACHE
    try:
        from .docling_parser import bundled_models_hub_dir

        _KIND_CACHE = KIND_FULL if bundled_models_hub_dir() else KIND_LITE
    except Exception:  # noqa: BLE001 — 判定失败按精简版走（体积更小，下载代价低）
        _KIND_CACHE = KIND_LITE
    return _KIND_CACHE


def is_lite_asset(name: str) -> bool:
    """安装包文件名是否为精简版（``...-lite.exe``）。"""
    return "-lite" in (name or "").lower()


def pick_asset(assets: list[dict], kind: str, version: str = "") -> dict | None:
    """从资产列表里挑出与当前安装形态匹配的安装包。

    assets 每项形如 ``{"name","url","size","sha256"}``。优先精确匹配
    ``PaperWB-Setup-<版本>[-lite].exe``，找不到再按 ``-lite`` 后缀规则兜底。

    **形态绝不自动替换**：完整版用户如果被塞精简版安装包，升级后会静默丢掉
    预置离线模型（首次解析突然要联网下载 500MB）；反之精简版用户被塞完整版
    则是莫名其妙的 2.5 倍下载量。因此只在本形态有对应包时返回，否则返回 None
    ——由调用方降级为"打开发布页"，把选择权交回用户。
    """
    exes = [a for a in assets if (a.get("name") or "").lower().endswith(".exe")]
    if not exes:
        return None
    want_lite = kind == KIND_LITE
    if version:
        for suffix in (f"PaperWB-Setup-{version}" + ("-lite.exe" if want_lite else ".exe"),):
            for a in exes:
                if (a.get("name") or "").lower() == suffix.lower():
                    return a
        # 版本号写法可能带 v 前缀或补位不同，退一步按"含版本号 + 形态"匹配
        ver_digits = version.lstrip("vV")
        for a in exes:
            name = (a.get("name") or "").lower()
            if ver_digits.lower() in name and is_lite_asset(name) == want_lite:
                return a
    exact = [a for a in exes if is_lite_asset(a.get("name", "")) == want_lite]
    if exact:
        return exact[0]
    return None


def _assets_from_github(release: dict) -> list[dict]:
    """GitHub release JSON 的 assets → 统一资产结构（sha256 取 digest 字段）。"""
    out: list[dict] = []
    for a in release.get("assets") or []:
        if not isinstance(a, dict):
            continue
        digest = str(a.get("digest") or "")
        sha = ""
        if digest.startswith("sha256:"):
            sha = digest.split(":", 1)[1].strip().lower()
        out.append({
            "name": str(a.get("name") or ""),
            "url": str(a.get("browser_download_url") or ""),
            "size": int(a.get("size") or 0),
            "sha256": sha,
        })
    return out


def _assets_from_manifest(raw) -> list[dict]:
    """自定义清单的 assets → 统一资产结构。

    支持两种写法：``{"full": {...}, "lite": {...}}``（键名即形态，URL 可省）或
    ``[{"name","url","size","sha256"}, ...]``（与 GitHub 同构）。
    """
    out: list[dict] = []
    if isinstance(raw, dict):
        for kind_key, item in raw.items():
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            if not name:
                # 只给了 URL：从 URL 尾部取文件名，供 pick_asset 判形态
                name = os.path.basename(urllib.parse.urlparse(str(item.get("url") or "")).path)
            if not name and kind_key in (KIND_FULL, KIND_LITE):
                name = f"PaperWB-Setup-{'lite' if kind_key == KIND_LITE else ''}.exe".replace("--", "-")
            out.append({
                "name": name,
                "url": str(item.get("url") or ""),
                "size": int(item.get("size") or 0),
                "sha256": str(item.get("sha256") or "").strip().lower(),
            })
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                out.append({
                    "name": str(item.get("name") or ""),
                    "url": str(item.get("url") or item.get("browser_download_url") or ""),
                    "size": int(item.get("size") or 0),
                    "sha256": str(item.get("sha256") or "").strip().lower(),
                })
    return out


def parse_github_release(release: dict, kind: str = "") -> UpdateInfo | None:
    """GitHub ``releases/latest`` JSON → UpdateInfo；结构不可识别返回 None。"""
    if not isinstance(release, dict):
        return None
    tag = str(release.get("tag_name") or release.get("name") or "").strip()
    if not tag:
        return None
    version = tag.lstrip("vV")
    kind = kind or install_kind()
    asset = pick_asset(_assets_from_github(release), kind, version) or {}
    return UpdateInfo(
        version=version,
        notes=str(release.get("body") or "").strip(),
        page_url=str(release.get("html_url") or GITHUB_PAGE),
        published_at=str(release.get("published_at") or ""),
        kind=kind,
        asset_name=str(asset.get("name") or ""),
        asset_url=str(asset.get("url") or ""),
        asset_size=int(asset.get("size") or 0),
        sha256=str(asset.get("sha256") or ""),
        source="github",
    )


def parse_custom_manifest(payload: dict, kind: str = "") -> UpdateInfo | None:
    """自建静态 JSON 清单 → UpdateInfo；结构不可识别返回 None。

    清单结构（字段均可选，缺失时降级为"仅打开发布页"）::

        {"version": "0.3.0", "notes": "...", "page_url": "...",
         "published_at": "...",
         "assets": {"full": {"url": "...", "size": 814000000, "sha256": "..."},
                    "lite": {"url": "...", "size": 327000000, "sha256": "..."}}}
    """
    if not isinstance(payload, dict):
        return None
    version = str(payload.get("version") or "").strip().lstrip("vV")
    if not version:
        return None
    kind = kind or install_kind()
    asset = pick_asset(_assets_from_manifest(payload.get("assets")), kind, version) or {}
    return UpdateInfo(
        version=version,
        notes=str(payload.get("notes") or payload.get("body") or "").strip(),
        page_url=str(payload.get("page_url") or payload.get("url") or GITHUB_PAGE),
        published_at=str(payload.get("published_at") or ""),
        kind=kind,
        asset_name=str(asset.get("name") or ""),
        asset_url=str(asset.get("url") or ""),
        asset_size=int(asset.get("size") or 0),
        sha256=str(asset.get("sha256") or ""),
        source="custom",
    )


def _require_https(url: str, what: str = "更新地址") -> None:
    """更新链路只接受 HTTPS：明文 HTTP 下清单和安装包都可被中间人替换。"""
    parsed = urllib.parse.urlparse(url or "")
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise UpdateError(f"{what}必须使用 HTTPS：{url or '（空）'}")


def resolve_source(source: str = "", url: str = "") -> tuple[str, str]:
    """确定实际请求地址：返回 (来源标识, URL)。自定义源未填 URL 时回退 GitHub。

    自定义源填写了 http:// 时明确报错而不是静默回退——否则用户以为自建源
    生效了，实际请求却发往 GitHub，排查方向完全错。
    """
    src = (source or "github").strip().lower()
    if src == "custom":
        target = (url or "").strip()
        if not target:
            return "github", GITHUB_API_LATEST
        _require_https(target, "自定义更新源")
        return "custom", target
    return "github", GITHUB_API_LATEST


def _fetch_manifest(url: str, timeout: float = CHECK_TIMEOUT) -> dict:
    """拉取并解析版本清单 JSON（瞬时错误重试，失败抛 UpdateError）。"""
    _require_https(url, "更新清单地址")
    req = urllib.request.Request(url, headers={
        "User-Agent": user_agent(),
        "Accept": "application/vnd.github+json, application/json",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    try:
        raw = retry_urlopen(req, timeout=timeout, attempts=3, base_delay=1.0)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise NoReleaseError(
                "更新源未找到发布版本（该仓库尚未发布 Release）") from e
        if e.code == 403:
            raise UpdateError("更新源拒绝访问（GitHub API 可能有速率限制，稍后再试）") from e
        raise UpdateError(f"更新源返回 HTTP {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise UpdateError(f"网络不可达：{reason}") from e
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise UpdateError("更新源返回的内容不是合法 JSON（自定义清单 URL 可能配错了）") from e


def check_for_update(
    current: str = "",
    source: str = "",
    url: str = "",
    skipped_version: str = "",
    respect_skipped: bool = True,
) -> UpdateInfo | None:
    """检查更新：有新版本返回 UpdateInfo，否则返回 None（失败抛 UpdateError）。

    ``respect_skipped``：自动检查传 True（用户选过「跳过此版本」就不再打扰）；
    手动检查传 False（用户主动点，被跳过的版本也要显示）。
    """
    cur = (current or current_version()).strip()
    src, target = resolve_source(source, url)
    payload = _fetch_manifest(target)
    info = (parse_custom_manifest(payload) if src == "custom"
            else parse_github_release(payload))
    if info is None:
        raise UpdateError("无法从更新源识别版本信息（清单结构不符合预期）")
    if not info.version:
        raise UpdateError("更新源未提供版本号")
    if compare_versions(info.version, cur) is None:
        raise UpdateError(f"版本号格式无法比较（本地 {cur} / 远端 {info.version}）")
    if not is_newer(info.version, cur):
        return None
    if respect_skipped and skipped_version and info.version == skipped_version.strip().lstrip("vV"):
        return None
    info.notes = notes_clean(info.notes)
    return info


def notes_clean(notes: str, limit: int = 8000) -> str:
    """更新说明裁剪：过长截断（避免几十 KB 的 release note 撑爆对话框）。"""
    text = (notes or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n\n…（更新说明过长已截断，完整内容见发布页）"


# ---- 检查节流 ----

def should_auto_check(cfg: dict) -> bool:
    """按配置判断此刻是否该自动检查（开关 + 间隔节流）。"""
    if not cfg.get("enabled"):
        return False
    last = str(cfg.get("last_check") or "")
    if not last:
        return True
    try:
        prev = datetime.fromisoformat(last)
    except ValueError:
        return True
    try:
        hours = max(1, int(cfg.get("interval_hours") or 24))
    except (TypeError, ValueError):
        hours = 24
    return datetime.now() - prev >= timedelta(hours=hours)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---- 后台线程 ----

class UpdateCheckWorker(QThread):
    """后台查询版本清单（不阻塞 UI）。

    信号：``found(UpdateInfo)`` / ``up_to_date()`` / ``failed(str)``。
    """

    found = Signal(object)
    up_to_date = Signal()
    no_release = Signal()   # 更新源尚无已发布版本（自动检查静默，手动检查提示）
    failed = Signal(str)

    def __init__(self, current: str = "", source: str = "", url: str = "",
                 skipped_version: str = "", respect_skipped: bool = True, parent=None):
        super().__init__(parent)
        self._current = current
        self._source = source
        self._url = url
        self._skipped = skipped_version
        self._respect_skipped = respect_skipped

    def run(self) -> None:  # noqa: D102
        try:
            info = check_for_update(
                current=self._current,
                source=self._source,
                url=self._url,
                skipped_version=self._skipped,
                respect_skipped=self._respect_skipped,
            )
        except NoReleaseError:
            # 尚无已发布版本：自动检查静默（等同"已是最新"），手动检查明确告知
            if not self.isInterruptionRequested():
                self.no_release.emit()
            return
        except UpdateError as e:
            if not self.isInterruptionRequested():
                self.failed.emit(str(e))
            return
        except Exception as e:  # noqa: BLE001 — 兜底，绝不从线程里抛出
            if not self.isInterruptionRequested():
                self.failed.emit(f"检查更新失败：{e}")
            return
        if self.isInterruptionRequested():
            return
        if info is None:
            self.up_to_date.emit()
        else:
            self.found.emit(info)


class UpdateDownloadWorker(QThread):
    """后台下载安装包 → 校验 → 就位（不阻塞 UI，可取消）。

    信号：``progress(int done, int total)``（total 可能为 0 = 未知）、
    ``finished_ok(str path)`` / ``failed(str)``。
    """

    progress = Signal(int, int)
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, info: UpdateInfo, dest_dir: str = "", parent=None):
        super().__init__(parent)
        self._info = info
        self._dest_dir = dest_dir

    def _dest_path(self) -> str:
        directory = self._dest_dir or str(get_updates_dir())
        name = self._info.asset_name or f"PaperWB-Setup-{self._info.version}.exe"
        # 防目录穿越：只取文件名部分
        return os.path.join(directory, os.path.basename(name))

    def run(self) -> None:  # noqa: D102
        info = self._info
        if not info.asset_url:
            self.failed.emit("该更新未提供安装包下载地址，请改用「打开发布页」手动下载")
            return
        dest = self._dest_path()
        part = dest + ".part"
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            self._download(info.asset_url, part)
        except UpdateError as e:
            self._cleanup(part)
            if not self.isInterruptionRequested():
                self.failed.emit(str(e))
            return
        except Exception as e:  # noqa: BLE001
            self._cleanup(part)
            if not self.isInterruptionRequested():
                self.failed.emit(f"下载失败：{e}")
            return
        if self.isInterruptionRequested():
            self._cleanup(part)
            return
        try:
            os.replace(part, dest)
        except OSError as e:
            self._cleanup(part)
            self.failed.emit(f"保存安装包失败：{e}")
            return
        self.finished_ok.emit(dest)

    def _download(self, url: str, part: str) -> None:
        """流式下载到 .part，边下边算 sha256，完成后校验体积与哈希。"""
        info = self._info
        _require_https(url, "安装包下载地址")
        if not info.sha256:
            raise UpdateError(
                "发布方未提供 sha256 校验值，出于安全考虑已拒绝自动安装。\n"
                "请点击「打开发布页」手动下载安装包。")
        req = urllib.request.Request(url, headers={
            "User-Agent": user_agent(),
            "Accept": "application/octet-stream",
        })
        digest = hashlib.sha256()
        done = 0
        total = int(info.asset_size or 0)
        try:
            resp = urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT)
        except urllib.error.HTTPError as e:
            raise UpdateError(f"下载被拒绝（HTTP {e.code}）：{e.reason}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise UpdateError(f"下载连接失败：{reason}") from e
        # 禁止 HTTPS → HTTP 重定向降级：urllib 默认自动跟随，必须检查终点。
        final_url = getattr(resp, "geturl", lambda: url)()
        if urllib.parse.urlparse(final_url).scheme.lower() != "https":
            resp.close()
            raise UpdateError("下载被重定向到非 HTTPS 地址，已中止")
        step = 0
        try:
            with resp, open(part, "wb") as f:
                if not total:
                    try:
                        total = int(resp.headers.get("Content-Length") or 0)
                    except (TypeError, ValueError):
                        total = 0
                while True:
                    if self.isInterruptionRequested():
                        raise UpdateError("下载已取消")
                    try:
                        chunk = resp.read(CHUNK_SIZE)
                    except (TimeoutError, OSError) as e:
                        raise UpdateError(f"下载中断：{e}") from e
                    if not chunk:
                        break
                    f.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    step += 1
                    if step % max(1, PROGRESS_STEPS // 10) == 0 or done == total:
                        self.progress.emit(done, total)
        except OSError as e:
            raise UpdateError(f"写入安装包失败（磁盘空间不足？）：{e}") from e
        if done == 0:
            raise UpdateError("下载内容为空（下载地址可能已失效）")
        if info.asset_size and done != info.asset_size:
            raise UpdateError(
                f"下载体积不符（期望 {format_size(info.asset_size)}，实际 {format_size(done)}），已丢弃")
        if info.sha256:
            actual = digest.hexdigest()
            if actual.lower() != info.sha256.lower():
                raise UpdateError("安装包校验失败（sha256 不匹配），文件可能损坏或已被篡改，已丢弃")
        self.progress.emit(done, total or done)

    @staticmethod
    def _cleanup(part: str) -> None:
        try:
            if os.path.exists(part):
                os.remove(part)
        except OSError:
            pass


# ---- 安装器启动 ----

def launcher_script(setup_path: str, app_path: str = "") -> str:
    """生成更新启动器脚本内容（全 ASCII，避免 cmd 的 OEM 代码页乱码）。

    为什么需要启动器：应用必须**先退出**才能让安装器覆盖 `PaperWB.exe`，
    而 Inno 的 `[Run]` 段带 ``skipifsilent``（静默安装不自动启动应用），
    因此由本脚本在安装结束后负责把应用重新拉起。

    步骤：等待应用释放文件锁 → 静默安装（``/SUPPRESSMSGBOXES`` 防无人值守时
    卡对话框）→ 重启应用 → 自删。``/NORESTARTAPPLICATIONS`` 让安装器不要自行
    重启（避免出现两个实例），重启统一由本脚本负责。
    """
    app = app_path or (sys.executable if getattr(sys, "frozen", False) else "")
    lines = [
        "@echo off",
        "rem PaperWB self-update launcher (auto-generated, safe to delete)",
        "rem wait for the app to exit and release file locks",
        "ping -n 3 127.0.0.1 >nul",
        f'start /wait "" "{setup_path}" /SILENT /SUPPRESSMSGBOXES /NORESTARTAPPLICATIONS',
    ]
    if app:
        lines.append(f'start "" "{app}"')
    lines.append('start /b "" cmd /c del "%~f0" >nul 2>&1')
    return "\r\n".join(lines) + "\r\n"


def write_launcher(setup_path: str, app_path: str = "") -> str:
    """把启动器脚本写到临时目录，返回脚本路径。"""
    from ..utils.config import get_tmp_dir

    path = os.path.join(str(get_tmp_dir()), "paperwb_update.cmd")
    with open(path, "w", encoding="ascii", errors="replace", newline="") as f:
        f.write(launcher_script(setup_path, app_path))
    return path


def spawn_launcher(script_path: str) -> None:
    """脱离当前进程启动更新脚本（应用随后应立即退出）。"""
    flags = 0
    if os.name == "nt":
        # DETACHED_PROCESS：脚本不随应用退出而死，也不弹控制台窗口
        flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | \
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        creation = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        flags |= creation
    subprocess.Popen(
        ["cmd", "/c", script_path],
        close_fds=True,
        creationflags=flags,
        cwd=os.path.dirname(script_path),
    )


def launch_installer_after_exit(setup_path: str, app_path: str = "") -> None:
    """写好启动器并脱离启动；调用方紧接着应调用 ``QApplication.quit()``。"""
    if not os.path.isfile(setup_path):
        raise UpdateError("安装包不存在（可能已被清理），请重新下载")
    script = write_launcher(setup_path, app_path)
    spawn_launcher(script)


def install_supported() -> bool:
    """当前环境是否支持应用内自动安装（源码运行不支持）。"""
    return install_kind() != KIND_DEV


def open_url(url: str) -> bool:
    """用系统浏览器打开 URL（失败返回 False，绝不抛异常）。"""
    target = (url or "").strip()
    if not target.startswith(("http://", "https://")):
        return False
    try:
        if os.name == "nt":
            os.startfile(target)  # type: ignore[attr-defined]
        else:
            import webbrowser

            webbrowser.open(target)
        return True
    except Exception:  # noqa: BLE001
        return False


__all__ = [
    "GITHUB_PAGE",
    "KIND_DEV",
    "KIND_FULL",
    "KIND_LITE",
    "UpdateDownloadWorker",
    "UpdateCheckWorker",
    "UpdateError",
    "UpdateInfo",
    "check_for_update",
    "install_kind",
    "install_supported",
    "launch_installer_after_exit",
    "now_iso",
    "open_url",
    "parse_custom_manifest",
    "parse_github_release",
    "pick_asset",
    "resolve_source",
    "should_auto_check",
]
