# PaperWB — AI 论文解读助手

[![Python](https://img.shields.io/badge/Python-3.11-blue.svg)](https://www.python.org/)
[![PySide6](https://img.shields.io/badge/PySide6-6.11-green.svg)](https://pypi.org/project/PySide6/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

基于大语言模型的 **Windows 桌面应用**，通过本地 Docling 版式解析 + 本地规则跨页整合（不耗 LLM），将科研论文 PDF 智能解析为结构化阅读视图，并提供综述/论文/专利/软著的全流程写作辅助，以及面向整个 Zotero 文献库的跨文献问答与定时文献巡视。

---

## 下载与安装（普通用户）

### 安装

1. 下载安装向导 `PaperWB-Setup-x.y.z.exe`（发布页）
2. 双击运行。由于个人分发未购买代码签名证书，Windows SmartScreen 可能弹出「Windows 已保护你的电脑」：
   点击 **「更多信息」→「仍要运行」** 即可（本软件不含任何网络服务端，仅调用你配置的 AI 接口）
3. 跟随向导完成安装：
   - **许可协议**：MIT 开源许可
   - **组件选择**：默认勾选「预置离线解析模型」（约 500 MB，推荐）——安装后解析 PDF **完全离线、无需等待下载**；取消勾选则首次解析时自动联网下载（走国内镜像）
   - **安装目录**：默认当前用户目录（`%LOCALAPPDATA%\Programs\PaperWB`，无需管理员权限），也可在向导中改为「所有用户」
   - **完成页**：可勾选「运行安装自检」验证核心组件与离线模型（约 1 分钟），以及「立即运行 PaperWB」
4. 默认安装路径面向当前用户、无需 UAC；桌面/开始菜单快捷方式自动创建

### 获取免费 API Key（推荐入门路径）

论文翻译、图表解读等阅读功能用**智谱 GLM 免费模型**即可零成本上手：

1. 注册智谱开放平台账号：https://open.bigmodel.cn （新用户送额度，免费模型不额外扣费）
2. 创建 API Key
3. PaperWB 中「设置 → API 接口设置」→ 预设选 **「GLM（智谱）」** → 粘贴 Key → 模型选 `glm-4.7-flash`（免费文本）/ `glm-4.6v-flash`（免费视觉，图表解读）

其它含免费模型的预设：**OpenCode Zen**（`mimo-v2.5-free`、`hy3-free` 等，需 OpenCode 账号）。

### 卸载

控制面板/设置中卸载，或用开始菜单「PaperWB → 卸载」。卸载时可选是否保留配置（API Key、数据库路径等，位于 `%APPDATA%\PaperWB`）——保留则下次安装无需重新配置。

### 便携版（备选）

不便安装时也可下载 zip 绿色版（解压到任意可写目录后运行 `PaperWB.exe`；不含预置模型，首次解析需联网下载）。

### 故障排查（可发给开发者）

| 日志 | 路径 | 内容 |
|------|------|------|
| 安装自检 | `%TEMP%\paperwb_selftest.log` | 组件导入/配置/Zotero/离线模型/Docling 解析逐项结果 |
| 运行错误 | `%APPDATA%\PaperWB\error.log` | 程序异常完整 traceback（含时间戳） |
| 原生崩溃 | `%APPDATA%\PaperWB\faulthandler.log` | C++ 层崩溃的全线程调用栈 |
| 安装器本身 | `%TEMP%\Setup Log*.txt` | Inno Setup 安装日志（向导崩溃/文件占用时） |

手动自检：命令行运行 `PaperWB.exe --selftest [样例.pdf]`，退出码 0 为通过。

---

## 功能

### 论文阅读

| 功能 | 说明 |
|------|------|
| 两阶段解析 | **Stage 1** Docling 本地版式解析（标题/正文/表格/公式/参考文献分区，快/省/离线） → **Stage 2** 本地规则组装：章节/正文/引文/图表归类绑定 + 跨页截断段落自动合并为完整结构化文档 |
| 缓存自动失效 | 解析管线版本变更后旧缓存自动失效；右键菜单可单独重跑 Stage 1 / Stage 2 |
| 结构化阅读视图 | 标题/章节/正文/图表/参考文献按 `element_type` 分类渲染，关键章节暖金色高亮 |
| 图表智能提取 | Docling 提供图表 bbox → 自动裁剪保存，图注从页面文本回填；图表内容可多模态问答 |
| 中英对照翻译 | 标题/摘要/正文/关键词/图表注均支持一键翻译为中文，逐段对照阅读（复用纯文本 API） |
| 论文问答 | 基于结构化全文上下文（正文+元信息+图表描述+参考文献）的流式 AI 对话；长文档自动启用 **BM25 检索增强**（只发相关段落） |
| 断点续传 | Stage 1 每页独立缓存，重新打开无需重新解析；支持右键分开重跑 |

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
| 引文原文全文 | 自动从 Zotero 提取匹配文献的 PDF **相关段落**（BM25 检索证据）发送给 LLM，支持深入验证引文细节 |
| 文献补充 | 双轨制：① LLM 直接推荐已知道的文献（含标题/DOI，可导出 CSV）② 生成 PubMed + arXiv 搜索词检索更多 |
| 引用密度分析 | 风格分析时 LLM 按章节统计范文的引用分布，润色时提示引用密度参考 |
| 小结与过渡 | LLM 根据范文风格自动判断何时添加过渡句或小结段落 |
| 多写作类型 | 支持综述 / 研究型论文 / 专利 / 软件著作权四种写作类型的专用提示词 |
| AI 对话 | 润色结果对话框中可直接向 AI 提问（如某处修改的依据），AI 基于 PDF 全文回答 |
| 非模态窗口 | 润色/文献补充对话框不阻塞主界面，可同时阅读论文或操作其他功能 |

### 通用

| 功能 | 说明 |
|------|------|
| 多 API 预设 | DeepSeek / GLM（智谱）/ Mimo / OpenCode Go / OpenCode Zen / Ollama / 自定义，共 7 个预设、近 50 个模型 |
| 两套独立 API | 多模态接口负责图表问答；纯文本接口负责论文问答、翻译、写作和文献检索，可分别配置 API Key、Base URL、模型 |
| 全文献库问答 | 阅读侧「论文问答」双页签：本篇论文问答之外，可对整个 Zotero 库提问（元数据+全文混合 BM25 检索，答案带 `[n]` 来源角标） |
| Zotero 集成 | 左侧「Zotero」标签页只读镜像集合树，**周期同步**（启动加载 + 每 30 分钟后台重载 + 手动刷新）；引文核查提取 PDF 相关段落 |
| 流式对话 | AI 回复实时逐字显示，Markdown 渲染，上下文自动管理（1M token 窗口） |
| 论文库 | 拖拽导入 PDF，文件夹分类管理，对话和解析状态按文档持久化 |
| 轻量主题 | 浅灰工作区、白色内容面板、石墨文字和蓝色主操作色，全组件自定义 QSS 样式 |
| 任务栏多窗口 | 润色/文献补充对话框各自独立显示在任务栏分组中 |

### 检索工作台（自然语言检索 + 按库推荐 + 定向巡视）

| 功能 | 说明 |
|------|------|
| AI 检索（三源） | 自然语言描述需求，LLM 生成 OpenAlex / PubMed / arXiv 三源检索式（含年份、综述/研究论文过滤与同义词扩展），自动滤除库内已有 |
| 两轮闭环 | 初检后 AI 分析缺口（缺综述？缺方法学？术语偏差？）并剔除不切题命中，必要时自动补充第二轮检索 |
| 按库推荐 | 勾选「📚 按库推荐」切换模式（与自然语言检索互斥）；级联选择 Zotero 集合（任一级即含其全部子级）作推荐源：OpenAlex 引文图谱推荐（不耗 LLM，标注关联种子数）+ AI 集合画像检索，两路合并去重 |
| 定向文献巡视 | 为每个研究方向配置检索式，独立定时器到点自动检索；两级去重（本地 DOI/标题精确匹配 + 可选 LLM 模糊比对），已推送文献跨启动记忆不重复推荐 |
| 结果落地三条路 | Zotero 只读不能直写，推荐结果支持导出 RIS（Zotero 可导入）/ 导出 CSV / 复制引文，或一键生成检索式跳转 PubMed 网页 |
| 巡视结果 | 巡视方向与结果同栏相邻展示；最近 200 条推荐跨启动保留，卡片带被引数标注，可「忽略」后不再出现 |

---

## 环境搭建

### 前置要求

- Windows 10/11
- Miniconda / Anaconda
- Git

### 安装

```bash
git clone https://github.com/Efdix/PaperWB.git
cd PaperWB
conda create -n PaperWB python=3.11 -y
conda activate PaperWB
pip install -r requirements.txt
```

> 国内网络可加镜像源加速安装（本仓库用 Miniforge + 清华 PyPI 镜像验证通过）：
> ```bash
> pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
> ```

依赖：`PySide6==6.11.1` `openai==2.44.0` `PyMuPDF==1.27.2.3` `docling==2.118.0` `rank_bm25==0.2.2` `hf_transfer==0.1.9` `python-docx==1.2.0` `Pillow==12.3.0`

> **安装体积提示**：docling 会自动带入 torch / transformers / docling-ibm-models / rapidocr 等重型依赖，首次 `pip install` 需下载约 2.5–3 GB，请耐心等待。

> **Docling 说明**：用于 Stage 1 的 PDF 版式本地解析（标题/正文/表格/公式/参考文献分区）。
> - 首次使用时自动下载模型（一次性，之后离线运行）：版式识别模型来自 Hugging Face（缓存在 `~/.cache/huggingface`），文字识别模型（RapidOCR）自动从 ModelScope 下载
> - Hugging Face 镜像 (hf-mirror.com) **默认开启、无需设置**；如需回退官方源，设置环境变量 `PAPERWB_HF_MIRROR=0`
> - 无需 Java / Docker / GPU，纯 CPU 即可运行；程序已默认禁用 torch JIT 编译（`DOCLING_INFERENCE_COMPILE_TORCH_MODELS=0`），不要求安装 Visual Studio C++ 编译器或开启开发者模式
> - 启动入口 `main.py` 会自动以 UTF-8 模式运行（避免中文 Windows 编码问题）

### 获取 API Key

支持所有 OpenAI 兼容接口。内置预设：

| 预设 | Base URL | 含免费模型 |
|------|----------|-----------|
| DeepSeek | `https://api.deepseek.com` | 否 |
| GLM（智谱） | `https://open.bigmodel.cn/api/paas/v4` | ✅ `glm-4.7-flash` / `glm-4.5-flash` / `glm-4.6v-flash`（视觉）等 |
| Mimo | `https://api.xiaomimimo.com/v1` | 否 |
| OpenCode Go | `https://opencode.ai/zen/go/v1` | 否 |
| OpenCode Zen | `https://opencode.ai/zen/v1` | ✅ `mimo-v2.5-free` / `hy3-free` / `big-pickle` 等 |
| Ollama | `https://ollama.com/v1`（云端，需 API Key） | `deepseek-v4-flash:0731-cloud` / `gemma4:cloud` / `gpt-oss:120b-cloud` 等（也可手动输入其它云端模型标签） |
| 自定义 | 任意 OpenAI 兼容 URL | — |

- **多模态接口**：负责图表内容问答，会把渲染图作为多模态消息发送给模型；不使用图表解读时可以不配置
- **纯文本接口**：负责论文问答（本篇论文/全文献库）、段落翻译、写作、文献检索与巡视；写作和复杂核查建议使用强推理模型

### 启动

```bash
conda activate PaperWB
python main.py
```

首次启动弹出数据根目录选择窗口。之后在 **菜单 → 设置 → API 接口设置...** 中填入 API Key 即可使用。**Zotero 文献库路径设置**和**缓存文件存储路径设置**在设置菜单中与 API 接口设置平级，分别独立保存。

---

## 构建安装包（Windows）

正式分发物是 **Inno Setup 安装向导**（用户下载单个 exe，向导指引完成安装，预置离线解析模型）。

### 前置（一次性）

```powershell
# 1. conda 环境与依赖（含构建工具 PyInstaller，自动装入该环境）
conda create -n PaperWB python=3.11 -y
conda activate PaperWB
pip install -r requirements.txt

# 2. Inno Setup 6（编译安装向导的构建工具）
winget install -e --id JRSoftware.InnoSetup
```

### 一条龙构建

```powershell
conda activate PaperWB
powershell -ExecutionPolicy Bypass -File installer\build_installer.ps1
```

脚本自动完成六步（均可 `-SkipBuild / -SkipModels / -SkipSelftest` 跳过复用中间产物）：

1. 检查/安装 PyInstaller（conda 环境 pip）
2. 从项目根目录 `PaperWB.jpg` 生成 `assets\PaperWB.ico` 和预览 PNG
3. `pyinstaller --noconfirm --clean PaperWB.spec` → `dist\PaperWB\`（onedir）
4. `installer/stage_models.py`：从本机 HF 缓存提取 Docling 模型（约 505 MB）到 `installer\models_cache\hub\`（缺模型时先运行一次应用解析任意 PDF 预热缓存）
5. **验收门**：`dist\PaperWB\PaperWB.exe --selftest <test\ 下首个 PDF>`，任一项 FAIL 即中止出包
6. ISCC 编译 `installer/PaperWB.iss`（版本号自动从 `main.py` 抓取）

产物：`installer\Output\PaperWB-Setup-<版本>.exe`

### 体积与耗时预期

| 项 | 预期 |
|----|------|
| onedir 产物 `dist\PaperWB\` | 约 3 GB（含 torch/PySide6/Docling） |
| 安装包（LZMA2 压缩） | 约 1.5–2 GB，压缩 20–40 分钟 |
| 安装后占用 | 约 3.5 GB（含预置模型 505 MB） |

### 打包要点

- **onedir 而非 onefile**：onefile 每次启动需解压 30 秒以上且易踩 PyInstaller 大包解压竞态；onedir 秒开、DLL 就近加载更稳
- **离线模型预置原理**：安装包把两个模型缓存目录装入 `<安装目录>\models\hub\`；应用启动时 `src/core/docling_parser.py` 检测到目录齐全即重定向 `HF_HUB_CACHE` 并置 `HF_HUB_OFFLINE=1`——首次解析无需联网。RapidOCR 小模型已随 rapidocr 包打进主程序；未预置模型（绿色版/源码运行）时保持原行为：首次解析经国内镜像自动下载
- **图标**：`installer/make_icon.py` 从项目根目录 `PaperWB.jpg` 自动裁出图形主体，生成 `assets/PaperWB.ico` 和 `assets/PaperWB.png`（exe/向导/窗口共用）；一条龙构建会自动重生成
- **分发前验收**：产物机器上运行 `PaperWB.exe --selftest 样例.pdf`，退出码 0 + `%TEMP%\paperwb_selftest.log` 全 PASS 方可发布
- 目标机器如缺 Visual C++ 运行时请安装对应版本（无签名可加装 [Broadway 许可的 VC 运行库](https://learn.microsoft.com/visual-cpp/downloads/)）

### 便携版（备选）

将 `dist\PaperWB` 整个目录压缩为 zip 绿色版（不含预置模型）：

```powershell
Compress-Archive -Path dist\PaperWB\* -DestinationPath build\PaperWB-portable.zip
```


## 使用指南

### 论文阅读

1. 左侧 **论文库** 拖拽或导入 PDF
2. PDF 导入后自动触发 **Stage 1** 逐页解析（状态栏显示进度）
3. 解析完成后点击论文，自动 **Stage 2** 跨页整合为结构化阅读视图
4. 英文段落卡片（标题/摘要/章节标题/图表注均可）点击 **翻译** 查看中文对照
5. 右侧聊天面板输入问题，`Ctrl+Enter` 发送

右键菜单支持**重跑逐页解析** 和 **重跑跨页整合**。

### 综述写作

1. 切换到 **"写作"** 标签页
2. 连接 Zotero：在「设置 → Zotero 文献库路径设置」中指定 Zotero 数据目录
3. 创建知识库：下拉菜单选择 "+ 新建知识库..."，添加写作范文 PDF 和期刊范文 PDF
4. 点击 **"生成风格指南"**，LLM 分析写作习惯（术语/句式/段落/过渡/引用详略度/引用密度）
5. 风格生成后可点击 **"查看风格指南"** 随时回顾
6. 在编辑器中撰写草稿，选中文字后使用右侧工具检查器：
   - **AI 润色与核查** → 内联 diff 展示修改 + 引文核查结果
   - **仅核查引文** → 不修改文字，仅验证引用准确性
   - **补充参考文献** → LLM 推荐已知文献 + PubMed 检索
7. 工具检查器可以隐藏，正文会自动扩展；阅读工作台的文献列表和论文问答也可以独立收起。

### 全文献库问答与检索工作台

1. **全文献库问答**：阅读工作台右侧「论文问答」切换到 **"全文献库"** 页签，对整个 Zotero 库提问（后台自动构建全文索引），答案中的 `[n]` 角标即来源文献，点击参考文献卡片直接打开该 PDF 阅读；「只问库」开关可跳过全文只做元数据级问答
2. **AI 检索**：切换到 **"检索工作台"**（顶部导航顺序：阅读 → 检索 → 写作），左侧用自然语言描述需求：LLM 生成三源检索式（OpenAlex / PubMed / arXiv）初检 → AI 缺口分析 → 必要时自动补充第二轮
3. **按库推荐**：勾选检索面板右上「📚 按库推荐」切换到推荐模式（取消勾选回到自然语言检索），逐级选择 Zotero 集合——先展示最上级，选中任一级即以该级及其全部子级文献为种子（也可继续下钻缩小范围）→ OpenAlex 引文图谱推荐 + AI 集合画像检索
4. **定向巡视**：右侧巡视面板上半「+ 新方向」填写方向名与英文检索式（可限定某个 Zotero 集合），周期到点自动检索新文献，也可点「立即巡视」手动触发；巡视结果展示在方向正下方
5. 巡视结果中的卡片可导出 RIS/CSV 或复制引文再导入你的 Zotero 库

### 润色对话框操作

- **◀ 上一处 / 下一处 ▶**：在修改块之间跳转
- **✅ 接受**：保留新增，移除删除
- **❌ 拒绝**：保留原文，移除新增
- **手动编辑**：diff 编辑器可直接打字修改
- **💬 AI 对话**：对某处修改有疑问，输入问题直问 AI
- **替换原文**：将最终编辑结果写回写作编辑器
- 对话框可最小化/最大化/在任务栏独立显示

### 界面布局

PaperWB 使用统一的浅灰工作区、白色内容面板和蓝色主操作色。三个工作台都以中央内容为主，次要信息放在可收起的侧栏或检查器中：

- 阅读：左侧文献列表、中央结构化阅读、右侧论文问答（本篇论文/全文献库双页签）可分别隐藏。
- 写作：编辑器优先，知识库、Zotero、批注和 AI 辅助按页签分组。
- 检索：AI 检索与按库推荐为主区，右侧巡视面板（方向+结果同栏）可整体收起。

---

## 数据存储

配置文件位置：`%APPDATA%/PaperWB/config.json`

用户数据存储在首次启动时选择的数据根目录下：

```
{data_root}/
├── library/                        # 导入的 PDF 论文
│   └── *.pdf
└── .paperwb/
    ├── config.json                 # （不存这里，见上方 %APPDATA%）
    ├── library.json                # PDF 图书列表
    ├── chats/                      # 对话历史（按文档 MD5 隔离）
    ├── states/                     # Stage 2 整合结果 + 翻译状态
    ├── page_cache/                 # Stage 1 逐页解析缓存（每页独立 JSON）
    │   └── {pdf_md5}/
    │       ├── _manifest.json      # 页面缓存清单
    │       └── page_001.json       # 单页解析结果
    ├── writing_kb/                 # 写作知识库
    │   └── {profile_name}/
    │       ├── config.json         # 含论文全文 + 风格分析结果
    │       ├── personal_papers/    # 写作范文文本
    │       └── journal_papers/     # 期刊范文文本
    ├── drafts/                     # 编辑器草稿自动保存（每 30 秒）
    ├── polish_history/             # 润色结果历史（最多 20 条）
    ├── lib_index/                  # 全文献库问答的全库全文索引（键=Zotero 条目 key，PDF mtime 失效）
    └── scout/                      # 文献巡视: topics.json（方向）/ seen.json（去重记忆）/ feed.json（推荐流）
```

---

## 项目结构

```
PaperWB/
├── main.py                      # 入口: QApplication + MainWindow（自动 UTF-8 重启 + --selftest 无头自检）
├── requirements.txt             # 依赖清单（PySide6/openai/PyMuPDF/docling 等）
├── PaperWB.spec                 # PyInstaller onedir 打包配置（含图标）
├── LICENSE                      # MIT
├── PaperWB.jpg                  # 应用图标源图（生成时自动裁切图形主体）
├── assets/                      # 应用图标（make_icon.py 生成）
├── installer/                   # 安装向导构建
│   ├── PaperWB.iss              # Inno Setup 向导脚本（组件/快捷方式/完成页自检）
│   ├── build_installer.ps1      # 一条龙构建：PyInstaller → 模型 staging → 自检 → ISCC 出包
│   ├── stage_models.py          # Docling 模型 staging（HF 缓存 → 安装包预置目录）
│   ├── make_icon.py             # 图标生成
│   └── lang/ChineseSimplified.isl  # 向导简体中文语言文件
├── src/
│   ├── app.py                   # MainWindow — 首次启动弹窗 + 信号枢纽 + 多模态/纯文本客户端 + Zotero watcher
│   ├── core/
│   │   ├── pdf_parser.py        # PDF 底层工具: 文本提取/渲染 (PyMuPDF)
│   │   ├── pdf_processor.py     # 两阶段管线: Docling 本地布局 (Stage 1) + 本地规则跨页整合 (Stage 2)
│   │   ├── docling_parser.py    # Docling 本地解析器: PDF → 逐页元素（HF 镜像/禁用 torch 编译）
│   │   ├── llm_client.py        # OpenAI 兼容 API 客户端 + 7 个提供商预设（DeepSeek/GLM/Mimo/OpenCode/Ollama/自定义）
│   │   ├── context_manager.py   # Token 预算管理: 长文档 BM25 检索增强（只发相关段落）
│   │   ├── retriever.py         # 轻量本地检索器: Retriever 接口 + Bm25Retriever
│   │   ├── zotero_parser.py     # Zotero SQLite 解析器: 集合层级/文献/附件 + reload()
│   │   ├── zotero_watcher.py    # Zotero 周期同步: 每 30 分钟后台重载 + 手动刷新
│   │   ├── library_qa.py        # 全文献库问答·库内 RAG: 元数据+全文混合检索 + LLM 消息组装
│   │   ├── literature_search.py # 统一文献检索核心: 三源检索(PubMed/arXiv/OpenAlex) + LLM 检索式生成(年份/类型过滤) + 两轮闭环反思 + 加权排序
│   │   ├── openalex.py          # OpenAlex 客户端: 关键词检索 + 种子解析 + 引文图谱推荐
│   │   ├── library_recommender.py # 按库推荐: Zotero 集合种子 → 引文推荐 + LLM 画像检索
│   │   ├── literature_scout.py  # 检索工作台·定向巡视: 方向 CRUD + 定时 PubMed/arXiv 检索 + 两级滤重
│   │   ├── reference_match.py   # 文献匹配公共口径: DOI/标题归一化 + 库内查重 + 可选 LLM 模糊比对
│   │   ├── unified_writer.py    # 统一润色+引文核查: 证据检索化 + JSON 多层容错
│   │   ├── draft_reviewer.py    # 草稿评审
│   │   ├── writing_coach.py     # 写作教练: 知识库/风格分析/引用密度
│   │   ├── writing_prompts.py   # 四种写作类型系统提示词
│   │   ├── docx_io.py           # Word (.docx) 读写: 段落级读写 + 审阅批注解析 + 修订检测
│   │   ├── doc_diff.py          # QTextEdit 内联 diff 控制器: 渲染/锚点/导航/接受拒绝（润色对话框与写作编辑器共用）
│   │   ├── pubmed_searcher.py   # PubMed E-utilities 检索客户端 + 网络重试
│   │   └── ai_words.py / json_utils.py  # AI 词汇库 / JSON 容错工具
│   ├── ui/
│   │   ├── pdf_viewer.py        # 结构化阅读面板: ParagraphCard 按 element_type 渲染 + 中英翻译（标题/摘要/图表注可译）+ I 形光标
│   │   ├── pdf_list_panel.py    # 左侧面板: Tab1 Zotero 文献库 + Tab2 其它文献
│   │   ├── zotero_panel.py      # Zotero 树形视图: 集合树+文献+PDF 附件，watcher 实时刷新
│   │   ├── workbench_panel.py   # 检索工作台(两栏): 左·AI 检索主区 / 右·巡视面板(方向卡片+巡视结果同栏相邻)
│   │   ├── chat_panel.py        # 聊天面板: Markdown 气泡/流式渲染（阅读侧栏「本篇论文」页签）
│   │   ├── library_qa_panel.py  # 库内问答面板（阅读侧栏「全文献库」页签）: 索引构建+跨文献 RAG 问答
│   │   ├── writing_panel.py     # 写作面板: 编辑器优先+可收起工具检查器+自动保存+字数统计
│   │   ├── diff_dialog.py       # 润色对比对话框: 内联 diff + 导航 + 逐项接受/拒绝 + AI 对话
│   │   ├── review_dialog.py     # 评审对话框
│   │   ├── polish_history_dialog.py  # 润色历史对话框
│   │   ├── lit_search_dialog.py # 文献补充对话框: LLM 推荐 + PubMed/arXiv 检索（非模态）
│   │   ├── settings_dialog.py   # API 接口设置: 多模态/纯文本/文献检索源(OpenAlex 密钥可选) + 连接测试（Zotero/缓存路径为独立菜单项）
│   │   └── styles.py            # 轻量浅灰/白色研究工作台主题 QSS
│   └── utils/
│       ├── config.py            # 持久化层: 配置/图书馆/聊天/缓存/草稿/润色历史 读写
│       ├── layout.py            # 递归布局高度计算
│       └── threads.py           # 运行中 QThread 全局保活注册表
├── test/                        # 测试数据 + 自测/验收脚本
│   ├── validate_zotero.py       #   Zotero 文献两阶段整合验收（--count 可调，输出 JSON 报告）
│   ├── capture_zotero_screenshots.py  #   UI 截图验收（--count 可调，输出 PNG）
│   ├── selftest_bugfixes.py     #   纯逻辑回归自测
│   ├── selftest_workbench.py    #   检索工作台与库内问答核心逻辑自测（假 PubMed，无 LLM 无网络）
│   ├── selftest_docx.py         #   Word 读写自测
│   ├── selftest_docdiff.py      #   内联 diff 自测
│   ├── selftest_workbench_app.py / smoke_workbench_app.py  #   offscreen 工作台集成自测/全窗口冒烟
│   └── debug_parse.py           #   解析调试工具
└── .opencode/                   # opencode 开发辅助配置
```

---

## 常见问题

**支持哪些模型？** 所有 OpenAI 兼容接口。内置 DeepSeek、GLM（智谱）、Mimo、OpenCode Go、OpenCode Zen、Ollama 六类预设 + 自定义，共 7 个预设（GLM 与 OpenCode Zen 含免费模型；Ollama 为云端服务，需 API Key）。普通论文问答无需多模态；仅图表内容解读需选择支持视觉的模型。

**支持多长的论文？** DeepSeek V4 支持 1M token 上下文，几百页论文可以一次性处理。

**Stage 1 解析太慢？** Stage 1 固定为 **Docling 本地解析**（快、免费、离线，无 API 限流），速度取决于 CPU；首次使用前需先自动下载模型（一次性）。右键「重新逐页解析」会同时失效旧 Stage 2 结果，确保新页缓存重新整合。

**Docling 首次使用要下载模型？** 安装向导默认已**预置离线模型**（约 500 MB 装入安装目录 `models\hub`），解析全程离线、无下载。仅绿色版/源码运行才需首次下载（一次性，版式模型走 Hugging Face 国内镜像，RapidOCR 模型已随程序内置）；下载失败时可设置环境变量 `PAPERWB_HF_MIRROR=0` 回退官方源重试。

**Zotero 文献怎么同步到软件？** 左侧面板切换到「Zotero」标签页，启动时自动加载，此后每 30 分钟后台自动同步一次（不监听文件事件），也可点面板「刷新」按钮立即手动同步；点击带 PDF 的文献直接用两阶段管线阅读（只读，不会改动你的 Zotero 库）。

**检索工作台和阅读工作台有什么区别？** 阅读工作台负责读与问：单篇论文的结构化阅读问答，以及「全文献库」页签的跨文献综合问答（答案带 `[n]` 来源角标，点击直接打开对应 PDF 阅读）；检索工作台只负责找文献：自然语言 AI 检索（OpenAlex / PubMed / arXiv 三源两轮）、以 Zotero 集合为种子的按库推荐、以及按方向定时巡视（导出 RIS 可导入 Zotero）。

**引文核查匹配不上文献？** 确保 Zotero 中该文献已附加 PDF 附件。引文年份带字母后缀（如 `2025a`）会被自动去后缀匹配。支持 Author-Year 和 [1] 编号两种引用格式。

**润色后看到大量 "No original full text"？** 说明 Zotero 中对应文献条目缺少 PDF 附件。去 Zotero 为该条目右键 → Find Available PDF 或手动附加 PDF。

## 许可证

MIT License
