# PDFasker — AI 论文解读助手

[![Python](https://img.shields.io/badge/Python-3.11-blue.svg)](https://www.python.org/)
[![PySide6](https://img.shields.io/badge/PySide6-6.11-green.svg)](https://pypi.org/project/PySide6/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

基于视觉大语言模型的 **Windows 桌面应用**，将科研论文 PDF 通过两阶段视觉 LLM 管线智能解析为结构化阅读视图，并提供综述/论文/专利/软著的全流程写作辅助。

---

## 功能

### 论文阅读

| 功能 | 说明 |
|------|------|
| 两阶段视觉解析 | **Stage 1** 逐页(图片+文本)发给多模态 LLM 识别结构 → **Stage 2** LLM 跨页整合为完整结构化文档 |
| 结构化阅读视图 | 标题/章节/正文/图表/参考文献按 `element_type` 分类渲染，关键章节暖金色高亮 |
| 图表智能提取 | LLM 标注图表 bbox → 自动裁剪保存，附带 AI 生成的图表描述 |
| 中英对照翻译 | 英文段落一键翻译为中文，逐段对照阅读（独立翻译 API） |
| 论文问答 | 基于结构化全文上下文（正文+元信息+图表描述+参考文献）的流式 AI 对话 |
| 断点续传 | Stage 1 每页独立缓存，重新打开无需重新解析；支持右键分开重跑 |
| 并发控制 | Stage 1 支持同步/异步两种模式，可配置并发页数避免 API 限流 |

### 综述写作（与阅读并列的核心模块）

| 功能 | 说明 |
|------|------|
| 知识库管理 | 创建多个知识库 profile，分别添加个人论文和期刊范文 PDF |
| 风格指南生成 | LLM 分析所有参考论文，自动提取引用格式/结构模板/句式风格/术语偏好 |
| 智能润色 | 选中文字后 AI 润色，自动遵循当前知识库的风格指南和写作类型 |
| 引文核查 | 四步骤引擎：提取声明 → Zotero 匹配(三策略级联) → 对照原文逐条分析 → 整体评估 |
| 基于引文改写 | 根据 Zotero 中匹配文献的原文内容，AI 辅助改写综述中的引用表述 |
| 文献推荐 | LLM 分析草稿遗漏方向 → Semantic Scholar 推荐 API + 搜索 API → 结果可导出 CSV |
| 多写作类型 | 支持综述 / 研究型论文 / 专利 / 软件著作权四种写作类型的专用提示词 |

### 通用

| 功能 | 说明 |
|------|------|
| 三套独立 API | 阅读-解析 / 阅读-翻译 / 写作 各可独立配置不同的 API Key、Base URL、模型 |
| 流式对话 | AI 回复实时逐字显示，Markdown 渲染，上下文自动管理（1M token 窗口） |
| 论文库 | 拖拽导入 PDF，文件夹分类管理，对话和解析状态按文档持久化 |
| Catppuccin 暗色主题 | 护眼舒适的暗色配色方案，全组件自定义 QSS 样式 |

---

## 环境搭建

### 前置要求

- Windows 10/11
- Miniconda / Anaconda
- Git

### 安装

```bash
git clone https://github.com/Efdix/PDFasker.git
cd PDFasker
conda create -n PDFasker python=3.11 -y
conda activate PDFasker
pip install -r requirements.txt
```

### 获取 API Key

推荐使用 **DeepSeek V4**：访问 [platform.deepseek.com](https://platform.deepseek.com/)，获取 Key。

- **阅读-解析**：需要视觉多模态能力，建议 `deepseek-v4-pro`
- **阅读-翻译**：可用便宜模型如 `deepseek-v4-flash`
- **写作**：需要强推理能力，建议 `deepseek-v4-pro`

### 启动

```bash
conda activate PDFasker
python main.py
```

首次启动后在 **设置 → API 配置** 中填入三套 API Key 即可使用。

---

## 使用指南

### 论文阅读

1. 左侧 **论文库** 拖拽或导入 PDF
2. PDF 导入后自动触发 **Stage 1** 逐页解析（状态栏显示进度）
3. 解析完成后点击论文，自动 **Stage 2** 跨页整合为结构化阅读视图
4. 英文段落卡片点击 **翻译** 查看中文对照
5. 右侧聊天面板输入问题，`Ctrl+Enter` 发送

### 右键操作

- **重跑逐页解析**：清除 Stage 1 缓存，重新逐页发给 LLM
- **重跑跨页整合**：仅清除 Stage 2 结果，用现有缓存重新整合
- **清除全部缓存**：删除所有缓存，从头开始

### 综述写作

1. 切换到 **"写作"** 标签页
2. 连接 Zotero：设置 → API 配置 → 写作标签页底部 → 设置 Zotero 数据目录路径（或自动检测）
3. 创建知识库：下拉菜单选择 "+ 新建知识库..."，命名后添加参考论文 PDF 和期刊范文 PDF
4. 生成风格指南：点击"生成风格指南"，LLM 分析所有论文的写作习惯
5. 在编辑器中编写综述，使用右侧 AI 辅助功能：
   - **润色选中文字**：遵循风格指南 + 写作类型
   - **基于引文改写**：根据 Zotero 文献原文辅助改写
   - **核查引文准确性**：逐条对比原文，标记"引用恰当 / 建议补充 / 需核实 / 文献未匹配"
   - **文献推荐**：检测草稿遗漏方向，调用 Semantic Scholar 推荐 → 可导出 CSV

---

## 数据存储

所有数据存储在图书馆目录（默认 `Documents/PDFasker_Library/`）的 `.pdfasker/` 文件夹中：

```
PDFasker_Library/
├── .pdfasker/
│   ├── config.json          # API 配置
│   ├── library.json          # PDF 论文列表
│   ├── chats/                # 对话历史（按文档 MD5 隔离）
│   ├── states/               # Stage 2 整合结果 + 翻译状态
│   ├── page_cache/           # Stage 1 逐页解析缓存（每页独立 JSON）
│   │   └── {pdf_md5}/
│   │       ├── _manifest.json  # 页面缓存清单
│   │       └── page_001.json   # 单页解析结果
│   ├── image_cache/          # PDF 图片提取缓存
│   └── writing_kb/           # 写作知识库
│       └── {profile_name}/
│           ├── config.json
│           ├── personal_papers/
│           └── journal_papers/
└── *.pdf                     # 导入的论文文件
```

---

## 项目结构

```
PDFasker/
├── main.py                    # 入口: QApplication + MainWindow
├── requirements.txt           # 4 个依赖: PySide6/openai/PyMuPDF/python-dotenv
├── PDFasker.spec              # PyInstaller 打包配置
├── src/
│   ├── app.py                 # MainWindow — 信号连接枢纽，三套 LLMClient
│   ├── core/
│   │   ├── pdf_parser.py      # PDF 底层工具: 文本提取/渲染/图片提取
│   │   ├── pdf_processor.py   # 两阶段管线: PageAnalysisWorker/IntegrationWorker/PDFProcessor
│   │   ├── llm_client.py      # OpenAI 兼容 API 客户端: 流式/同步/图片消息
│   │   ├── context_manager.py # Token 预算管理: 1M窗口截断策略
│   │   ├── zotero_parser.py   # Zotero SQLite 解析器: 自动检测/搜索/主题排序
│   │   ├── review_checker.py  # 引文核查引擎: 提取→匹配→对照→评估
│   │   ├── writing_coach.py   # 写作教练: 知识库/风格指南/润色/改写/遗漏文献检测
│   │   └── writing_prompts.py # 四种写作类型提示词
│   ├── ui/
│   │   ├── pdf_viewer.py      # 结构化阅读面板: ParagraphCard 按 element_type 渲染
│   │   ├── pdf_list_panel.py  # 论文库侧边栏: 拖拽导入/文件夹分类/右键菜单
│   │   ├── chat_panel.py      # 聊天面板: Markdown 气泡/流式渲染
│   │   ├── writing_panel.py   # 写作面板: 编辑器+Zotero+AI辅助(润色/改写/核查/推荐)
│   │   ├── settings_dialog.py # API 设置: 三标签页+处理设置+Zotero路径
│   │   └── styles.py          # Catppuccin 暗色主题 QSS
│   └── utils/
│       ├── config.py          # 持久化层: 配置/图书馆/聊天/缓存 读写
│       └── layout.py          # 递归布局高度计算
├── .opencode/                 # opencode 开发辅助配置
│   └── commands/              # 自定义命令
└── opencode.jsonc             # opencode 项目配置
```

---

## 打包

```bash
conda activate PDFasker
pyinstaller PDFasker.spec
```

生成的 `dist/PDFasker.exe` 可在未安装 Python 的 Windows 环境运行（约 88 MB）。

---

## 常见问题

**支持哪些模型？** 所有 OpenAI 兼容接口：DeepSeek V4、Mimo、通义千问、智谱 GLM 等。三套 API 可独立配置不同模型。阅读-解析需视觉多模态能力。

**支持多长的论文？** DeepSeek V4 支持 1M token 上下文，几百页论文可以一次性处理。ContextManager 会按"前70%+后30%"策略截断超长论文。

**Stage 1 解析太慢？** 设置中可切换为"异步（并发处理）"模式，调整并发页数（推荐 2-4）。注意并发过高可能触发 API 限流。

**引文核查匹配不上文献？** 确保 Zotero 中该文献已附加 PDF 附件。支持三种匹配策略级联：作者+年份精确匹配 → 标题关键词模糊搜索 → 引文标记回退搜索。

## 许可证

MIT License
