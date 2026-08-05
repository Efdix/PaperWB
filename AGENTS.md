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
main.py                    # 入口: QApplication + MainWindow
requirements.txt
src/
├── app.py                 # MainWindow — 首次启动弹窗(FirstLaunchDialog) + 信号枢纽 + 三套 LLMClient
├── core/
│   ├── pdf_parser.py      # PDF 底层工具（PyMuPDF）: 文本提取/渲染
│   ├── pdf_processor.py   # 核心！两阶段视觉 LLM 管线:
│   │                      #   Stage 1: 逐页(图片+文本) → 视觉LLM → 结构化JSON → 缓存
│   │                      #   Stage 2: 读缓存 → LLM跨页整合 → StructuredDocument → UI
│   ├── llm_client.py      # OpenAI 兼容 API 客户端 + 5 个提供商预设(DeepSeek/Mimo/OpenCode Go/OpenCode Zen/自定义)
│   ├── context_manager.py # Token 预算管理: 1M窗口，"前70%+后30%"截断策略
│   ├── zotero_parser.py   # Zotero SQLite 解析器: 自动检测/文献搜索/主题排序（支持指定路径不全局探测）
│   ├── unified_writer.py  # 统一润色+引文核查: UNIFIED_PROMPT + VERIFY_ONLY_PROMPT + 多层容错 JSON 解析
│   ├── writing_coach.py   # 写作教练: 知识库管理/写作习惯分析/期刊格式分析/引用密度分析
│   ├── writing_prompts.py # 四种写作类型的系统提示词（综述/论文/专利/软著）
│   └── pubmed_searcher.py # PubMed E-utilities 检索客户端（esearch + efetch）
├── ui/
│   ├── pdf_viewer.py      # 结构化阅读面板: ParagraphCard 按 element_type 渲染+中英文翻译
│   ├── pdf_list_panel.py  # 论文库侧边栏: 拖拽导入/文件夹分类/右键菜单/进度显示
│   ├── chat_panel.py      # 聊天面板: Markdown 气泡/流式渲染
│   ├── writing_panel.py   # 写作面板: 编辑器+知识库管理+Zotero+AI辅助(润色/核查/文献补充)+自动保存+字数统计
│   ├── diff_dialog.py     # 润色对比对话框: 内联 diff(单编辑框)+导航栏(上一处/下一处/接受/拒绝)+AI 对话+引用高亮
│   ├── lit_search_dialog.py # 文献补充对话框: LLM 双轨推荐(已知文献+搜索词)→PubMed 检索→导出 CSV（非模态）
│   ├── settings_dialog.py # API 设置对话框: 三标签页+处理设置(同步/异步)+Zotero路径
│   └── styles.py          # Catppuccin 暗色主题 QSS
└── utils/
    ├── config.py          # 持久化层: 配置(含多API/数据根目录/Zotero)+图书馆+聊天+缓存+草稿+润色历史
    └── layout.py          # 递归布局高度计算（heightForWidth）
```

## 架构说明

### 数据存储架构

- 配置文件: `%APPDATA%/PDFasker/config.json`（固定路径）
- 数据根目录: 首次启动弹窗选择，存储在 config 的 `data_root` 字段
- 所有用户数据在 `{data_root}/.pdfasker/` 下，包括 library.json、chats、states、page_cache、writing_kb、drafts、polish_history
- PDF 文件存储在 `{data_root}/library/` 下
- 菜单「设置 → 数据目录...」可随时更改 data_root

### 两阶段解析管线（阅读）

- **Stage 1**: PDF 导入后自动逐页发给视觉 LLM，每页独立缓存，支持断点续传和并发控制
- **Stage 2**: 用户点击论文时，LLM 跨页整合为 StructuredDocument
- 右键菜单支持分开重跑 Stage 1 / Stage 2

### 三套独立 API

- **阅读-解析** (`parse_api`): 逐页视觉解析 + 跨页整合 + 论文问答（需视觉多模态模型）
- **阅读-翻译** (`translate_api`): 段落中英对照翻译（可用便宜/免费模型）
- **写作** (`write_api`): 引文核查 + 风格分析 + 润色 + 文献推荐（需强推理模型）

### 写作系统

- **WritingCoach**: 知识库(CRUD)→风格分析(六维度+引用密度)→AI辅助(润色/核查/文献补充)
- **UnifiedWriter**: 统一润色+引文核查，支持常规模式和仅核查模式(verify_only)
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

### Zotero 集成

- 写作面板工具栏 + API 设置写作标签页，两处联动设置 Zotero 数据目录
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
