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

### 综述写作

| 功能 | 说明 |
|------|------|
| 知识库管理 | 创建多个知识库 profile，分别添加写作范文和期刊范文 PDF |
| 风格指南生成 | LLM 分析所有范文，自动提取引用格式/结构模板/句式风格/术语偏好/过渡方式/引用密度 |
| AI 润色与核查 | 选中文字后 AI 同步润色语言 + 核查引文准确性，结果以**内联 diff** 展示（红色删除线删除、绿色新增） |
| 逐项接受/拒绝 | 导航栏「上一处/下一处」跳转修改点，可逐项接受或拒绝每个修改 |
| 仅核查引文 | 不修改原文表述，仅验证引文是否准确反映原文发现 |
| 可编辑 diff | 润色结果可直接手动修改、打字、删除，最终版按「替换原文」写回编辑器 |
| 参考文献高亮 | diff 编辑器中自动标黄所有引用标记（Author-Year / [1] / 中文格式） |
| 引文原文全文 | 自动从 Zotero 提取匹配文献的 PDF 全文发送给 LLM，支持深入验证引文细节 |
| 文献补充 | 双轨制：① LLM 直接推荐已知道的文献（含标题/DOI，可导出 CSV）② 生成 PubMed 搜索词检索更多 |
| 引用密度分析 | 风格分析时 LLM 按章节统计范文的引用分布，润色时提示引用密度参考 |
| 小结与过渡 | LLM 根据范文风格自动判断何时添加过渡句或小结段落 |
| 多写作类型 | 支持综述 / 研究型论文 / 专利 / 软件著作权四种写作类型的专用提示词 |
| AI 对话 | 润色结果对话框中可直接向 AI 提问（如某处修改的依据），AI 基于 PDF 全文回答 |
| 非模态窗口 | 润色/文献补充对话框不阻塞主界面，可同时阅读论文或操作其他功能 |

### 通用

| 功能 | 说明 |
|------|------|
| 多 API 预设 | DeepSeek / Mimo / OpenCode Go / OpenCode Zen / 自定义，共 5 个预设、50+ 模型 |
| 三套独立 API | 阅读-解析 / 阅读-翻译 / 写作 各可独立配置不同的 API Key、Base URL、模型 |
| Zotero 集成 | 自动探测或手动指定 Zotero 数据目录，引文核查时提取 PDF 全文 |
| 流式对话 | AI 回复实时逐字显示，Markdown 渲染，上下文自动管理（1M token 窗口） |
| 论文库 | 拖拽导入 PDF，文件夹分类管理，对话和解析状态按文档持久化 |
| Catppuccin 暗色主题 | 护眼舒适的暗色配色方案，全组件自定义 QSS 样式 |
| 任务栏多窗口 | 润色/文献补充对话框各自独立显示在任务栏分组中 |

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

依赖：`PySide6==6.11.1` `openai==2.44.0` `PyMuPDF==1.27.2.3`

### 获取 API Key

支持所有 OpenAI 兼容接口。内置预设：

| 预设 | Base URL | 含免费模型 |
|------|----------|-----------|
| DeepSeek | `https://api.deepseek.com` | 否 |
| Mimo | `https://api.xiaomimimo.com/v1` | 否 |
| OpenCode Go | `https://opencode.ai/zen/go/v1` | 否 |
| OpenCode Zen | `https://opencode.ai/zen/v1` | ✅ deepseek-v4-flash-free 等 |
| 自定义 | 任意 OpenAI 兼容 URL | — |

- **阅读-解析**：需要视觉多模态能力，建议 `deepseek-v4-pro` 或 `glm-5.2`
- **阅读-翻译**：可用便宜/免费模型
- **写作**：需要强推理能力，建议 `deepseek-v4-pro`

### 启动

```bash
conda activate PDFasker
python main.py
```

首次启动弹出数据根目录选择窗口。之后在 **菜单 → 设置 → API 配置** 中填入 API Key 即可使用。Zotero 路径在 **菜单 → 设置 → API 配置 → 写作标签页** 或 **写作面板工具栏** 中设置（两处联动）。

---

## 打包（Windows）

推荐使用 `PyInstaller` 将程序打包为独立可执行文件。示例流程：

```powershell
conda activate PDFasker
pip install pyinstaller
pyinstaller PDFasker.spec
```

上述命令会根据仓库中的 `PDFasker.spec` 构建并在 `dist/PDFasker/` 生成可执行程序（如 `PDFasker.exe`），构建中间产物会在 `build/` 下生成。

如果需要生成单文件可执行（onefile），可以使用：

```powershell
pyinstaller --noconfirm --onefile --windowed main.py
```

可选：将 `dist` 目录压缩为发行包：

```powershell
Compress-Archive -Path dist\PDFasker\* -DestinationPath build\PDFasker.zip
```

打包前请确保已在打包环境中安装 `requirements.txt` 中列出的依赖；在目标机器上可能还需要安装对应的 Visual C++ 运行时。


## 使用指南

### 论文阅读

1. 左侧 **论文库** 拖拽或导入 PDF
2. PDF 导入后自动触发 **Stage 1** 逐页解析（状态栏显示进度）
3. 解析完成后点击论文，自动 **Stage 2** 跨页整合为结构化阅读视图
4. 英文段落卡片点击 **翻译** 查看中文对照
5. 右侧聊天面板输入问题，`Ctrl+Enter` 发送

右键菜单支持**重跑逐页解析** 和 **重跑跨页整合**。

### 综述写作

1. 切换到 **"写作"** 标签页
2. 连接 Zotero：工具栏或 API 设置中指定 Zotero 数据目录
3. 创建知识库：下拉菜单选择 "+ 新建知识库..."，添加写作范文 PDF 和期刊范文 PDF
4. 点击 **"生成风格指南"**，LLM 分析写作习惯（术语/句式/段落/过渡/引用详略度/引用密度）
5. 风格生成后可点击 **"查看风格指南"** 随时回顾
6. 在编辑器中撰写草稿，选中文字后使用右侧 AI 辅助：
   - **✨ AI 润色与核查** → 内联 diff 展示修改 + 引文核查结果
   - **🔍 仅核查引文** → 不修改文字，仅验证引用准确性
   - **🔍 文献补充** → LLM 推荐已知文献 + PubMed 检索

### 润色对话框操作

- **◀ 上一处 / 下一处 ▶**：在修改块之间跳转
- **✅ 接受**：保留新增，移除删除
- **❌ 拒绝**：保留原文，移除新增
- **手动编辑**：diff 编辑器可直接打字修改
- **💬 AI 对话**：对某处修改有疑问，输入问题直问 AI
- **替换原文**：将最终编辑结果写回写作编辑器
- 对话框可最小化/最大化/在任务栏独立显示

---

## 数据存储

配置文件位置：`%APPDATA%/PDFasker/config.json`

用户数据存储在首次启动时选择的数据根目录下：

```
{data_root}/
├── library/                        # 导入的 PDF 论文
│   └── *.pdf
└── .pdfasker/
    ├── config.json                 # （不存这里，见上方 %APPDATA%）
    ├── library.json                # PDF 图书列表
    ├── chats/                      # 对话历史（按文档 MD5 隔离）
    ├── states/                     # Stage 2 整合结果 + 翻译状态
    ├── page_cache/                 # Stage 1 逐页解析缓存（每页独立 JSON）
    │   └── {pdf_md5}/
    │       ├── _manifest.json      # 页面缓存清单
    │       └── page_001.json       # 单页解析结果
    ├── para_cache/                 # 段落解析缓存
    ├── image_cache/                # PDF 图片提取缓存
    ├── writing_kb/                 # 写作知识库
    │   └── {profile_name}/
    │       ├── config.json         # 含论文全文 + 风格分析结果
    │       ├── personal_papers/    # 写作范文文本
    │       └── journal_papers/     # 期刊范文文本
    ├── drafts/                     # 编辑器草稿自动保存（每 30 秒）
    └── polish_history/             # 润色结果历史（最多 20 条）
```

---

## 项目结构

```
PDFasker/
├── main.py                      # 入口: QApplication + MainWindow
├── requirements.txt             # 3 个核心依赖
├── src/
│   ├── app.py                   # MainWindow — 首次启动弹窗 + 信号总枢纽 + 三套 LLMClient
│   ├── core/
│   │   ├── pdf_parser.py        # PDF 底层工具: 文本提取/渲染/图片提取 (PyMuPDF)
│   │   ├── pdf_processor.py     # 两阶段管线: PageAnalysisWorker / IntegrationWorker / PDFProcessor
│   │   ├── llm_client.py        # OpenAI 兼容 API 客户端 + 5 个提供商预设
│   │   ├── context_manager.py   # Token 预算管理: 1M窗口截断策略
│   │   ├── zotero_parser.py     # Zotero SQLite 解析器: 自动检测/搜索/主题排序
│   │   ├── unified_writer.py    # 统一润色+引文核查: prompt 模板 + JSON 多层容错解析
│   │   ├── review_checker.py    # 引文核查引擎: 提取→匹配→对照→评估
│   │   ├── writing_coach.py     # 写作教练: 知识库/风格分析/润色/改写/遗漏文献检测
│   │   ├── writing_prompts.py   # 四种写作类型提示词
│   │   └── pubmed_searcher.py   # PubMed E-utilities 检索客户端
│   ├── ui/
│   │   ├── pdf_viewer.py        # 结构化阅读面板: ParagraphCard 按 element_type 渲染
│   │   ├── pdf_list_panel.py    # 论文库侧边栏: 拖拽导入/文件夹分类/右键菜单
│   │   ├── chat_panel.py        # 聊天面板: Markdown 气泡/流式渲染
│   │   ├── writing_panel.py     # 写作面板: 编辑器+Zotero+AI辅助+风格指南弹窗
│   │   ├── diff_dialog.py       # 润色对比对话框: 内联 diff + 导航栏 + 逐项接受/拒绝 + AI 对话
│   │   ├── lit_search_dialog.py # 文献补充对话框: LLM 推荐 + PubMed 检索（非模态）
│   │   ├── settings_dialog.py   # API 设置: 三标签页+处理设置+Zotero路径
│   │   └── styles.py            # Catppuccin 暗色主题 QSS
│   └── utils/
│       ├── config.py            # 持久化层: 配置/图书馆/聊天/缓存/草稿/润色历史 读写
│       └── layout.py            # 递归布局高度计算
├── test/                        # 测试数据
└── .opencode/                   # opencode 开发辅助配置
```

---

## 常见问题

**支持哪些模型？** 所有 OpenAI 兼容接口。内置 DeepSeek、Mimo、OpenCode Go、OpenCode Zen 四种预设。阅读-解析需视觉多模态能力。

**支持多长的论文？** DeepSeek V4 支持 1M token 上下文，几百页论文可以一次性处理。

**Stage 1 解析太慢？** 设置中可切换为"异步（并发处理）"模式，调整并发页数（推荐 2-4）。注意并发过高可能触发 API 限流。

**引文核查匹配不上文献？** 确保 Zotero 中该文献已附加 PDF 附件。引文年份带字母后缀（如 `2025a`）会被自动去后缀匹配。支持 Author-Year 和 [1] 编号两种引用格式。

**润色后看到大量 "No original full text"？** 说明 Zotero 中对应文献条目缺少 PDF 附件。去 Zotero 为该条目右键 → Find Available PDF 或手动附加 PDF。

## 许可证

MIT License
