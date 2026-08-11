# PDFasker — AI 论文解读助手

## 运行环境

- Python 环境: `D:\science\miniforge3\envs\PDFasker`（conda env，Python 3.11）
- 所有 Python 命令前需激活: `conda activate PDFasker`
- 包管理: pip + `requirements.txt`
- 3 个核心依赖: `PySide6==6.11.1` `openai==2.44.0` `PyMuPDF==1.27.2.3`
- （`python-dotenv` 在 requirements.txt 中但未被使用）

## 项目入口

```powershell
conda activate PDFasker
python main.py
```

## 项目结构

```
main.py                    # 入口: QApplication + MainWindow（自动 UTF-8 重启；打包版: DLL 路径/解压等待/重库预加载 + --selftest 无头自检）
requirements.txt
src/
├── app.py                 # MainWindow — 首次启动弹窗(FirstLaunchDialog) + 信号枢纽 + 三套 LLMClient + Zotero watcher
├── core/
│   ├── pdf_parser.py      # PDF 底层工具（PyMuPDF）: 文本提取/渲染
│   ├── pdf_processor.py   # 核心！两阶段管线:
│   │                      #   Stage 1: Docling 本地布局解析 → JSON → 缓存
│   │                      #   Stage 2: 读缓存 → LLM跨页整合 → StructuredDocument → UI
│   ├── docling_parser.py  # Docling 本地解析器: PDF → 与视觉管线兼容的逐页元素（设置 HF 镜像/禁用编译）
│   ├── llm_client.py      # OpenAI 兼容 API 客户端 + json_mode(response_format) + 5 个提供商预设
│   ├── context_manager.py # Token 预算管理: 长文档走 BM25 检索增强（只发相关段落），短文档用"前70%+后30%"
│   ├── retriever.py       # 轻量本地检索器: Retriever 接口 + Bm25Retriever（预留向量升级）
│   ├── zotero_parser.py   # Zotero SQLite 解析器: 集合层级/文献/附件 + reload()（临时副本只读，支持指定路径不全局探测）
│   ├── zotero_watcher.py  # Zotero 周期同步: 启动加载后每 30 分钟自动重载（不做文件事件监听）+ 手动刷新
│   ├── unified_writer.py  # 统一润色+引文核查: 证据检索化(按声明检索相关段落) + json_mode + 多层容错 JSON
│   ├── writing_coach.py   # 写作教练: 知识库管理/写作习惯分析/期刊格式分析/引用密度分析
│   ├── writing_prompts.py # 四种写作类型的系统提示词（综述/论文/专利/软著）
│   └── pubmed_searcher.py # PubMed E-utilities 检索客户端（esearch + efetch）
├── ui/
│   ├── pdf_viewer.py      # 结构化阅读面板: ParagraphCard 按 element_type 渲染+中英文翻译(多并发+滚动自动翻译)；Stage1 完成后自动跨页整合
│   ├── pdf_list_panel.py  # 左侧面板: Tab1 Zotero 只读文献库 + Tab2 其它文献
│   ├── zotero_panel.py    # Zotero 树形视图: 集合树+文献+PDF附件标记，周期同步/手动刷新驱动刷新
│   ├── chat_panel.py      # 聊天面板: Markdown 气泡/流式渲染
│   ├── writing_panel.py   # 写作面板: 编辑器+知识库管理+Zotero+AI辅助(润色/仅核查/文献补充)+自动保存+字数统计
│   ├── diff_dialog.py     # 润色对比对话框: 内联 diff(单编辑框)+导航栏(上一处/下一处/接受/拒绝)+AI 对话+引用高亮
│   ├── lit_search_dialog.py # 文献补充对话框: LLM 双轨推荐(已知文献+搜索词)→PubMed 检索→导出 CSV（非模态）
│   ├── settings_dialog.py # 设置对话框: API接口设置(解析/翻译/写作)+Zotero路径+缓存文件存储路径+连接测试
│   └── styles.py          # 暖白研究工作台主题 QSS
└── utils/
    ├── config.py          # 持久化层: 配置(含多接口/数据根目录/Zotero)+图书馆+聊天+缓存+草稿+润色历史
    ├── layout.py          # 递归布局高度计算（heightForWidth）
    └── threads.py         # 运行中 QThread 全局保活注册表（track/sweep），杜绝运行中销毁崩溃
```

## 架构说明

### 数据存储架构

- 配置文件: `%APPDATA%/PDFasker/config.json`（固定路径）
- 数据根目录: 首次启动弹窗选择，存储在 config 的 `data_root` 字段
- 所有用户数据在 `{data_root}/.pdfasker/` 下，包括 library.json、chats、states、page_cache、writing_kb、drafts、polish_history
- PDF 文件存储在 `{data_root}/library/` 下
- 菜单「设置 → 缓存文件存储路径...」或设置对话框底部可随时更改 data_root（设置对话框中改名「缓存文件存储路径设置」）

### 两阶段解析管线（阅读）

- **Stage 1**: PDF 导入后由 Docling 本地版式解析，每页独立缓存，支持断点续传；不再调用视觉模型，也不再配置并发页数；Docling 漏检的位图区域自动用 PyMuPDF 兜底补成 figure 元素
- **Stage 2**: 点击论文后自动触发（无手动「AI 整合」按钮），**本地规则组装**（不调 LLM）：读页缓存 → 章节/正文/引文/图/表归类绑定（图挂载对应页渲染图+回填图注，引文优先折叠显示）→ StructuredDocument → UI；跨页被截断的段落（章节间切页、表格断页）由后台线程按文本特征合并，合并结果缓存（merged_seams）
- 旧版 LLM 跨页整合缓存不再使用：状态文件标记 `doc_format: fast`，旧 `structured_document` 首次打开时自动重建（图注/图序从页面文本重新绑定，见 `rebuild_document_fast`）
- 右键菜单: 本地库「重新解析整合」（清页缓存全流程重跑）与分开重跑「重新逐页解析」「重新跨页整合」；Zotero 文献只保留「重新解析整合」+「打开文件位置」
- 长文档问答走 BM25 检索增强（`context_manager` + `retriever`），只发相关段落而非全量截断
- 列表已整合文献以浅绿背景标记

### 三套独立 API

- **解析** (`parse_api`): 论文问答（长文档 BM25 检索增强）+ 图表内容问答（图问答把渲染图 base64 附加为多模态消息，见 `MainWindow._attach_vision_message`）；逐页解析与跨页段落整合由本地完成，不耗 LLM
- **翻译** (`translate_api`): 段落中英对照翻译（可用便宜/免费模型）
- **写作** (`write_api`): 引文核查 + 风格分析 + 润色 + 文献推荐（需强推理模型）

### 写作系统

- **WritingCoach**: 知识库(CRUD)→风格分析(六维度+引用密度)→AI辅助(润色/核查/文献补充)
- **UnifiedWriter**: 统一润色+引文核查，支持常规模式和仅核查模式(verify_only)；引文证据按声明检索相关段落，不再整篇塞全文
- **DiffDialog**: 内联 diff 展示 + 导航工具栏 + 逐项接受/拒绝 + 可编辑 diff + AI 对话（非模态）
- **LitSearchDialog**: 双轨文献推荐——LLM 已知文献 + PubMed 检索词（非模态）
- 支持 4 种写作类型: 综述/研究型论文/专利/软著
- 编辑器自动保存（每30秒）到 `{data_root}/.pdfasker/drafts/`
- 润色历史保存（最近20条）到 `{data_root}/.pdfasker/polish_history/`

### 引用高亮规则

润色 diff 编辑器中支持四种引用格式的标黄：
- `(Author et al., 2024)` 和 `(Author & Author, 2017; Author & Author, 2021)` 多引用分号分隔
- `[1]` `[2,3]` `[4-7]` 数字编号
- `（中文等，2024）` 中文括号
- `Author等（2024）` 无括号中文

### Zotero 集成（只读 + 周期同步）

- 左侧面板 Tab1「Zotero 文献库」: 只读镜像 Zotero 集合树（集合→文献→PDF 附件），点击文献直接用两阶段管线阅读（不导入本地库）
- 周期同步: 启动时加载一次，此后 `ZoteroWatcher` 每 30 分钟由 QTimer 后台 `reload()` 一次并按 key 做差异刷新 UI；**不做文件事件监听**（避免 Windows 高 I/O 自激循环）；面板「刷新」按钮可立即手动同步
- **只读铁律**: 所有访问通过系统临时目录的数据库副本（`tempfile`），绝不写 Zotero 数据目录；PDF 只读打开
- Zotero 数据目录在设置对话框「API 接口设置」下方单独一格配置（「Zotero 文献库路径设置」），阅读与写作共用；再往下是「缓存文件存储路径设置」
- 用户显式指定路径时，仅在该目录下搜索 zotero.sqlite（不全局探测）
- 年份后缀自动去字母（`2025a` → `2025`）匹配 Zotero
- 优先选择有 PDF 附件的匹配条目（按标题去重）

### 技术要点

- 所有 LLM 调用在 QThread 中执行，不阻塞 UI
- 缓存校验使用文件 mtime
- Zotero 数据库复制到临时目录避免写锁
- 使用 PySide6 Signals 进行组件间通信（不直接耦合）
- 各对话框使用非模态 (`show()` + `accepted_signal`) 以支持并行操作
- 对话框设置 `WindowMaximizeButtonHint | WindowMinimizeButtonHint | Window` 以支持任务栏独立分组
- 无 max_tokens 硬限制（依赖模型自身容量）

## 测试

- 无测试框架/Lint/Typecheck 配置
- 所有 Python 文件使用 `from __future__ import annotations` 和类型注解
- 测试数据在 `test/` 目录下（含示例 PDF、写作草稿、缓存快照）
