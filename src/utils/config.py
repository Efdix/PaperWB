"""
配置管理 —— 所有数据存储在用户设定的数据根目录下。

配置文件位置::

    %APPDATA%/PDFasker/config.json   (Windows)
    ~/.config/PDFasker/config.json   (Linux)
    ~/Library/Application Support/PDFasker/config.json  (macOS)

数据目录结构::

    {data_root}/
      ├── library/                  # 导入的 PDF 文件
      │   └── *.pdf
      ├── .pdfasker/
      │   ├── config.json           # (不存这里，存 %APPDATA%)
      │   ├── library.json          # PDF 图书列表
      │   ├── chats/                # 对话历史
      │   ├── states/               # 排版/翻译状态
      │   ├── page_cache/           # 逐页解析缓存（Stage 1）
      │   ├── writing_kb/           # 写作知识库
      │   ├── drafts/               # 编辑器草稿自动保存
      │   └── polish_history/       # 润色结果历史
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

# ---- 应用配置目录（固定位置，存放 config.json） ----

def _app_config_dir() -> Path:
    """跨平台的 AppData 配置目录。"""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
    d = Path(base) / "PDFasker"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _default_data_root() -> Path:
    return Path.home() / "Documents" / "PDFasker_Data"


# ---- 默认配置 ----

DEFAULT_CONFIG: dict = {
    "parse_api": {
        "provider": "DeepSeek",
        "api_key": "",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-flash",
        "description": "阅读-解析 API — 用于 PDF 逐页视觉解析、跨页整合、论文问答（需视觉多模态能力）",
    },
    "translate_api": {
        "provider": "DeepSeek",
        "api_key": "",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-flash",
        "description": "阅读-翻译 API — 用于段落中英对照翻译（可用便宜快速的模型）",
    },
    "write_api": {
        "provider": "DeepSeek",
        "api_key": "",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-flash",
        "description": "写作 API — 用于综述引文核查、综述优化（需强推理能力）",
    },
    "max_tokens": 1_000_000,
    "data_root": "",          # 空字符串 = 未设置，需首次启动弹窗
    "zotero_data_dir": "",
    "stage1_mode": "async",
    "stage1_concurrency": 3,
    "stage1_parser": "vision",   # 逐页解析引擎: vision（视觉LLM） | docling（本地，快/省）
    "custom_writing_types": {},  # 自定义写作类型: {key: {"label": str, "system_prompt": str}}
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
    """获取数据子目录 .pdfasker/（在 data_root 下）。"""
    root = _get_data_root(config)
    d = Path(root) / ".pdfasker"
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
    """编辑器草稿目录：{data_root}/.pdfasker/drafts/"""
    d = _resolve_data_dir() / "drafts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_polish_history_dir() -> Path:
    """润色历史目录：{data_root}/.pdfasker/polish_history/"""
    d = _resolve_data_dir() / "polish_history"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_writing_kb_dir() -> Path:
    """写作知识库目录：{data_root}/.pdfasker/writing_kb/"""
    d = _resolve_data_dir() / "writing_kb"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_chats_dir() -> Path:
    """对话历史目录：{data_root}/.pdfasker/chats/"""
    d = _resolve_data_dir() / "chats"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_states_dir() -> Path:
    """状态缓存目录：{data_root}/.pdfasker/states/"""
    d = _resolve_data_dir() / "states"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_page_cache_root_dir() -> Path:
    """逐页缓存根目录：{data_root}/.pdfasker/page_cache/"""
    d = _resolve_data_dir() / "page_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_page_cache_dir(pdf_path: str) -> Path:
    """单篇 PDF 逐页缓存：{data_root}/.pdfasker/page_cache/{pdf_md5}/"""
    d = get_page_cache_root_dir() / _doc_id(pdf_path)
    d.mkdir(parents=True, exist_ok=True)
    return d


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
        saved["parse_api"]["description"] = "阅读-解析 API — 用于 PDF 逐页视觉解析、跨页整合、论文问答"

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

    config.update(saved)
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


def get_parse_api(config: dict) -> dict:
    return config.get("parse_api", DEFAULT_CONFIG["parse_api"])


def get_translate_api(config: dict) -> dict:
    return config.get("translate_api", DEFAULT_CONFIG["translate_api"])


def get_write_api(config: dict) -> dict:
    return config.get("write_api", DEFAULT_CONFIG["write_api"])


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
            return json.loads(f.read_text(encoding="utf-8"))
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
            return json.loads(f.read_text(encoding="utf-8"))
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


# ========== 草稿评价持久化 ==========


def get_reviews_dir() -> Path:
    """评价结果目录：{data_root}/.pdfasker/reviews/"""
    d = _resolve_data_dir() / "reviews"
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_review(profile_name: str, review: dict) -> None:
    """保存整体评价结果（覆盖式）。"""
    f = get_reviews_dir() / f"{profile_name}.json"
    f.write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")


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
