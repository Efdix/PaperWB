# PaperWB — AI 论文解读助手

## 运行环境

- Python 环境: `D:\science\miniforge3\envs\PaperWB`（conda env，Python 3.11）
- 所有 Python 命令前需激活: `conda activate PaperWB`
- 包管理: pip + `requirements.txt`
- 依赖（requirements.txt）: `PySide6==6.11.1` `openai==2.44.0` `PyMuPDF==1.27.2.3` `docling==2.118.0` `rank_bm25==0.2.2` `hf_transfer==0.1.9`
- 构建：`PyInstaller`（已装入该环境）+ Inno Setup 6（系统级，`winget install -e --id JRSoftware.InnoSetup`）

## 项目入口

```powershell
conda activate PaperWB
python main.py
```

## 项目结构

```
main.py                    # 入口: QApplication + MainWindow（自动 UTF-8 重启；打包版: DLL 路径/解压等待/重库预加载 + --selftest 无头自检）
requirements.txt
PaperWB.spec               # PyInstaller onedir 打包配置（图标 assets/PaperWB.ico）
LICENSE                    # MIT
assets/                    # 应用图标（installer/make_icon.py 生成，exe/安装向导/窗口共用）
installer/                 # 安装向导构建
├── PaperWB.iss            # Inno Setup 脚本: 许可/组件(可选预置模型)/目录/完成页自检/卸载询问配置清理
├── build_installer.ps1    # 一条龙: 补装 PyInstaller → pyinstaller → 模型 staging → dist selftest 验收门 → ISCC 出包
├── stage_models.py        # Docling 模型 staging: 本机 HF 缓存 → installer/models_cache/hub（约 505 MB）
├── make_icon.py           # 生成 assets/PaperWB.ico
├── lang/ChineseSimplified.isl  # 向导简体中文语言文件（官方非官方翻译库）
└── Output/                # 产物 PaperWB-Setup-<版本>.exe（gitignore）
src/
├── app.py                 # MainWindow — 首次启动弹窗(FirstLaunchDialog) + 信号枢纽 + 三套 LLMClient + Zotero watcher + 三工作台切换(阅读/写作/文献)
├── core/
│   ├── pdf_parser.py      # PDF 底层工具（PyMuPDF）: 文本提取/渲染
│   ├── pdf_processor.py   # 核心！两阶段管线:
│   │                      #   Stage 1: Docling 本地布局解析 → JSON → 缓存
│   │                      #   Stage 2: 读缓存 → 本地规则组装 → StructuredDocument → UI（跨页接缝合并可调 LLM，失败自动规则兜底）
│   ├── docling_parser.py  # Docling 本地解析器: PDF → 与视觉管线兼容的逐页元素（设置 HF 镜像/禁用编译；检测 <安装目录>/models/hub 预置模型 → 重定向 HF_HUB_CACHE+离线模式）
│   ├── llm_client.py      # OpenAI 兼容 API 客户端 + json_mode(response_format) + 7 个提供商预设（DeepSeek/GLM智谱/Mimo/OpenCode Go/Zen/Ollama/自定义；GLM 与 Zen 含免费模型）
│   ├── context_manager.py # Token 预算管理: 长文档走 BM25 检索增强（只发相关段落），短文档用"前70%+后30%"
│   ├── retriever.py       # 轻量本地检索器: Retriever 接口 + Bm25Retriever（预留向量升级；小语料 IDF 退化时按词面重叠兜底）
│   ├── zotero_parser.py   # Zotero SQLite 解析器: 集合层级/文献(含摘要 abstractNote)/附件 + reload()（临时副本只读，支持指定路径不全局探测）
│   ├── zotero_watcher.py  # Zotero 周期同步: 启动加载后每 30 分钟自动重载（不做文件事件监听）+ 手动刷新
│   ├── library_qa.py      # 文献工作台·库内 RAG: 元数据轻索引 + PDF 全文索引(条目 key 缓存/mtime 失效/增量刷新) + 混合检索 + LLM 消息组装
│   ├── literature_scout.py # 文献工作台·定向巡视: 方向 CRUD(topics.json) + 每方向 QTimer 定时 PubMed 检索 + 库内比对滤重(seen.json) + feed.json/RIS/CSV
│   ├── reference_match.py # 文献匹配公共口径: DOI/标题归一化 + 库内查重 + 可选 LLM 批量模糊比对（写作文献补充与巡视共用）
│   ├── unified_writer.py  # 统一润色+引文核查: 证据检索化(按声明检索相关段落) + json_mode + 多层容错 JSON
│   ├── writing_coach.py   # 写作教练: 知识库管理/写作习惯分析/期刊格式分析/引用密度分析
│   ├── writing_prompts.py # 四种写作类型的系统提示词（综述/论文/专利/软著）
│   └── pubmed_searcher.py # PubMed E-utilities 检索客户端（esearch + efetch）
├── ui/
│   ├── pdf_viewer.py      # 结构化阅读面板: ParagraphCard 按 element_type 渲染+中英文翻译(标题/摘要/正文/关键词/图表注可译, 多并发+滚动自动翻译)+文字区 I 形光标；Stage1 完成后自动跨页整合
│   ├── pdf_list_panel.py  # 左侧面板: Tab1 Zotero 只读文献库 + Tab2 其它文献
│   ├── zotero_panel.py    # Zotero 树形视图: 集合树+文献+PDF附件标记，周期同步/手动刷新驱动刷新
│   ├── chat_panel.py      # 聊天面板: Markdown 气泡/流式渲染
│   ├── workbench_panel.py # 文献工作台(三栏): 左·检索方向卡片+定时巡视 / 中·库内问答(流式+[n]角标+参考文献跳转阅读) / 右·文献推荐流(卡片+忽略/RIS/CSV)
│   ├── writing_panel.py   # 写作面板: 编辑器+知识库管理+Zotero+AI辅助(润色/仅核查/文献补充)+自动保存+字数统计
│   ├── diff_dialog.py     # 润色对比对话框: 内联 diff(单编辑框)+导航栏(上一处/下一处/接受/拒绝)+AI 对话+引用高亮
│   ├── lit_search_dialog.py # 文献补充对话框: LLM 双轨推荐(已知文献+搜索词)→PubMed 检索→导出 CSV（非模态）
│   ├── settings_dialog.py # 设置对话框: API接口设置(解析/翻译/写作)+连接测试（Zotero/缓存路径在独立 DirectorySettingDialog，与 API 设置平级菜单）
│   └── styles.py          # 暖白研究工作台主题 QSS
└── utils/
    ├── config.py          # 持久化层: 配置(含多接口/数据根目录/Zotero)+图书馆+聊天+缓存+草稿+润色历史+lib_index/scout 目录
    ├── layout.py          # 递归布局高度计算（heightForWidth）
    └── threads.py         # 运行中 QThread 全局保活注册表（track/sweep），杜绝运行中销毁崩溃
```

## 架构说明

### 数据存储架构

- 配置文件: `%APPDATA%/PaperWB/config.json`（固定路径）
- 数据根目录: 首次启动弹窗选择，存储在 config 的 `data_root` 字段
- 所有用户数据在 `{data_root}/.paperwb/` 下，包括 library.json、chats、states、page_cache、writing_kb、drafts、polish_history、lib_index（文献工作台全库索引）、scout（巡视方向/去重记忆/推荐流）
- PDF 文件存储在 `{data_root}/library/` 下
- 菜单「设置 → 缓存文件存储路径设置...」可随时更改 data_root；Zotero 路径也在同一菜单中与 API 设置平级

### 两阶段解析管线（阅读）

- **Stage 1**: PDF 导入后由 Docling 本地版式解析，每页独立缓存，支持断点续传；不再调用视觉模型，也不再配置并发页数；Docling 漏检的位图区域自动用 PyMuPDF 兜底补成 figure 元素
- **Stage 2**: 点击论文后自动触发（无手动「AI 整合」按钮），**本地规则组装**（不调 LLM）：读页缓存 → 章节/正文/引文/图/表归类绑定（图挂载对应页渲染图+回填图注，引文优先折叠显示）→ StructuredDocument → UI；跨页被截断的段落（章节间切页、表格断页）由后台线程按文本特征合并，合并结果缓存（merged_seams）
- 旧版 LLM 跨页整合缓存不再使用：状态文件标记 `doc_format: fast`，旧 `structured_document` 首次打开时自动重建（图注/图序从页面文本重新绑定，见 `rebuild_document_fast`）
- 右键菜单: 本地库「重新解析整合」（清页缓存全流程重跑）与分开重跑「重新逐页解析」「重新跨页整合」；Zotero 文献只保留「重新解析整合」+「打开文件位置」
- 长文档问答走 BM25 检索增强（`context_manager` + `retriever`），只发相关段落而非全量截断
- 列表已整合文献以浅绿背景标记

### 三套独立 API

- **解析** (`parse_api`): 论文问答（长文档 BM25 检索增强）+ 图表内容问答（图问答把渲染图 base64 附加为多模态消息，见 `MainWindow._attach_vision_message`）+ **文献工作台库内问答与巡视二级比对**；逐页解析与跨页段落整合由本地完成，不耗 LLM
- **翻译** (`translate_api`): 段落中英对照翻译（可用便宜/免费模型）
- **写作** (`write_api`): 引文核查 + 风格分析 + 润色 + 文献推荐（需强推理模型）

### 文献工作台（第三工作台：库内跨文献问答 + 定向巡视）

职责划分：文献工作台面向**整个 Zotero 库**（跨文献综合问答、定时文献巡视）；单篇论文的阅读问答仍在阅读工作台。顶部导航「文献工作台」`_switch_workspace(2)` 切入，三栏布局（检索方向 / 库内问答 / 文献推荐流，见 `workbench_panel.py`）。

**库内问答（library_qa.py，RAG）**：
- 索引：条目元数据（标题/作者/年份/期刊/摘要/DOI）内存 BM25 轻索引；有 PDF 的条目由 PyMuPDF 按页抽段合并缓存到 `{data_root}/.paperwb/lib_index/fulltext.json`（键 = Zotero 条目 key，失效判据 = PDF mtime，增量刷新、无变化不重写落盘）
- 后台 `IndexBuildWorker` 启动/Zotero 变更后增量构建（全程 track() 保活、可中断），状态显示于问答输入框下方
- 检索：元数据 + 全文混合，元数据命中加权在前（点名某篇优先），每篇携带最佳 2 段（含页码）；「只问库」开关跳过全文只做元数据级问答
- 回答：流式渲染，要求 LLM 标注 [n] 角标；参考文献卡片点击 → `open_pdf_requested` → 切阅读工作台按两阶段管线打开该 PDF
- Zotero 只读铁律不变：索引只读打开原 PDF

**定向巡视（literature_scout.py）**：
- 方向（Topic）持久化到 `{data_root}/.paperwb/scout/topics.json`：{name, keywords(英文检索式), collection_key(可选限定集合), interval_hours, limit, enabled, use_llm_match}
- 每个启用方向独立 QTimer（不做文件监听），到点 `ScoutWorker` 后台检索 PubMed；启动 20 秒后补跑到期方向；卡片「立即巡视」手动触发
- 滤重两级：本地零成本（规范化 DOI + 标题归一精确匹配，`reference_match.py`，与写作文献补充共用口径）→ 可选 LLM 批量模糊比对（应对 DOI 缺失/标题改写，json_mode）
- 已推送 PMID 存 `seen.json` 不重复推送；推荐流存 `feed.json`（最近 200 条，跨启动保留）
- 结果落地三条路（Zotero 只读不能直写）：导出 RIS（Zotero 可导入）/ 导出 CSV / 复制引文 / 「🔍」生成检索式打开 PubMed 网页

### 写作系统

- **WritingCoach**: 知识库(CRUD)→风格分析(六维度+引用密度)→AI辅助(润色/核查/文献补充)
- **UnifiedWriter**: 统一润色+引文核查，支持常规模式和仅核查模式(verify_only)；引文证据按声明检索相关段落，不再整篇塞全文
- **DiffDialog**: 内联 diff 展示 + 导航工具栏 + 逐项接受/拒绝 + 可编辑 diff + AI 对话（非模态）
- **LitSearchDialog**: 双轨文献推荐——LLM 已知文献 + PubMed 检索词（非模态）
- 支持 4 种写作类型: 综述/研究型论文/专利/软著
- 编辑器自动保存（每30秒）到 `{data_root}/.paperwb/drafts/`
- 润色历史保存（最近20条）到 `{data_root}/.paperwb/polish_history/`

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
- Zotero 数据目录在「设置」菜单中与「API 接口设置」平级配置（「Zotero 文献库路径设置」），阅读与写作共用；「缓存文件存储路径设置」同样独立
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

### 安装包分发与预置离线模型

- 正式分发物 = Inno Setup 安装向导（`installer/PaperWB.iss`）：许可(MIT)/组件选择（「预置离线解析模型」默认勾选）/目录（默认 `%LOCALAPPDATA%\Programs\PaperWB`，免管理员，PrivilegesRequiredOverridesAllowed 可改全局）/完成页可选「安装自检」（[Code] Exec `--selftest` 按退出码弹窗）
- **预置模型原理**：安装包把两个 HF 模型缓存目录（`models--docling-project--docling-layout-heron` 版式 + `models--docling-project--docling-models` TableFormer，共约 505 MB）装入 `<安装目录>\models\hub\`；`docling_parser.py` 导入时检测两目录齐全 → `HF_HUB_CACHE`/`HF_HUB_OFFLINE=1` 重定向（setdefault，用户显式设置优先）→ 首次解析完全离线；不齐全则回退在线下载（hf-mirror）。**不用** docling `artifacts_path`（会强制要求 RapidOcr 目录，否则扫描版 PDF 解析抛错）；RapidOCR 小模型已随 rapidocr wheel 收进主程序
- 模型 staging（`stage_models.py`）从本机 `~/.cache/huggingface/hub` 复制：copytree 解引用符号链接、剔除 blobs/.lock，保留 refs+snapshots 标准 HF 缓存布局；本机无缓存时先运行应用解析一次预热
- 构建入口 `installer/build_installer.ps1`：PyInstaller(onedir) → staging → **dist selftest 验收门**（任一 FAIL 中止出包）→ ISCC（版本号从 main.py `setApplicationVersion` 抓取）→ `installer/Output/PaperWB-Setup-<版本>.exe`
- 远程排查日志：`%TEMP%\paperwb_selftest.log`（自检，含 bundled-models 项）+ `%APPDATA%\PaperWB\error.log`（excepthook）+ `faulthandler.log` + Inno `%TEMP%\Setup Log*.txt`；SmartScreen 拦截见 README「下载与安装」

## 测试

- 无测试框架/Lint/Typecheck 配置
- 所有 Python 文件使用 `from __future__ import annotations` 和类型注解
- 测试数据在 `test/` 目录下（含示例 PDF、写作草稿、缓存快照）
- 验收脚本: `test/validate_zotero.py`（Zotero 文献两阶段整合验收，`--count` 可调，默认 20，输出 JSON 报告）与 `test/capture_zotero_screenshots.py`（UI 截图验收，`--count` 可调，默认 20，输出 PNG）
- 自测脚本: `test/selftest_bugfixes.py`（纯逻辑回归）与 `test/selftest_workbench.py`（文献工作台核心逻辑：匹配口径/索引/RAG 组装/巡视全链路假 PubMed/UI 离屏构建，无 LLM 无网络）；`test/smoke_workbench_app.py`（offscreen 全窗口集成冒烟，读真实配置与 Zotero 库但不调 LLM）
