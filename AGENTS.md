# PDFasker — AI 论文解读助手

## 运行环境

- Python 环境: `D:\Science\miniforge\envs\PDFasker`（conda env，Python 3.11）
- 所有 Python 命令前需激活: `conda activate PDFasker`
- 包管理: pip + `requirements.txt`（无 lock 文件）
- 4 个依赖: PySide6==6.11.1, openai==2.44.0, PyMuPDF==1.27.2.3, python-dotenv==1.2.2

## 项目入口

```
conda activate PDFasker
python main.py
```

## 项目结构

```
main.py                    # 入口: QApplication + MainWindow
requirements.txt
PDFasker.spec              # PyInstaller 打包配置 -> dist/PDFasker.exe
src/
├── app.py                 # MainWindow — 信号连接枢纽，三套 LLMClient
├── core/
│   ├── pdf_parser.py      # PDF 底层工具（PyMuPDF）: 文本提取/渲染/图片提取
│   ├── pdf_processor.py   # 核心！两阶段视觉 LLM 管线:
│   │                      #   Stage 1: 逐页(图片+文本) → 视觉LLM → 结构化JSON → 缓存
│   │                      #   Stage 2: 读缓存 → LLM跨页整合 → StructuredDocument → UI
│   ├── llm_client.py      # OpenAI 兼容 API 客户端: 流式/同步/图片消息
│   ├── context_manager.py # Token 预算管理: 1M窗口，"前70%+后30%"截断策略
│   ├── zotero_parser.py   # Zotero SQLite 解析器: 自动检测/文献搜索/主题排序
│   ├── review_checker.py  # 引文核查引擎: 提取声明→Zotero匹配→对照原文→评估
│   ├── writing_coach.py   # 写作教练: 知识库管理/风格指南生成/润色/改写/遗漏文献检测
│   └── writing_prompts.py # 四种写作类型的系统提示词（综述/论文/专利/软著）
├── ui/
│   ├── pdf_viewer.py      # 结构化阅读面板: ParagraphCard 按 element_type 渲染
│   ├── pdf_list_panel.py  # 论文库侧边栏: 拖拽导入/文件夹分类/右键菜单
│   ├── chat_panel.py      # 聊天面板: Markdown 气泡/流式渲染
│   ├── writing_panel.py   # 写作面板: 编辑器+Zotero+AI辅助(润色/引文改写/核查/推荐)
│   ├── settings_dialog.py # API 设置对话框: 三标签页+处理设置+Zotero路径
│   └── styles.py          # Catppuccin 暗色主题 QSS
└── utils/
    ├── config.py          # 持久化层: 配置/图书馆/聊天/缓存 读写
    └── layout.py          # 递归布局高度计算（heightForWidth）
```

## 架构说明

### 两阶段解析管线（阅读）
- **Stage 1**: PDF 导入后自动逐页发给视觉 LLM，每页独立缓存，支持断点续传和并发控制（默认3页）
- **Stage 2**: 用户点击论文时，LLM 跨页整合为 StructuredDocument，含 display_elements / metadata_pool / figures / tables / references / toc
- 右键菜单支持分开重跑 Stage 1 / Stage 2

### 三套独立 API
- **阅读-解析** (`parse_api`): 逐页视觉解析 + 跨页整合 + 论文问答（需视觉多模态模型）
- **阅读-翻译** (`translate_api`): 段落中英对照翻译（可用便宜模型）
- **写作** (`write_api`): 引文核查 + 风格分析 + 润色 + 改写 + 文献推荐（需强推理模型）

### 写作系统（与阅读并列）
- **WritingCoach**: 三层架构——知识库(CRUD)→风格指南生成(LLM分析)→AI辅助(润色/改写/遗漏文献检测)
- **ReviewChecker**: 四步骤引文核查——提取声明→Zotero匹配(三策略级联)→逐条对照原文→整体评估
- **Semantic Scholar 集成**: 推荐 API + 搜索 API，结果可导出 CSV
- 支持 4 种写作类型: 综述/研究型论文/专利/软著

### 技术要点
- 所有 LLM 调用在 QThread 中执行，不阻塞 UI
- 缓存校验使用文件 mtime
- Zotero 数据库复制到临时目录避免写锁
- 使用 PySide6 Signals 进行组件间通信（不直接耦合）

## 构建

```powershell
conda activate PDFasker
pyinstaller PDFasker.spec
```

## 无测试/Lint/Typecheck

项目当前没有测试框架、lint 或 typecheck 配置。所有 Python 文件使用 `from __future__ import annotations` 和类型注解，但未经 mypy 验证。
