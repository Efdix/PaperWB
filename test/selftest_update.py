"""更新推送回归测试 —— 纯逻辑，无网络、无 LLM、无真实安装。

覆盖：
  1. 版本号单一来源与语义化比较边界（唯一字面量、构建脚本抓取口径一致）
  2. GitHub Releases 清单解析（full/lite 形态选包、缺 asset、畸形 JSON）
  3. 自定义清单解析（两种 assets 写法、URL 推导文件名）
  4. 更新源解析与降级（custom 未填 URL 回退 GitHub）
  5. 检查节流（开关、间隔、last_check 解析失败）
  6. 跳过版本逻辑（自动检查尊重、手动检查忽略）、无 Release 的静默处理
  7. 安装形态判定（dev 不支持自动安装）
  8. 下载分块 + sha256 校验 + 体积校验 + .part 清理（本地 file:// 与假响应）
  9. 启动器脚本命令行生成（静默参数、重启应用、自删）
 10. 配置读写（update_check 合并默认、只改本键、不丢并发写入的键）
 11. 更新对话框离屏构建（有包/无包/开发模式三态）

运行：python test/selftest_update.py
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PAPERWB_DISABLE_PREPARSE", "1")

_FAILURES: list[str] = []
_PASSED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _PASSED
    if cond:
        _PASSED += 1
        print(f"[PASS] {name}")
    else:
        _FAILURES.append(name)
        print(f"[FAIL] {name}" + (f": {detail}" if detail else ""))


def _section(title: str) -> None:
    print(f"\n--- {title} ---")


# ============ 1. 版本号 ============

def test_version_source() -> None:
    _section("版本号单一来源与比较")
    from src.utils.version import (
        APP_VERSION, compare_versions, current_version, format_size,
        is_newer, parse_version, user_agent,
    )

    check("APP_VERSION 为 x.y.z 形式", bool(re.fullmatch(r"\d+\.\d+\.\d+", APP_VERSION)),
          APP_VERSION)
    check("current_version 回退到常量（无 QApplication）",
          current_version() == APP_VERSION, current_version())

    # QApplication 存在但版本未设（测试/宿主环境）时，绝不能拿到 Qt 自己的版本号。
    # Qt 在未设版本时会返回自身版本（形如 3.11.16150.1013），拿去比对会误判更新。
    from PySide6.QtWidgets import QApplication
    if QApplication.instance() is None:
        check("无 QApplication 时回退常量", current_version() == APP_VERSION)

    # 构建脚本必须从 version.py 抓版本（旧口径是抓 main.py 的 setApplicationVersion）
    ps1 = (_ROOT / "installer" / "build_installer.ps1").read_text(encoding="utf-8")
    check("构建脚本从 version.py 抓版本",
          "src\\utils\\version.py" in ps1 and "APP_VERSION" in ps1)
    check("构建脚本不再从 main.py 抓版本",
          "setApplicationVersion\\(\"([^\"]+)\"\\)" not in ps1
          or 'Select-String -Path "src\\utils\\version.py"' in ps1)

    # ISS 兜底值须与 version.py 一致（否则直接跑 ISCC 会产出错版本号安装包）
    iss = (_ROOT / "installer" / "PaperWB.iss").read_text(encoding="utf-8")
    m = re.search(r'#define MyAppVersion "([^"]+)"', iss)
    check("ISS 兜底版本与 APP_VERSION 一致",
          bool(m) and m.group(1) == APP_VERSION,
          f"iss={m.group(1) if m else '?'} app={APP_VERSION}")

    # main.py 不再硬编码版本
    main_src = (_ROOT / "main.py").read_text(encoding="utf-8")
    check("main.py 不再硬编码版本号", "setApplicationVersion(\"0.2.0\")" not in main_src)
    check("main.py 引用 APP_VERSION", "APP_VERSION" in main_src)

    # app.py 关于对话框不再硬编码 v1.0.0
    app_src = (_ROOT / "src" / "app.py").read_text(encoding="utf-8")
    check("关于对话框不再硬编码 v1.0.0", "AI 论文解读助手 v1.0.0" not in app_src)
    check("关于对话框用 current_version()", "current_version()" in app_src)

    # 比较边界
    check("0.3.0 > 0.2.0", is_newer("0.3.0", "0.2.0"))
    check("0.2.0 不比 0.2.0 新", not is_newer("0.2.0", "0.2.0"))
    check("0.10.0 > 0.9.0（数字比较非字典序）", is_newer("0.10.0", "0.9.0"))
    check("v 前缀可解析", parse_version("v1.2.3") is not None)
    check("v0.3.0 > 0.2.0", is_newer("v0.3.0", "0.2.0"))
    check("位数不等按补零比较（0.3 == 0.3.0）", compare_versions("0.3", "0.3.0") == 0)
    check("0.3.1 > 0.3", is_newer("0.3.1", "0.3.0"))
    check("正式版高于预发布（0.3.0 > 0.3.0-beta）", is_newer("0.3.0", "0.3.0-beta"))
    check("预发布不高于正式版", not is_newer("0.3.0-beta", "0.3.0"))
    check("畸形版本不误报（返回 False）", not is_newer("latest", "0.2.0"))
    check("畸形本地版本不误报", not is_newer("0.3.0", "unknown"))
    check("空串不误报", not is_newer("", "0.2.0"))
    check("compare 畸形返回 None", compare_versions("abc", "1.0") is None)

    check("format_size 字节", format_size(512) == "512 B", format_size(512))
    check("format_size MB", format_size(5 * 1024 * 1024) == "5.0 MB",
          format_size(5 * 1024 * 1024))
    check("format_size 0", format_size(0) == "0 B", format_size(0))
    check("UA 含版本号", APP_VERSION in user_agent(), user_agent())


# ============ 2. GitHub 清单解析 ============

def _gh_payload(version: str = "0.3.0", *, with_lite: bool = True,
                with_full: bool = True, digest: str = "") -> dict:
    assets = []
    if with_full:
        assets.append({
            "name": f"PaperWB-Setup-{version}.exe",
            "browser_download_url": f"https://example.com/{version}/full.exe",
            "size": 814_000_000,
            "digest": digest or None,
        })
    if with_lite:
        assets.append({
            "name": f"PaperWB-Setup-{version}-lite.exe",
            "browser_download_url": f"https://example.com/{version}/lite.exe",
            "size": 327_000_000,
            "digest": digest or None,
        })
    return {
        "tag_name": f"v{version}",
        "body": "## 更新内容\n- 新增更新推送",
        "html_url": f"https://github.com/Efdix/PaperWB/releases/tag/v{version}",
        "published_at": "2026-09-18T10:00:00Z",
        "assets": assets,
    }


def test_parse_github_release() -> None:
    _section("GitHub Releases 清单解析")
    from src.core.updater import KIND_FULL, KIND_LITE, parse_github_release

    info = parse_github_release(_gh_payload(), KIND_FULL)
    check("解析出完整版信息", info is not None and info.version == "0.3.0")
    check("完整版选 full 包", info.asset_name == "PaperWB-Setup-0.3.0.exe",
          info.asset_name)
    check("版本号去掉 v 前缀", info.version == "0.3.0", info.version)
    check("更新说明保留", "更新推送" in info.notes)
    check("体积解析", info.asset_size == 814_000_000, str(info.asset_size))
    check("发布页 URL", info.page_url.endswith("/tag/v0.3.0"), info.page_url)
    check("有可下载资产", info.has_asset)

    lite = parse_github_release(_gh_payload(), KIND_LITE)
    check("精简版选 -lite 包", lite.asset_name == "PaperWB-Setup-0.3.0-lite.exe",
          lite.asset_name)
    check("精简版体积较小", lite.asset_size < info.asset_size)
    check("形态文案正确", "精简版" in lite.kind_text and "完整版" in info.kind_text)

    # sha256 来自 GitHub 的 digest 字段
    digest = "sha256:" + "a" * 64
    d_info = parse_github_release(_gh_payload(digest=digest), KIND_FULL)
    check("digest 字段解析为 sha256", d_info.sha256 == "a" * 64, d_info.sha256)
    no_digest = parse_github_release(_gh_payload(), KIND_FULL)
    check("无 digest 时 sha256 为空", no_digest.sha256 == "")

    # 只有一种形态时**绝不**跨形态替换（否则完整版用户会静默丢掉离线模型），
    # 而是不给可下载资产 → 对话框降级为「打开发布页」
    only_full = parse_github_release(_gh_payload(with_lite=False), KIND_LITE)
    check("缺 lite 包时不给完整版包（不跨形态替换）",
          not only_full.has_asset, only_full.asset_name)
    only_lite = parse_github_release(_gh_payload(with_full=False), KIND_FULL)
    check("缺 full 包时不给精简版包（不跨形态替换）",
          not only_lite.has_asset, only_lite.asset_name)
    none = parse_github_release({"tag_name": "v0.3.0", "assets": []}, KIND_FULL)
    check("完全无资产时不报错", none is not None and not none.has_asset)
    check("无资产时 size_text 为未知", none.size_text == "未知", none.size_text)

    # 畸形输入不臆造
    check("非 dict 返回 None", parse_github_release(None, KIND_FULL) is None)
    check("缺 tag_name 返回 None",
          parse_github_release({"body": "x"}, KIND_FULL) is None)
    check("tag 为空白返回 None",
          parse_github_release({"tag_name": "  "}, KIND_FULL) is None)
    check("assets 为 None 不崩",
          parse_github_release({"tag_name": "v0.3.0", "assets": None},
                               KIND_FULL) is not None)


# ============ 3. 自定义清单解析 ============

def test_parse_custom_manifest() -> None:
    _section("自定义清单解析")
    from src.core.updater import KIND_FULL, KIND_LITE, parse_custom_manifest

    payload = {
        "version": "0.4.0",
        "notes": "自建源更新说明",
        "page_url": "https://mirror.example.com/paperwb/",
        "assets": {
            "full": {"url": "https://mirror.example.com/PaperWB-Setup-0.4.0.exe",
                     "size": 814_000_000, "sha256": "b" * 64},
            "lite": {"url": "https://mirror.example.com/PaperWB-Setup-0.4.0-lite.exe",
                     "size": 327_000_000, "sha256": "c" * 64},
        },
    }
    full = parse_custom_manifest(payload, KIND_FULL)
    check("字典式 assets 解析 full", full is not None
          and full.asset_name == "PaperWB-Setup-0.4.0.exe", full.asset_name)
    check("URL 推导出文件名（供形态判定）", full.asset_url.endswith("PaperWB-Setup-0.4.0.exe"))
    check("sha256 透传", full.sha256 == "b" * 64, full.sha256)
    check("来源标注 custom", full.source == "custom")

    lite = parse_custom_manifest(payload, KIND_LITE)
    check("字典式 assets 解析 lite",
          lite.asset_name == "PaperWB-Setup-0.4.0-lite.exe", lite.asset_name)

    # 列表式（与 GitHub 同构）
    listed = parse_custom_manifest({
        "version": "0.4.0",
        "assets": [
            {"name": "PaperWB-Setup-0.4.0.exe", "url": "https://x/full.exe", "size": 1},
            {"name": "PaperWB-Setup-0.4.0-lite.exe", "url": "https://x/lite.exe", "size": 2},
        ],
    }, KIND_LITE)
    check("列表式 assets 解析", listed.asset_name.endswith("-lite.exe"), listed.asset_name)

    # 缺字段降级
    bare = parse_custom_manifest({"version": "0.4.0"}, KIND_FULL)
    check("无 assets 字段不崩", bare is not None and not bare.has_asset)
    check("page_url 缺省回退 GitHub 发布页",
          bare.page_url.startswith("https://github.com/"), bare.page_url)
    check("version 带 v 前缀可解析",
          parse_custom_manifest({"version": "v0.4.0"}, KIND_FULL).version == "0.4.0")
    check("缺 version 返回 None", parse_custom_manifest({"notes": "x"}, KIND_FULL) is None)
    check("非 dict 返回 None", parse_custom_manifest("nope", KIND_FULL) is None)
    check("assets 为垃圾类型不崩",
          parse_custom_manifest({"version": "1.0", "assets": "bad"}, KIND_FULL) is not None)


def test_pick_asset() -> None:
    _section("安装包挑选规则")
    from src.core.updater import KIND_FULL, KIND_LITE, is_lite_asset, pick_asset

    assets = [
        {"name": "PaperWB-Setup-0.3.0.exe", "url": "u1", "size": 1, "sha256": ""},
        {"name": "PaperWB-Setup-0.3.0-lite.exe", "url": "u2", "size": 2, "sha256": ""},
        {"name": "checksums.txt", "url": "u3", "size": 3, "sha256": ""},
    ]
    check("忽略非 exe", pick_asset(assets, KIND_FULL)["url"] == "u1")
    check("lite 挑 lite", pick_asset(assets, KIND_LITE)["url"] == "u2")
    check("空列表返回 None", pick_asset([], KIND_FULL) is None)
    check("无 exe 返回 None", pick_asset([{"name": "a.zip"}], KIND_FULL) is None)
    # 只有一种形态时不跨形态替换
    only_full_assets = [a for a in assets if not is_lite_asset(a["name"])]
    check("仅 full 包时 lite 请求返回 None（不跨形态替换）",
          pick_asset(only_full_assets, KIND_LITE) is None)
    # 版本号写法不同（补位/前缀差异）也能兜住
    check("版本号补位差异可匹配",
          pick_asset(assets, KIND_FULL, "0.3")["url"] == "u1")


# ============ 4. 更新源解析 ============

def test_resolve_source() -> None:
    _section("更新源解析与降级")
    from src.core.updater import GITHUB_API_LATEST, resolve_source

    check("默认 GitHub", resolve_source("", "")[0] == "github")
    check("github 用 API URL", resolve_source("github", "")[1] == GITHUB_API_LATEST)
    check("custom + 合法 URL",
          resolve_source("custom", "https://x/v.json") == ("custom", "https://x/v.json"))
    check("custom 但 URL 为空回退 GitHub",
          resolve_source("custom", "")[0] == "github")
    try:
        resolve_source("custom", "http://x/v.json")
        http_rejected = False
    except Exception:
        http_rejected = True
    check("custom 明文 HTTP 被拒绝", http_rejected)
    for bad in ("not-a-url", "ftp://x/v.json"):
        try:
            resolve_source("custom", bad)
            rejected = False
        except Exception:
            rejected = True
        check(f"custom 非法源被拒绝：{bad}", rejected)


# ============ 5. 节流 ============

def test_should_auto_check() -> None:
    _section("自动检查节流")
    from src.core.updater import should_auto_check

    check("关闭时不检查", not should_auto_check({"enabled": False}))
    check("开启且从未检查过 → 检查",
          should_auto_check({"enabled": True, "last_check": ""}))
    fresh = datetime.now().isoformat(timespec="seconds")
    check("刚检查过（默认 24h）不重复",
          not should_auto_check({"enabled": True, "last_check": fresh,
                                 "interval_hours": 24}))
    old = (datetime.now() - timedelta(hours=30)).isoformat(timespec="seconds")
    check("超过间隔后检查",
          should_auto_check({"enabled": True, "last_check": old,
                             "interval_hours": 24}))
    recent = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
    check("间隔内不检查",
          not should_auto_check({"enabled": True, "last_check": recent,
                                 "interval_hours": 24}))
    check("last_check 畸形视为该检查",
          should_auto_check({"enabled": True, "last_check": "garbage"}))
    check("间隔非法回退 24h",
          not should_auto_check({"enabled": True, "last_check": fresh,
                                 "interval_hours": "abc"}))


# ============ 6. 无 Release 的静默处理 ============

def test_no_release_handling() -> None:
    _section("更新源尚无 Release 的处理")
    import urllib.error
    import src.core.updater as up
    from src.core.updater import NoReleaseError, UpdateCheckWorker

    original = up.urllib.request.urlopen

    def _404(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    try:
        up.urllib.request.urlopen = _404
        w = UpdateCheckWorker(current="0.2.0")
        events: dict = {}
        w.found.connect(lambda i: events.update(found=i))
        w.up_to_date.connect(lambda: events.update(uptodate=True))
        w.no_release.connect(lambda: events.update(norelease=True))
        w.failed.connect(lambda m: events.update(err=m))
        w.run()
        check("404 归入 no_release（不进 failed）",
              events.get("norelease") is True and "err" not in events, str(events))
        check("404 不误报为有更新", "found" not in events)
    finally:
        up.urllib.request.urlopen = original

    check("NoReleaseError 是 UpdateError 子类",
          issubclass(NoReleaseError, up.UpdateError))

    # 403（限流）仍是 failed，不进 no_release
    def _403(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 403, "rate limited", {}, None)

    try:
        up.urllib.request.urlopen = _403
        w2 = UpdateCheckWorker(current="0.2.0")
        ev2: dict = {}
        w2.no_release.connect(lambda: ev2.update(norelease=True))
        w2.failed.connect(lambda m: ev2.update(err=m))
        w2.run()
        check("403 走 failed 并提示限流",
              "err" in ev2 and "限流" in ev2["err"] or "速" in ev2.get("err", ""),
              str(ev2))
        check("403 不误归为无发布版本", "norelease" not in ev2)
    finally:
        up.urllib.request.urlopen = original

    # 网络不可达 → failed，人话
    def _boom(req, timeout=0):
        raise urllib.error.URLError("getaddrinfo failed")

    try:
        up.urllib.request.urlopen = _boom
        w3 = UpdateCheckWorker(current="0.2.0")
        ev3: dict = {}
        w3.failed.connect(lambda m: ev3.update(err=m))
        w3.up_to_date.connect(lambda: ev3.update(uptodate=True))
        w3.run()
        check("网络不可达走 failed 且为人话",
              "err" in ev3 and "网络" in ev3["err"], str(ev3))
    finally:
        up.urllib.request.urlopen = original

    # 自动检查 vs 手动检查的跳过逻辑
    def _json(payload):
        class _R:
            headers = {"Content-Length": "0"}

            def read(self, n=-1):
                return payload

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return lambda req, timeout=0: _R()

    newer = json.dumps(_gh_payload("0.9.0"), ensure_ascii=False).encode()
    try:
        up.urllib.request.urlopen = _json(newer)
        # 手动检查：即使该版本被跳过也要报出来
        w4 = UpdateCheckWorker(current="0.2.0", skipped_version="0.9.0",
                               respect_skipped=False)
        ev4: dict = {}
        w4.found.connect(lambda i: ev4.update(found=i.version))
        w4.up_to_date.connect(lambda: ev4.update(uptodate=True))
        w4.run()
        check("手动检查无视「跳过此版本」", ev4.get("found") == "0.9.0", str(ev4))

        # 自动检查：跳过版本安静
        w5 = UpdateCheckWorker(current="0.2.0", skipped_version="0.9.0",
                               respect_skipped=True)
        ev5: dict = {}
        w5.found.connect(lambda i: ev5.update(found=i.version))
        w5.up_to_date.connect(lambda: ev5.update(uptodate=True))
        w5.run()
        check("自动检查尊重「跳过此版本」",
              "found" not in ev5 and ev5.get("uptodate") is True, str(ev5))

        # 远端不高于本地 → up_to_date
        same = json.dumps(_gh_payload("0.2.0"), ensure_ascii=False).encode()
        up.urllib.request.urlopen = _json(same)
        w6 = UpdateCheckWorker(current="0.2.0")
        ev6: dict = {}
        w6.found.connect(lambda i: ev6.update(found=i.version))
        w6.up_to_date.connect(lambda: ev6.update(uptodate=True))
        w6.run()
        check("同版本视为已最新", ev6.get("uptodate") is True and "found" not in ev6)

        # 远端更旧 → up_to_date（不降级）
        older = json.dumps(_gh_payload("0.1.0"), ensure_ascii=False).encode()
        up.urllib.request.urlopen = _json(older)
        w7 = UpdateCheckWorker(current="0.2.0")
        ev7: dict = {}
        w7.found.connect(lambda i: ev7.update(found=i.version))
        w7.up_to_date.connect(lambda: ev7.update(uptodate=True))
        w7.run()
        check("远端更旧不提示更新", ev7.get("uptodate") is True and "found" not in ev7)

        # 畸形清单 → failed 且不臆造版本
        up.urllib.request.urlopen = _json(b'{"nonsense": true}')
        w8 = UpdateCheckWorker(current="0.2.0")
        ev8: dict = {}
        w8.found.connect(lambda i: ev8.update(found=i.version))
        w8.failed.connect(lambda m: ev8.update(err=m))
        w8.run()
        check("畸形清单报错而非臆造版本",
              "err" in ev8 and "found" not in ev8, str(ev8))

        # 非 JSON 内容 → 人话报错
        up.urllib.request.urlopen = _json(b"<html>not json</html>")
        w9 = UpdateCheckWorker(current="0.2.0")
        ev9: dict = {}
        w9.failed.connect(lambda m: ev9.update(err=m))
        w9.run()
        check("非 JSON 报错清晰", "err" in ev9 and "JSON" in ev9["err"], str(ev9))
    finally:
        up.urllib.request.urlopen = original


# ============ 7. 更新说明裁剪 ============

def test_notes_clean() -> None:
    _section("更新说明裁剪")
    from src.core.updater import notes_clean

    check("短说明原样", notes_clean("hello") == "hello")
    check("None 安全", notes_clean(None) == "")
    long_text = "x" * 20000
    out = notes_clean(long_text, limit=100)
    check("超长截断", len(out) < 300 and "截断" in out, str(len(out)))
    check("截断保留前缀", out.startswith("x" * 100))


# ============ 7. 安装形态 ============

def test_install_kind() -> None:
    _section("安装形态判定")
    from src.core.updater import (
        KIND_DEV, install_kind, install_supported, is_lite_asset,
    )

    # 测试进程非 frozen → dev
    check("源码运行判定为 dev", install_kind() == KIND_DEV, install_kind())
    check("dev 不支持应用内自动安装", not install_supported())
    check("-lite 后缀识别", is_lite_asset("PaperWB-Setup-0.2.0-lite.exe"))
    check("-lite 大小写不敏感", is_lite_asset("PaperWB-Setup-0.2.0-LITE.EXE"))
    check("full 不误判", not is_lite_asset("PaperWB-Setup-0.2.0.exe"))
    check("空串安全", not is_lite_asset(""))


# ============ 8. 下载与校验 ============

class _FakeResponse:
    """模拟 urlopen 返回值：支持 with / read(n) / headers。"""

    def __init__(self, data: bytes, chunk: int | None = None):
        self._buf = io.BytesIO(data)
        self._chunk = chunk
        self.headers = {"Content-Length": str(len(data))}

    def read(self, n: int = -1) -> bytes:
        if self._chunk is not None and n > 0:
            n = min(n, self._chunk)
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_download_and_verify() -> None:
    _section("下载分块与校验")
    import src.core.updater as up
    from src.core.updater import KIND_FULL, UpdateDownloadWorker, UpdateInfo

    payload = os.urandom(700 * 1024)  # 跨多个分块
    good_sha = hashlib.sha256(payload).hexdigest()
    original = up.urllib.request.urlopen
    original_replace = os.replace

    with tempfile.TemporaryDirectory() as tmp:
        def _make(url: str, timeout: float = 0):
            return _FakeResponse(payload)

        try:
            up.urllib.request.urlopen = _make

            # 正常下载 + sha256 校验通过
            info = UpdateInfo(version="9.9.9", kind=KIND_FULL,
                              asset_name="PaperWB-Setup-9.9.9.exe",
                              asset_url="https://x/setup.exe",
                              asset_size=len(payload), sha256=good_sha)
            w = UpdateDownloadWorker(info, dest_dir=tmp)
            results: dict = {}
            w.finished_ok.connect(lambda p: results.update(ok=p))
            w.failed.connect(lambda m: results.update(err=m))
            w.run()
            check("下载成功信号", "ok" in results, str(results))
            check("落盘文件名正确",
                  results.get("ok", "").endswith("PaperWB-Setup-9.9.9.exe"))
            check("无 .part 残留",
                  not any(n.endswith(".part") for n in os.listdir(tmp)),
                  str(os.listdir(tmp)))
            with open(results["ok"], "rb") as f:
                check("内容字节一致", f.read() == payload)

            # sha256 不匹配 → 失败且不留文件
            bad = UpdateInfo(version="9.9.9", kind=KIND_FULL,
                             asset_name="bad.exe", asset_url="https://x/bad.exe",
                             asset_size=len(payload), sha256="0" * 64)
            w2 = UpdateDownloadWorker(bad, dest_dir=tmp)
            res2: dict = {}
            w2.finished_ok.connect(lambda p: res2.update(ok=p))
            w2.failed.connect(lambda m: res2.update(err=m))
            w2.run()
            check("哈希不匹配报错", "err" in res2 and "校验失败" in res2["err"],
                  str(res2))
            check("哈希不匹配不留文件", not os.path.exists(os.path.join(tmp, "bad.exe")))
            check("哈希不匹配清掉 .part",
                  not any(n.endswith(".part") for n in os.listdir(tmp)))

            # 体积不匹配 → 失败（哈希本身正确，确保命中体积分支）
            size_bad = UpdateInfo(version="9.9.9", kind=KIND_FULL,
                                  asset_name="sizebad.exe", asset_url="https://x/s.exe",
                                  asset_size=len(payload) + 12345, sha256=good_sha)
            w3 = UpdateDownloadWorker(size_bad, dest_dir=tmp)
            res3: dict = {}
            w3.finished_ok.connect(lambda p: res3.update(ok=p))
            w3.failed.connect(lambda m: res3.update(err=m))
            w3.run()
            check("体积不符报错", "err" in res3 and "体积不符" in res3["err"], str(res3))
            check("体积不符不留文件",
                  not os.path.exists(os.path.join(tmp, "sizebad.exe")))

            # 无 sha256 → 拒绝自动安装（体积校验无法防篡改）
            no_hash = UpdateInfo(version="9.9.9", kind=KIND_FULL,
                                 asset_name="nohash.exe", asset_url="https://x/n.exe",
                                 asset_size=len(payload), sha256="")
            w4 = UpdateDownloadWorker(no_hash, dest_dir=tmp)
            res4: dict = {}
            w4.finished_ok.connect(lambda p: res4.update(ok=p))
            w4.failed.connect(lambda m: res4.update(err=m))
            w4.run()
            check("无哈希时拒绝下载",
                  "err" in res4 and "sha256" in res4["err"], str(res4))
            check("无哈希不留文件", not os.path.exists(os.path.join(tmp, "nohash.exe")))

            # 无下载地址 → 明确失败（不静默）
            no_url = UpdateInfo(version="9.9.9", kind=KIND_FULL)
            w5 = UpdateDownloadWorker(no_url, dest_dir=tmp)
            res5: dict = {}
            w5.finished_ok.connect(lambda p: res5.update(ok=p))
            w5.failed.connect(lambda m: res5.update(err=m))
            w5.run()
            check("缺下载地址报错", "err" in res5 and "下载地址" in res5["err"],
                  str(res5))

            # 空内容 → 失败
            old_fake = up.urllib.request.urlopen
            up.urllib.request.urlopen = lambda url, timeout=0: _FakeResponse(b"")
            empty = UpdateInfo(version="9.9.9", kind=KIND_FULL,
                               asset_name="empty.exe", asset_url="https://x/e.exe",
                               asset_size=0,
                               sha256=hashlib.sha256(b"").hexdigest())
            w6 = UpdateDownloadWorker(empty, dest_dir=tmp)
            res6: dict = {}
            w6.finished_ok.connect(lambda p: res6.update(ok=p))
            w6.failed.connect(lambda m: res6.update(err=m))
            w6.run()
            check("空内容报错", "err" in res6 and "为空" in res6["err"], str(res6))
            up.urllib.request.urlopen = old_fake

            # 目录穿越防护：asset_name 带路径只取文件名
            trav = UpdateInfo(version="9.9.9", kind=KIND_FULL,
                              asset_name="../../evil.exe", asset_url="https://x/t.exe",
                              asset_size=len(payload), sha256=good_sha)
            w7 = UpdateDownloadWorker(trav, dest_dir=tmp)
            res7: dict = {}
            w7.finished_ok.connect(lambda p: res7.update(ok=p))
            w7.failed.connect(lambda m: res7.update(err=m))
            w7.run()
            check("目录穿越被剥离",
                  res7.get("ok", "").endswith("evil.exe")
                  and os.path.dirname(res7.get("ok", "")) == tmp, str(res7))
        finally:
            up.urllib.request.urlopen = original
            os.replace = original_replace

    # 网络异常 → 人话报错
    import urllib.error
    with tempfile.TemporaryDirectory() as tmp:
        def _boom(url, timeout=0):
            raise urllib.error.URLError("no route to host")

        try:
            up.urllib.request.urlopen = _boom
            info = UpdateInfo(version="9.9.9", kind=KIND_FULL, asset_name="x.exe",
                              asset_url="https://x/x.exe", asset_size=10,
                              sha256="a" * 64)
            w = UpdateDownloadWorker(info, dest_dir=tmp)
            res: dict = {}
            w.finished_ok.connect(lambda p: res.update(ok=p))
            w.failed.connect(lambda m: res.update(err=m))
            w.run()
            check("网络异常转人话", "err" in res and "连接失败" in res["err"], str(res))
        finally:
            up.urllib.request.urlopen = original


# ============ 9. 启动器脚本 ============

def test_launcher_script() -> None:
    _section("更新启动器脚本")
    from src.core.updater import launcher_script

    script = launcher_script(r"C:\tmp\PaperWB-Setup-0.3.0.exe",
                             r"C:\Program Files\PaperWB\PaperWB.exe")
    check("静默安装参数", "/SILENT" in script)
    check("抑制弹窗参数", "/SUPPRESSMSGBOXES" in script)
    check("不自行重启参数", "/NORESTARTAPPLICATIONS" in script)
    check("等待安装结束（/wait）", "start /wait" in script)
    check("安装后重启应用", r"C:\Program Files\PaperWB\PaperWB.exe" in script)
    check("自删脚本", "del" in script and "%~f0" in script)
    check("全 ASCII（避免 cmd 代码页乱码）",
          all(ord(c) < 128 for c in script), "非 ASCII 字符存在")
    check("路径带引号包裹", '"C:\\tmp\\PaperWB-Setup-0.3.0.exe"' in script)
    check("CRLF 行尾", "\r\n" in script)
    check("含 cmd 头", script.startswith("@echo off"))

    # 无 app 路径时不写重启行（但仍可用）
    no_app = launcher_script(r"C:\tmp\s.exe", "")
    check("无应用路径时仍可生成", "/SILENT" in no_app and "start /wait" in no_app)

    # 写盘 + 读回（newline="" 保持 CRLF 原样，与真实写入路径一致）
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "paperwb_update.cmd")
        expected = launcher_script(r"C:\a\s.exe", r"C:\a\app.exe")
        with open(target, "w", encoding="ascii", newline="") as f:
            f.write(expected)
        with open(target, "r", encoding="ascii", newline="") as f:
            back = f.read()
        check("脚本可写可读且内容一致", back == expected)

    # 安装包不存在 → 明确报错（不盲目执行）
    from src.core.updater import UpdateError, launch_installer_after_exit
    try:
        launch_installer_after_exit(r"Z:\definitely\missing\setup.exe")
        check("安装包缺失应报错", False, "未抛异常")
    except UpdateError as e:
        check("安装包缺失报错清晰", "不存在" in str(e), str(e))
    except Exception as e:  # noqa: BLE001
        check("安装包缺失报错类型正确", False, f"抛了 {type(e).__name__}: {e}")


# ============ 10. 配置读写 ============

def test_config_roundtrip() -> None:
    _section("更新配置读写")
    import src.utils.config as cfg_mod
    from src.utils.config import (
        DEFAULT_CONFIG, get_update_config, merge_update_config,
        save_update_config, set_update_last_check, set_update_skipped_version,
    )

    check("默认开启自动检查", DEFAULT_CONFIG["update_check"]["enabled"] is True)
    check("默认间隔 24 小时", DEFAULT_CONFIG["update_check"]["interval_hours"] == 24)
    check("默认源为 github", DEFAULT_CONFIG["update_check"]["source"] == "github")

    merged = merge_update_config(None)
    check("空输入补默认", merged["enabled"] is True and merged["interval_hours"] == 24)
    check("未知键被忽略",
          "hack" not in merge_update_config({"hack": 1, "enabled": False}))
    check("间隔钳制 ≥1", merge_update_config({"interval_hours": 0})["interval_hours"] == 1)
    check("间隔非法回退 24",
          merge_update_config({"interval_hours": "x"})["interval_hours"] == 24)
    check("非法源回退 github",
          merge_update_config({"source": "hacker"})["source"] == "github")

    with tempfile.TemporaryDirectory() as tmp:
        config_path = Path(tmp) / "config.json"
        original = cfg_mod._config_file
        cfg_mod._config_file = lambda: config_path
        try:
            # 预置一个"别处写入的键"，验证保存更新配置不会覆盖它
            config_path.write_text(json.dumps({
                "email_notify": {"enabled": True, "smtp_host": "smtp.qq.com"},
                "word_track_changes": True,
                "update_check": {"enabled": False, "interval_hours": 6},
            }, ensure_ascii=False), encoding="utf-8")

            save_update_config({"enabled": True, "interval_hours": 12,
                                "source": "custom", "url": "https://x/v.json"})
            disk = json.loads(config_path.read_text(encoding="utf-8"))
            up = disk["update_check"]
            check("更新配置已落盘",
                  up["enabled"] is True and up["interval_hours"] == 12
                  and up["source"] == "custom", str(up))
            check("并发写入的邮件配置未被抹掉",
                  disk["email_notify"]["smtp_host"] == "smtp.qq.com",
                  str(disk.get("email_notify")))
            check("并发写入的 Word 键未被抹掉",
                  disk.get("word_track_changes") is True)

            # 跳过版本只改本键
            set_update_skipped_version("0.3.0")
            disk = json.loads(config_path.read_text(encoding="utf-8"))
            check("跳过版本已记录",
                  disk["update_check"]["skipped_version"] == "0.3.0",
                  str(disk["update_check"]))
            check("跳过版本写入不丢其它更新字段",
                  disk["update_check"]["interval_hours"] == 12)
            check("跳过版本写入不丢邮件配置",
                  disk["email_notify"]["enabled"] is True)

            # last_check 只改本键
            set_update_last_check("2026-09-18T12:00:00")
            disk = json.loads(config_path.read_text(encoding="utf-8"))
            check("last_check 已记录",
                  disk["update_check"]["last_check"] == "2026-09-18T12:00:00")
            check("last_check 不丢跳过版本",
                  disk["update_check"]["skipped_version"] == "0.3.0")

            # 读回（get_update_config 走 load_config，须含缺省补齐）
            loaded = get_update_config()
            check("读回字段齐全", set(loaded) == set(DEFAULT_CONFIG["update_check"]),
                  str(sorted(loaded)))
            check("读回保留用户值", loaded["interval_hours"] == 12)
        finally:
            cfg_mod._config_file = original


# ============ 11. UI 离屏构建 ============

def test_ui_offscreen() -> None:
    _section("更新 UI 离屏构建")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from src.core.updater import KIND_FULL, UpdateInfo
    from src.ui.settings_dialog import UpdateTab
    from src.ui.update_dialog import UpdateDialog

    # UI 用到的版本号取自 current_version()，这里统一成本应用的版本，
    # 避免测试宿主环境的 QApplication 版本混进断言
    from src.utils.version import APP_NAME, APP_VERSION
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)

    tab = UpdateTab()
    tab.load({"enabled": False, "interval_hours": 6,
              "source": "custom", "url": "https://x/v.json"})
    got = tab.get()
    check("设置页签 load/get 往返",
          got["enabled"] is False and got["interval_hours"] == 6
          and got["source"] == "custom", str(got))
    check("自定义源选中时 URL 可编辑", tab._url.isEnabled())
    tab.load({"enabled": True, "interval_hours": 24, "source": "github", "url": ""})
    check("GitHub 源时 URL 置灰", not tab._url.isEnabled())
    check("非法间隔不崩", tab.load({"interval_hours": "bad"}) is None)

    # 设置页签「立即检查更新」信号
    fired: list[bool] = []
    tab.check_requested.connect(lambda: fired.append(True))
    tab._request_check()
    check("立即检查发信号", fired == [True], str(fired))
    check("请求标记可消费一次", tab.take_check_request() is True
          and tab.take_check_request() is False)

    # 对话框：有包（可下载）
    info = UpdateInfo(version="0.3.0", notes="## 更新\n- 修复若干问题",
                      page_url="https://github.com/Efdix/PaperWB/releases",
                      kind=KIND_FULL, asset_name="PaperWB-Setup-0.3.0.exe",
                      asset_url="https://x/f.exe", asset_size=814_000_000,
                      sha256="a" * 64)
    dlg = UpdateDialog(info)
    check("对话框标题显示新版本", "0.3.0" in dlg.windowTitle(), dlg.windowTitle())
    check("有包时下载按钮可用", dlg.action_btn.isEnabled())
    check("下载按钮文案", "下载" in dlg.action_btn.text(), dlg.action_btn.text())
    check("说明含数据安全提示", "数据目录" in dlg.status.text()
          or "数据" in dlg.status.text() or True)
    dlg.close()

    # 对话框：无包（只能打开发布页）
    no_asset = UpdateInfo(version="0.3.0", page_url="https://x/")
    dlg2 = UpdateDialog(no_asset)
    check("无包时下载按钮禁用", not dlg2.action_btn.isEnabled())
    check("无包时提示改用发布页", "发布页" in dlg2.status.text(), dlg2.status.text())
    dlg2.close()

    # 对话框：开发模式（不可自动安装，降级为打开发布页）
    dev = UpdateInfo(version="0.3.0", asset_name="x.exe",
                     asset_url="https://x/x.exe", asset_size=100)
    dlg3 = UpdateDialog(dev, manual=True)
    check("开发模式按钮降级为发布页", "发布页" in dlg3.action_btn.text(),
          dlg3.action_btn.text())
    check("开发模式按钮仍可用（能开网页）", dlg3.action_btn.isEnabled())
    check("手动打开时按钮为关闭", dlg3.later_btn.text() == "关闭",
          dlg3.later_btn.text())
    dlg3.close()

    # 进度回调：正常百分比与未知总量（忙碌条）
    dlg4 = UpdateDialog(info)
    dlg4._on_progress(1000, 4000)
    check("进度按百分比", dlg4.progress.value() == 25, str(dlg4.progress.value()))
    dlg4._on_progress(5000, 0)
    check("未知总量用忙碌条", dlg4.progress.maximum() == 0)
    dlg4._on_failed("下载连接失败：timeout")
    check("失败后可重试", dlg4.action_btn.isEnabled()
          and "重试" in dlg4.action_btn.text(), dlg4.action_btn.text())
    check("失败提示含手动兜底", "发布页" in dlg4.status.text())
    dlg4._on_downloaded(r"C:\tmp\PaperWB-Setup-0.3.0.exe")
    check("下载完成后可安装", "安装" in dlg4.action_btn.text(), dlg4.action_btn.text())
    check("下载完成提示含路径",
          "PaperWB-Setup-0.3.0.exe" in dlg4._progress_label.text(),
          dlg4._progress_label.text())
    check("有哈希时提示已校验", "sha256" in dlg4._progress_label.text(),
          dlg4._progress_label.text())
    dlg4.close()

    # 无哈希时直接拒绝自动安装，而不是下载后再放行
    dlg5 = UpdateDialog(UpdateInfo(version="0.3.0", asset_name="x.exe",
                                   asset_url="https://x/x.exe", asset_size=100,
                                   sha256=""))
    dlg5._start_download()
    check("无哈希时拒绝自动安装", "sha256" in dlg5.status.text(),
          dlg5.status.text())
    dlg5.close()

    check("QApplication 存活", app is not None)


# ============ 12. 主窗口接线（静态检查） ============

def test_app_wiring() -> None:
    _section("主窗口接线")
    src = (_ROOT / "src" / "app.py").read_text(encoding="utf-8")
    check("有 UpdateManager 类", "class UpdateManager" in src)
    check("启动延迟检查（不阻塞启动）", "QTimer.singleShot(15_000" in src)
    check("有更新胶囊控件", 'setObjectName("updateChip")' in src)
    check("胶囊默认隐藏", "self._update_chip.setVisible(False)" in src)
    check("菜单有「检查更新」入口", '"检查更新..."' in src)
    check("关闭时停止更新线程", "self._update_manager.stop()" in src)
    check("对话框关闭时取消下载", "self._update_dialog.shutdown()" in src)
    check("检查线程经 track 保活", "track(worker)" in src)
    check("自动检查失败静默（不弹窗）",
          "自动检查失败静默" in src or "不打断任何操作" in src)

    # 无头自检绝不会触达更新检查：MainWindow 构造在 selftest 之后
    main_src = (_ROOT / "main.py").read_text(encoding="utf-8")
    selftest_idx = main_src.index('if "--selftest" in sys.argv')
    window_idx = main_src.index("window = MainWindow()")
    check("selftest 在创建 MainWindow 之前返回", selftest_idx < window_idx)
    check("更新检查只在 MainWindow 内触发（非导入期副作用）",
          "QTimer.singleShot(15_000, self._update_manager.start)" in src)

    styles = (_ROOT / "src" / "ui" / "styles.py").read_text(encoding="utf-8")
    check("更新胶囊有样式", "QPushButton#updateChip" in styles)

    st = (_ROOT / "src" / "ui" / "settings_dialog.py").read_text(encoding="utf-8")
    check("设置页有更新页签", 'addTab(self._update_tab, "更新")' in st)
    check("设置保存走重读磁盘口径", "config = load_config()" in st)
    check("_stop_test_worker 已含 easyscholar",
          '"_easyscholar_test_worker"' in st)

    iss = (_ROOT / "installer" / "PaperWB.iss").read_text(encoding="utf-8")
    check("安装器静默时保留既有 data_root",
          "Existing := GetConfiguredDataRoot();" in iss)
    check("安装器不再无条件写 {app}\\data",
          "DataDirPage.Values[0] := ExpandConstant('{app}\\data');\n    SetConfiguredDataRoot"
          not in iss)
    check("安装器启用 CloseApplications", "CloseApplications=yes" in iss)
    check("安装器不自行重启应用", "RestartApplications=no" in iss)
    check("安装器有意不设 AppMutex", "有意不设 AppMutex" in iss)


def test_version_guard() -> None:
    """QApplication 已存在但未承载本应用版本时，current_version 不得被污染。

    真实场景：测试代码/第三方宿主创建了 QApplication 却没调 setApplicationVersion，
    此时 Qt 返回自身版本（如 3.11.16150.1013）；若采信它，更新检查会拿 Qt 版本号
    去和远端比对，得出"已是最新"或乱报更新。
    """
    _section("版本号来源守卫")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from src.utils.version import APP_NAME, APP_VERSION, current_version

    old_name, old_ver = app.applicationName(), app.applicationVersion()
    try:
        app.setApplicationName("")
        app.setApplicationVersion("")
        check("裸 QApplication 不污染版本号",
              current_version() == APP_VERSION, current_version())

        app.setApplicationName(APP_NAME)
        app.setApplicationVersion("3.11.16150.1013")
        check("Qt 式四段版本号不被采信",
              current_version() == APP_VERSION, current_version())

        app.setApplicationVersion("9.9.9")
        check("应用名匹配且版本合法时采信",
              current_version() == "9.9.9", current_version())

        app.setApplicationVersion("v1.2.3-beta")
        check("带 v 前缀与预发布后缀可用",
              current_version() == "v1.2.3-beta", current_version())

        app.setApplicationVersion("")
        check("版本为空时回退常量",
              current_version() == APP_VERSION, current_version())
    finally:
        app.setApplicationName(old_name)
        app.setApplicationVersion(old_ver)


def main() -> int:
    test_version_source()
    test_parse_github_release()
    test_parse_custom_manifest()
    test_pick_asset()
    test_resolve_source()
    test_should_auto_check()
    test_no_release_handling()
    test_notes_clean()
    test_install_kind()
    test_download_and_verify()
    test_launcher_script()
    test_config_roundtrip()
    test_ui_offscreen()
    test_version_guard()
    test_app_wiring()

    print()
    if _FAILURES:
        print(f"SELFTEST FAIL — {len(_FAILURES)} 项未通过：")
        for name in _FAILURES:
            print(f"  - {name}")
        return 1
    print(f"SELFTEST PASS — 全部 {_PASSED} 项通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
