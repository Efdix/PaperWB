"""
配置管理 —— 便携化布局：配置与日志贴着程序（exe/仓库根），数据挂在用户可改的 data_root 下。

配置文件位置（便携优先，不可写才回退系统 AppData）::

    打包版:  <安装目录>/config.json        （安装向导写入 data_root；装进只读目录时回退 %APPDATA%/PaperWB/）
    开发版:  <仓库根>/config.json
    回退:    %APPDATA%/PaperWB/config.json （Windows；Linux/macOS 对应 XDG/Application Support）

数据目录结构::

    {data_root}/                          # 默认 <安装目录>/data（开发版 <仓库根>/data），安装时可选
      ├── library/                  # 导入的 PDF 文件
      │   └── *.pdf
      ├── .paperwb/
      │   ├── library.json          # PDF 图书列表
      │   ├── chats/                # 对话历史
      │   ├── states/               # 排版/翻译状态
      │   ├── page_cache/           # 逐页解析缓存（Stage 1）
      │   ├── writing_kb/           # 写作知识库
      │   ├── drafts/               # 编辑器草稿自动保存
      │   ├── polish_history/       # 润色结果历史
      │   ├── tmp/                  # 短命临时文件（Zotero sqlite 副本等）
      │   └── hf_home/              # 运行时联网下载的 HF 模型缓存（无预置模型时）
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# ---- 应用配置目录（便携优先，存放 config.json） ----

def _portable_base_dir() -> Path | None:
    """便携模式基准目录：打包版为 exe 所在目录，开发模式为仓库根；不可写返回 None。"""
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        # src/utils/config.py → 仓库根
        base = Path(__file__).resolve().parents[2]
    try:
        base.mkdir(parents=True, exist_ok=True)
        probe = base / ".paperwb_write_probe"
        probe.touch()
        probe.unlink()
        return base
    except OSError:
        return None


def _appdata_config_dir() -> Path:
    """系统 AppData 配置目录（回退位置，也是旧版配置的迁移源）。"""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(base) / "PaperWB"


def _app_config_dir() -> Path:
    """应用配置目录：便携优先（exe/仓库根同级），不可写回退 AppData。

    首次在便携位置运行时自动迁移 %APPDATA% 旧配置（含 PDFasker 时代），
    保住 API key 与 data_root，旧用户升级无感。
    """
    portable = _portable_base_dir()
    if portable is not None:
        portable.mkdir(parents=True, exist_ok=True)
        new_config = portable / "config.json"
        if not new_config.exists():
            appdata = _appdata_config_dir()
            for legacy_dir in (appdata, appdata.parent / "PDFasker"):
                old_config = legacy_dir / "config.json"
                if old_config.exists():
                    try:
                        shutil.copy2(old_config, new_config)
                        break
                    except OSError:
                        pass
        return portable
    d = _appdata_config_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _default_data_root() -> Path:
    """数据根目录默认值：跟安装目录/仓库根走（便携化）；基准目录不可写时退 %LOCALAPPDATA%。"""
    base = _portable_base_dir()
    if base is not None:
        return base / "data"
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
        return Path(local) / "PaperWB" / "data"
    return Path.home() / "Documents" / "PaperWB_Data"


# ---- 默认配置 ----

DEFAULT_CONFIG: dict = {
    "vision_api": {
        "provider": "GLM（智谱）",
        "api_key": "",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4.6v-flash",
        "description": "多模态接口 — 仅用于图表问答（图片+文字）；建议使用视觉模型",
    },
    "text_api": {
        "provider": "DeepSeek",
        "api_key": "",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-flash",
        "description": "纯文本接口 — 论文问答、翻译、写作、文献检索、跨页整合共用",
    },
    "max_tokens": 1_000_000,
    "llm_max_output_tokens": 8192,  # 单次生成输出上限（0 或负数 = 不限制）
    "data_root": "",          # 空字符串 = 未设置，需首次启动弹窗
    "zotero_data_dir": "",
    "openalex_api_key": "",   # OpenAlex 检索源密钥（可选；空 = 无 key 免费额度）
    "easyscholar_api_key": "",  # EasyScholar 期刊影响因子密钥（可选；空 = 不显示 IF）
    "preparse_enabled": True,  # 后台全库预解析（空闲时本地解析 Zotero PDF，零 LLM）
    "custom_writing_types": {},  # 自定义写作类型: {key: {"label": str, "system_prompt": str}}
    "email_notify": {          # 邮件通知（文献巡视/网页更新提醒）
        "enabled": False,      # 总开关
        "smtp_host": "",       # 如 smtp.qq.com / smtp.163.com / smtp.gmail.com
        "smtp_port": 465,      # 465=SSL, 587=STARTTLS, 25=明文（不推荐）
        "use_ssl": True,       # True=SMTP_SSL(465)，False=STARTTLS(587) 或明文
        "username": "",        # 发件邮箱账号
        "password": "",        # 授权码（邮箱服务商生成的授权码，非登录密码）
        "sender": "",          # 发件人显示地址，空 = 用 username
        "recipients": "",      # 收件人，多个用英文逗号分隔
    },
}


# ---- 路径工具 ----

def _config_file() -> Path:
    """配置文件始终在 AppData 固定位置。"""
    return _app_config_dir() / "config.json"


def _get_data_root(config: dict | None = None) -> str:
    """获取数据根目录（优先 config，fallback 默认值）。"""
    if config is None:
        config = load_config()
    root = config.get("data_root", "")
    if not root:
        root = config.get("library_path", "")  # 向后兼容旧字段
    if not root:
        root = str(_default_data_root())
    return root


def _resolve_data_dir(config: dict | None = None) -> Path:
    """获取数据子目录 .paperwb/（在 data_root 下）。"""
    root = _get_data_root(config)
    d = Path(root) / ".paperwb"
    legacy = Path(root) / ".pdfasker"
    if not d.exists() and legacy.exists():
        try:
            legacy.replace(d)
        except OSError:
            d = legacy
    d.mkdir(parents=True, exist_ok=True)
    return d


def _doc_id(file_path: str) -> str:
    """基于文件路径生成短文档标识符（MD5 前 12 位）。"""
    return hashlib.md5(file_path.encode()).hexdigest()[:12]


# ---- 子目录获取 ----

def get_library_dir() -> Path:
    """PDF 图书馆目录：{data_root}/library/"""
    root = _get_data_root()
    d = Path(root) / "library"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_drafts_dir() -> Path:
    """编辑器草稿目录：{data_root}/.paperwb/drafts/"""
    d = _resolve_data_dir() / "drafts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_polish_history_dir() -> Path:
    """润色历史目录：{data_root}/.paperwb/polish_history/"""
    d = _resolve_data_dir() / "polish_history"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_docx_backup_dir() -> Path:
    """Word 写回前自动备份目录：{data_root}/.paperwb/docx_backup/"""
    d = _resolve_data_dir() / "docx_backup"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_writing_kb_dir() -> Path:
    """写作知识库目录：{data_root}/.paperwb/writing_kb/"""
    d = _resolve_data_dir() / "writing_kb"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_chats_dir() -> Path:
    """对话历史目录：{data_root}/.paperwb/chats/"""
    d = _resolve_data_dir() / "chats"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_states_dir() -> Path:
    """状态缓存目录：{data_root}/.paperwb/states/"""
    d = _resolve_data_dir() / "states"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_page_cache_root_dir() -> Path:
    """逐页缓存根目录：{data_root}/.paperwb/page_cache/"""
    d = _resolve_data_dir() / "page_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_lib_index_dir() -> Path:
    """全文献库问答索引目录：{data_root}/.paperwb/lib_index/"""
    d = _resolve_data_dir() / "lib_index"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_scout_dir() -> Path:
    """文献巡视（定时检索）数据目录：{data_root}/.paperwb/scout/"""
    d = _resolve_data_dir() / "scout"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_stats_dir() -> Path:
    """统计工作台数据目录：{data_root}/.paperwb/stats/"""
    d = _resolve_data_dir() / "stats"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_search_dir() -> Path:
    """检索记录库目录（检索历史 + 黑名单）：{data_root}/.paperwb/search/"""
    d = _resolve_data_dir() / "search"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_watch_dir() -> Path:
    """网页追踪数据目录（监控页配置 + 快照）：{data_root}/.paperwb/watch/"""
    d = _resolve_data_dir() / "watch"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_page_cache_dir(pdf_path: str) -> Path:
    """单篇 PDF 逐页缓存：{data_root}/.paperwb/page_cache/{pdf_md5}/"""
    d = get_page_cache_root_dir() / _doc_id(pdf_path)
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_tmp_dir() -> Path:
    """短命临时文件目录：{data_root}/.paperwb/tmp/（便携化，避免散落系统 %TEMP%）。

    data_root 不可用（未设置且默认位置不可写）时退回系统临时目录。
    """
    try:
        d = _resolve_data_dir() / "tmp"
        d.mkdir(parents=True, exist_ok=True)
        return d
    except OSError:
        return Path(tempfile.gettempdir())


def get_log_dir() -> Path:
    """日志目录：{配置目录}/logs/（便携模式=安装目录/仓库根下 logs，随程序走）。"""
    d = _app_config_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_hf_home_dir() -> str:
    """HuggingFace 缓存根（HF_HOME）：{data_root}/.paperwb/hf_home/。

    用于无预置模型时运行时联网下载的模型缓存（hub 与 xet 子目录都在其下），
    便携化后不再散落 ~/.cache/huggingface。
    """
    d = _resolve_data_dir() / "hf_home"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


# ========== 配置读写 ==========

def load_config() -> dict:
    """加载配置，兼容旧版本格式自动迁移。"""
    cf = _config_file()
    if not cf.exists():
        return DEFAULT_CONFIG.copy()

    try:
        saved = json.loads(cf.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        saved = {}

    config = DEFAULT_CONFIG.copy()

    # ---- 迁移旧 library_path → data_root ----
    if "library_path" in saved and not saved.get("data_root"):
        saved["data_root"] = saved.pop("library_path")

    # ---- 迁移旧 reading_api → parse_api ----
    if "reading_api" in saved and "parse_api" not in saved:
        saved["parse_api"] = saved.pop("reading_api")
        saved["parse_api"]["description"] = "解析接口 — 用于跨页段落整合和论文问答；逐页版式解析由本地模型完成"

    # ---- 迁移旧 review_api → write_api ----
    if "review_api" in saved and "write_api" not in saved:
        saved["write_api"] = saved.pop("review_api")
        saved["write_api"]["description"] = "写作 API — 用于综述引文核查、综述优化"

    # ---- 如果只有旧单 API 格式（api_key 在顶层）----
    if "parse_api" not in saved and "api_key" in saved:
        saved["parse_api"] = {
            "provider": saved.get("provider", "DeepSeek"),
            "api_key": saved.get("api_key", ""),
            "base_url": saved.get("base_url", "https://api.deepseek.com"),
            "model": saved.get("model", "deepseek-v4-flash"),
        }
    if "translate_api" not in saved and "parse_api" in saved:
        saved["translate_api"] = dict(saved["parse_api"])

    # ---- 三套旧接口 → 两套新接口（多模态 + 纯文本）----
    if "text_api" not in saved:
        # 纯文本接口优先继承写作接口（写作对模型要求最高），其次翻译，最后解析
        for old_key in ("write_api", "translate_api", "parse_api"):
            if old_key in saved:
                saved["text_api"] = dict(saved[old_key])
                break
    if "vision_api" not in saved:
        # 多模态接口继承解析接口；但解析模型非视觉模型时置空（图问答自动降级为纯文本提问）
        parse_cfg = saved.get("parse_api")
        if parse_cfg and _is_vision_model(parse_cfg.get("model", "")):
            saved["vision_api"] = dict(parse_cfg)
        else:
            saved["vision_api"] = dict(DEFAULT_CONFIG["vision_api"])
            saved["vision_api"]["api_key"] = ""
            saved["vision_api"]["base_url"] = ""
            saved["vision_api"]["model"] = ""

    # 旧三键不再使用
    for legacy_key in ("parse_api", "translate_api", "write_api"):
        saved.pop(legacy_key, None)

    config.update(saved)
    # 旧版本的 Stage 1 引擎/并发选项已移除，统一固定为本地版式解析。
    for legacy_key in ("stage1_mode", "stage1_concurrency", "stage1_parser"):
        config.pop(legacy_key, None)
    return config


def save_config(config: dict) -> None:
    """保存配置到 JSON 文件。对路径字段做 normalize 防乱码。"""
    if config.get("data_root"):
        config["data_root"] = os.path.normpath(str(config["data_root"]))
    if config.get("zotero_data_dir"):
        config["zotero_data_dir"] = os.path.normpath(str(config["zotero_data_dir"]))
    cf = _config_file()
    cf.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


# ---- 自定义写作类型 ----

def get_custom_writing_types() -> dict:
    """返回用户自定义写作类型: {key: {"label": str, "system_prompt": str}}。"""
    return load_config().get("custom_writing_types", {}) or {}


def add_custom_writing_type(key: str, label: str, system_prompt: str) -> None:
    """新增或更新一个自定义写作类型。"""
    config = load_config()
    custom = config.get("custom_writing_types", {}) or {}
    custom[key] = {"label": label, "system_prompt": system_prompt}
    config["custom_writing_types"] = custom
    save_config(config)


def remove_custom_writing_type(key: str) -> None:
    """删除一个自定义写作类型。"""
    config = load_config()
    custom = config.get("custom_writing_types", {}) or {}
    if key in custom:
        custom.pop(key)
        config["custom_writing_types"] = custom
        save_config(config)


def has_data_root() -> bool:
    """检查是否已设置数据根目录。"""
    config = load_config()
    return bool(config.get("data_root", ""))


def get_vision_api(config: dict) -> dict:
    return config.get("vision_api", DEFAULT_CONFIG["vision_api"])


def get_text_api(config: dict) -> dict:
    return config.get("text_api", DEFAULT_CONFIG["text_api"])


def get_openalex_api_key(config: dict | None = None) -> str:
    """OpenAlex 检索源密钥（可选，空字符串 = 无 key 免费额度模式）。"""
    if config is None:
        config = load_config()
    return str(config.get("openalex_api_key", "") or "").strip()


def get_easyscholar_api_key(config: dict | None = None) -> str:
    """EasyScholar 期刊影响因子密钥（可选，空字符串 = 不启用 IF 显示）。"""
    if config is None:
        config = load_config()
    return str(config.get("easyscholar_api_key", "") or "").strip()


def get_email_config(config: dict | None = None) -> dict:
    """邮件通知配置（合并默认值，保证字段齐全）。"""
    if config is None:
        config = load_config()
    merged = dict(DEFAULT_CONFIG["email_notify"])
    saved = config.get("email_notify")
    if isinstance(saved, dict):
        merged.update({k: v for k, v in saved.items() if k in merged})
    return merged


def save_email_config(cfg: dict) -> None:
    """保存邮件通知配置。"""
    config = load_config()
    merged = dict(DEFAULT_CONFIG["email_notify"])
    if isinstance(cfg, dict):
        merged.update({k: v for k, v in cfg.items() if k in merged})
    config["email_notify"] = merged
    save_config(config)


def _is_vision_model(model: str) -> bool:
    """判断模型名是否属于已知视觉模型（用于旧配置迁移时决定多模态接口是否可用）。"""
    from ..core.llm_client import VISION_MODELS
    return model.strip().lower() in VISION_MODELS


# ========== PDF 图书馆 ==========

def _library_file(config: dict | None = None) -> Path:
    return _resolve_data_dir(config) / "library.json"


def load_library() -> list[dict]:
    lf = _library_file()
    if lf.exists():
        try:
            return json.loads(lf.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return []


def save_library(library: list[dict]) -> None:
    lf = _library_file()
    lf.write_text(json.dumps(library, ensure_ascii=False, indent=2), encoding="utf-8")


def add_pdf_to_library(pdf_info: dict) -> None:
    lib = load_library()
    for item in lib:
        if item.get("path") == pdf_info.get("path"):
            item.update(pdf_info)
            save_library(lib)
            return
    lib.append(pdf_info)
    save_library(lib)


def remove_pdf_from_library(pdf_path: str) -> None:
    lib = [item for item in load_library() if item.get("path") != pdf_path]
    save_library(lib)


def get_library_folders(library: list[dict]) -> list[str]:
    return sorted({item.get("folder", "") for item in library if item.get("folder")})


# ========== 对话历史 ==========

def load_chat_history(file_path: str) -> list[dict]:
    f = get_chats_dir() / f"{_doc_id(file_path)}.json"
    if f.exists():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            pass
    return []


def save_chat_history(file_path: str, messages: list[dict]) -> None:
    f = get_chats_dir() / f"{_doc_id(file_path)}.json"
    f.write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")


def delete_chat_history(file_path: str) -> None:
    f = get_chats_dir() / f"{_doc_id(file_path)}.json"
    if f.exists():
        f.unlink()


# ========== 状态持久化 ==========

def load_doc_state(file_path: str) -> dict:
    f = get_states_dir() / f"{_doc_id(file_path)}.json"
    if f.exists():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_doc_state(file_path: str, state: dict) -> None:
    f = get_states_dir() / f"{_doc_id(file_path)}.json"
    f.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def delete_doc_state(file_path: str) -> None:
    f = get_states_dir() / f"{_doc_id(file_path)}.json"
    if f.exists():
        f.unlink()


# ========== 逐页解析缓存（Stage 1） ==========

def load_page_cache(pdf_path: str, page_num: int) -> dict | None:
    f = get_page_cache_dir(pdf_path) / f"page_{page_num:03d}.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_page_cache(pdf_path: str, page_num: int, data: dict) -> None:
    d = get_page_cache_dir(pdf_path)
    f = d / f"page_{page_num:03d}.json"
    f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_page_manifest(pdf_path: str) -> dict | None:
    f = get_page_cache_dir(pdf_path) / "_manifest.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_page_manifest(pdf_path: str, manifest: dict) -> None:
    d = get_page_cache_dir(pdf_path)
    f = d / "_manifest.json"
    f.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def delete_page_cache(pdf_path: str) -> None:
    import shutil
    d = get_page_cache_dir(pdf_path)
    if d.exists():
        shutil.rmtree(str(d))


# ========== 草稿 ==========

def load_draft(profile_name: str) -> str:
    """加载指定知识库的编辑器草稿。"""
    f = get_drafts_dir() / f"{profile_name}.txt"
    if f.exists():
        try:
            return f.read_text(encoding="utf-8")
        except OSError:
            pass
    return ""


def save_draft(profile_name: str, text: str) -> None:
    """保存编辑器草稿。空文本会覆盖旧草稿（用户有意清空）。"""
    f = get_drafts_dir() / f"{profile_name}.txt"
    f.write_text(text, encoding="utf-8")


# ========== 润色历史 ==========

MAX_POLISH_HISTORY = 20


def load_polish_history(profile_name: str) -> list[dict]:
    """加载指定知识库的润色历史。"""
    f = get_polish_history_dir() / f"{profile_name}.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return []


def save_polish_entry(profile_name: str, entry: dict) -> None:
    """追加一条润色记录，保留最近 MAX_POLISH_HISTORY 条。"""
    history = load_polish_history(profile_name)
    history.append(entry)
    if len(history) > MAX_POLISH_HISTORY:
        history = history[-MAX_POLISH_HISTORY:]
    f = get_polish_history_dir() / f"{profile_name}.json"
    f.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


# ========== LLM 输出上限 ==========

DEFAULT_MAX_OUTPUT_TOKENS = 8192


def get_max_output_tokens() -> int | None:
    """单次 LLM 调用的输出上限（None = 不传该参数，由服务端决定）。

    长报告类调用（草稿整体评价、风格分析）必须显式设置：不传时输出长度
    完全由服务端默认值决定，长草稿容易撞上限导致 JSON 从中间截断。
    """
    try:
        value = int(load_config().get("llm_max_output_tokens",
                                      DEFAULT_MAX_OUTPUT_TOKENS))
    except (TypeError, ValueError):
        return DEFAULT_MAX_OUTPUT_TOKENS
    return value if value > 0 else None


# ========== 知识库导出/导入 ==========

def get_sync_dir() -> str:
    """上次知识库导出/导入所在的目录（空 = 未用过）。"""
    return str(load_config().get("kb_sync_dir", "") or "")


def set_sync_dir(path: str) -> None:
    cfg = load_config()
    cfg["kb_sync_dir"] = str(path or "")
    save_config(cfg)


# ========== Word 交互（最近文件 / 绑定记忆 / 修订写回偏好） ==========

MAX_RECENT_WORD_FILES = 10
MAX_DOCX_BACKUPS = 20


def get_recent_word_files() -> list[str]:
    """最近打开的 Word 文档路径（新的在前，去重）。"""
    cfg = load_config()
    files = cfg.get("recent_word_files", [])
    if not isinstance(files, list):
        return []
    return [str(x) for x in files if x]


def push_recent_word_file(path: str) -> None:
    """记录最近打开的 Word 文档（置顶、去重、截断）。"""
    path = str(path)
    if not path:
        return
    cfg = load_config()
    files = [str(x) for x in cfg.get("recent_word_files", []) if x]
    files = [path] + [x for x in files if x != path]
    cfg["recent_word_files"] = files[:MAX_RECENT_WORD_FILES]
    save_config(cfg)


def get_word_binding(profile_name: str) -> dict | None:
    """知识库上次绑定的 Word 文档：{"path":…, "mtime":…} 或 None。"""
    cfg = load_config()
    bindings = cfg.get("word_bindings", {})
    binding = bindings.get(profile_name) if isinstance(bindings, dict) else None
    return binding if isinstance(binding, dict) and binding.get("path") else None


def set_word_binding(profile_name: str, path: str, mtime: float = 0.0) -> None:
    """记录知识库当前绑定的 Word 文档（跨会话恢复用）。"""
    cfg = load_config()
    bindings = cfg.setdefault("word_bindings", {})
    if path:
        bindings[profile_name] = {"path": str(path), "mtime": float(mtime or 0.0)}
    else:
        bindings.pop(profile_name, None)
    save_config(cfg)


def get_word_track_changes() -> bool:
    """保存时把 AI/编辑修改写为 Word 修订（track changes）的偏好。"""
    return bool(load_config().get("word_track_changes", False))


def set_word_track_changes(enabled: bool) -> None:
    cfg = load_config()
    cfg["word_track_changes"] = bool(enabled)
    save_config(cfg)


# ========== 草稿评价持久化 ==========


def get_reviews_dir() -> Path:
    """评价结果目录：{data_root}/.paperwb/reviews/"""
    d = _resolve_data_dir() / "reviews"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_review(profile_name: str, review: dict) -> Path:
    """保存整体评价结果（覆盖式），返回落盘文件路径。"""
    f = get_reviews_dir() / f"{profile_name}.json"
    f.write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
    return f


def load_review(profile_name: str) -> dict | None:
    """加载已保存的整体评价结果。不存在则返回 None。"""
    f = get_reviews_dir() / f"{profile_name}.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return None


def delete_review(profile_name: str) -> None:
    """删除整体评价结果。"""
    f = get_reviews_dir() / f"{profile_name}.json"
    if f.exists():
        try:
            f.unlink()
        except OSError:
            pass
