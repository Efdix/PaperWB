# PaperWB — AI 论文解读助手

## 运行环境

- Python 环境: conda env `PaperWB`（Python 3.11；本机位于 `D:\science\miniforge3\envs\PaperWB`，构建脚本通过 `conda env list` 自动发现，无需硬编码路径）
- 所有 Python 命令前需激活: `conda activate PaperWB`
- 包管理: pip + `requirements.txt`
- 依赖（requirements.txt）: `PySide6==6.11.1` `openai==2.44.0` `PyMuPDF==1.27.2.3` `docling==2.118.0` `rank_bm25==0.2.2` `hf_transfer==0.1.9` `python-docx==1.2.0` `Pillow==12.3.0`
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
PaperWB.jpg               # 应用图标源图（make_icon.py 自动裁切图形主体）
assets/                    # 应用图标（installer/make_icon.py 生成，exe/安装向导/窗口共用）
installer/                 # 安装向导构建
├── PaperWB.iss            # Inno Setup 脚本: 许可/组件(可选预置模型)/目录/数据目录选择页(写入 config.json 的 data_root)/完成页自检/卸载询问配置清理
├── build_installer.ps1    # 一条龙: 补装 PyInstaller → pyinstaller → 模型 staging → dist selftest 验收门 → ISCC 出包
├── stage_models.py        # Docling 模型 staging: 本机 HF 缓存 → installer/models_cache/hub（约 505 MB）
├── make_icon.py           # 从项目根目录 PaperWB.jpg 生成 assets/PaperWB.ico/png
├── lang/ChineseSimplified.isl  # 向导简体中文语言文件（官方非官方翻译库）
└── Output/                # 产物 PaperWB-Setup-<版本>.exe（gitignore）
src/
├── app.py                 # MainWindow — 首次启动弹窗(FirstLaunchDialog) + 信号枢纽 + 多模态/纯文本 LLMClient + Zotero watcher + 三工作台切换(阅读/检索/写作)；阅读侧「论文问答」双页签(本篇论文/全文献库)；LibraryPreparser 后台建库接线(启动/入队/索引刷新/处理器共享，PAPERWB_DISABLE_PREPARSE 可禁用)
├── core/
│   ├── pdf_parser.py      # PDF 底层工具（PyMuPDF）: 文本提取/渲染
│   ├── pdf_processor.py   # 核心！两阶段管线:
│   │                      #   Stage 1: Docling 本地布局解析 → JSON → 缓存
│   │                      #   Stage 2: 读缓存 → 本地规则组装 → StructuredDocument → UI（跨页接缝合并可调 LLM，失败自动规则兜底；两级接缝缓存 merged_seams 定稿 / merged_seams_prelim 后台建库初步 + seams_final 标记，打开初步整合文献时后台 LLM 精修一次并定稿）
│   ├── docling_parser.py  # Docling 本地解析器: PDF → 与视觉管线兼容的逐页元素（设置 HF 镜像/禁用编译；检测 <安装目录>/models/hub 预置模型 → 重定向 HF_HUB_CACHE+离线模式）
│   ├── llm_client.py      # OpenAI 兼容 API 客户端 + json_mode(response_format) + 7 个提供商预设（DeepSeek/GLM智谱/Mimo/OpenCode Go/Zen/Ollama/自定义；GLM 与 Zen 含免费模型）
│   ├── context_manager.py # Token 预算管理: 长文档走 BM25 检索增强（只发相关段落），短文档用"前70%+后30%"；完整参考文献列表仅引用类问题附带（每问省 3-8k token）
│   ├── retriever.py       # 轻量本地检索器: Retriever 接口 + Bm25Retriever（预留向量升级；index_text 可选评分字段；小语料 IDF 退化时按词面重叠兜底）
│   ├── zotero_parser.py   # Zotero SQLite 解析器: 集合层级/文献(含摘要 abstractNote)/附件 + reload()（临时副本只读，支持指定路径不全局探测）
│   ├── zotero_watcher.py  # Zotero 周期同步: 启动加载后每 30 分钟自动重载（不做文件事件监听）+ 手动刷新
│   ├── library_qa.py      # 全文献库问答 RAG: 元数据轻索引 + PDF 全文索引(条目 key 缓存/mtime 失效/增量刷新；结构化缓存优先抽段——只取正文/摘要并带章节名，回退 PyMuPDF 裸文本；refresh_item 单篇升级 + flush 批量收口) + 混合检索 + LLM 消息组装
│   ├── library_preparser.py # 后台全库预解析: 串行驱动两阶段管线(纯本地零 LLM；prelim 初步整合落盘；篇间小憩+用户解析让路；失败记忆 lib_index/preparse.json；与阅读共享 page_cache/states 缓存)
│   ├── literature_search.py # 统一文献检索核心: PubMedPaper 统一模型(含 cited_by) + PubMed/arXiv/OpenAlex 三源 MultiSourceSearcher(跨源去重) + LLM 检索式生成(v2: 年份/文献类型过滤+同义词扩展) + 两轮闭环反思(reflect_on_results) + 加权排序(rank_papers) + PaperSearchWorker/run_paper_search
│   ├── openalex.py        # OpenAlex 客户端: 关键词检索(原生年份/类型过滤+倒排摘要还原+被引数) + 种子解析(resolve_openalex_works) + 引文图谱推荐(recommend_by_citations: related_works+cites 聚合)
│   ├── library_recommender.py # 按库推荐: Zotero 集合(含子集合)种子构建 + OpenAlex 引文推荐(主路) + LLM 集合画像检索(辅路) + 合并去重过滤 + LibraryRecommendWorker
│   ├── literature_scout.py # 检索工作台·定向巡视: 方向 CRUD(topics.json) + 每方向 QTimer 定时多源检索 + 库内比对滤重(seen.json) + feed.json/RIS/CSV + push_to_feed 外部推送
│   ├── reference_match.py # 文献匹配公共口径: DOI/标题归一化 + 库内查重 + 可选 LLM 批量模糊比对（写作文献补充与巡视共用）
│   ├── unified_writer.py  # 统一润色+引文核查: 证据检索化(按声明检索相关段落) + json_mode + 多层容错 JSON
│   ├── writing_coach.py   # 写作教练: 知识库管理/写作习惯分析/期刊格式分析/引用密度分析
│   ├── writing_prompts.py # 四种写作类型的系统提示词（综述/论文/专利/软著）
│   └── pubmed_searcher.py # PubMed E-utilities 检索客户端（esearch + efetch）；PubMedPaper 统一文献模型(含 source/arxiv_id)
├── ui/
│   ├── pdf_viewer.py      # 结构化阅读面板: ParagraphCard 按 element_type 渲染+中英文翻译(标题/摘要/正文/关键词/图表注可译, 多并发+滚动自动翻译)+文字区 I 形光标；Stage1 完成后自动跨页整合；初步整合文献打开时后台 LLM 接缝精修(_refine_prelim_seams)
│   ├── pdf_list_panel.py  # 左侧面板: Tab1 Zotero 只读文献库 + Tab2 其它文献
│   ├── zotero_panel.py    # Zotero 树形视图: 集合树+文献+PDF附件标记，周期同步/手动刷新驱动刷新
│   ├── chat_panel.py      # 聊天面板: Markdown 气泡/流式渲染（阅读侧栏「本篇论文」页签）
│   ├── library_qa_panel.py # 库内问答面板(阅读侧栏「全文献库」页签): 索引构建/流式回答+[n]角标/只问库/重建索引/参考文献跳转打开 PDF/「后台建库解析」开关与状态行 + item_key_for_pdf/refresh_engine_item/flush_engine 预解析协作
│   ├── workbench_panel.py # 检索工作台(两栏): 左·AI 检索主区(自然语言→多源检索+结果卡片) / 右·巡视面板(上·方向卡片+定时巡视，下·巡视结果/推荐流 卡片+忽略/RIS/CSV，方向与结果同栏相邻)
│   ├── writing_panel.py   # 写作面板: 编辑器优先+可收起工具检查器(知识库/Zotero/批注/AI)+自动保存+字数统计
│   ├── diff_dialog.py     # 润色对比对话框: 内联 diff(单编辑框)+导航栏(上一处/下一处/接受/拒绝)+AI 对话+引用高亮
│   ├── lit_search_dialog.py # 文献补充对话框: LLM 双轨推荐(已知文献+搜索词)→多源检索(带来源标注)→导出 CSV/加入推荐流（非模态）
│   ├── settings_dialog.py # 设置对话框: API接口设置(多模态/纯文本/文献检索源-OpenAlex密钥可选,密钥用于三源检索与按库推荐)+连接测试（Zotero/缓存路径在独立 DirectorySettingDialog，与 API 设置平级菜单）
│   └── styles.py          # 轻量浅灰/白色研究工作台主题 QSS
└── utils/
    ├── config.py          # 持久化层: 配置(含多接口/数据根目录/Zotero)+图书馆+聊天+缓存+草稿+润色历史+lib_index/scout 目录
    ├── layout.py          # 递归布局高度计算（heightForWidth）
    └── threads.py         # 运行中 QThread 全局保活注册表（track/sweep），杜绝运行中销毁崩溃
```

## 架构说明

### 数据存储架构

- 配置文件: `%APPDATA%/PaperWB/config.json`（固定路径）
- 数据根目录: 首次启动弹窗选择，存储在 config 的 `data_root` 字段
- 所有用户数据在 `{data_root}/.paperwb/` 下，包括 library.json、chats、states、page_cache、writing_kb、drafts、polish_history、lib_index（全文献库问答索引）、scout（巡视方向/去重记忆/推荐流）
- PDF 文件存储在 `{data_root}/library/` 下
- 菜单「设置 → 缓存文件存储路径设置...」可随时更改 data_root；Zotero 路径也在同一菜单中与 API 设置平级

### 两阶段解析管线（阅读）

- **Stage 1**: PDF 导入后由 Docling 本地版式解析，每页独立缓存，支持断点续传；不再调用视觉模型，也不再配置并发页数；Docling 漏检的位图区域自动用 PyMuPDF 兜底补成 figure 元素
- **Stage 2**: 点击论文后自动触发（无手动「AI 整合」按钮），**本地规则组装**（不调 LLM）：读页缓存 → 章节/正文/引文/图/表归类绑定（图挂载对应页渲染图+回填图注，引文优先折叠显示）→ StructuredDocument → UI；跨页被截断的段落（章节间切页、表格断页）由后台线程按文本特征合并，合并结果缓存（merged_seams）
- **接缝续写判定**（`_looks_like_continuation`，跨页配对与同页断裂合并共用）：上段以连字符结尾是断词强信号（豁免短段门槛）；英文 b 以小写/括号/引号/数字开头时上段非真句终即接，句尾缩写点（e.g. / et al. / Fig. 等 `_ABBREV_TAIL_RE`）不算句终；大写开头仅当上段无真句终；中文无大小写、上段 >20 字且无句末标点即接；参考文献条目开头拒接；图注占位行（(legend on next page)）与图注面板标签（(A)/(A-C) 开头）拒接。配对池只取上一页末段 × 下一页首段，首页模板区块（Highlights/Authors 等整块收进 `_front_matter_block_ids`，>400 字段落视为已入正文停止收集）、作者单位块、页眉页脚许可行、裸数字引用编号一律过滤。判定规则变更须同步自增 `FAST_DOCUMENT_VERSION`，旧 structured_document 缓存自动失效重建
- **首页 front matter 分类状态机**（`build_document_fast` 内，仅第 1 页，进入正文后关闭）：Docling 常把标题/作者行/单位/摘要/关键词都标成 body 或 subtitle，组装时按版面顺序与文本特征归类为专用卡片（UI 已有对应渲染）：标题（页首首个 20-300 字符、非章节名/期刊名/模板标签的 subtitle；期刊名黑名单 `_JOURNAL_NAME_RE` 覆盖 Nature 系/Cell 系/PNAS/eLife/BMC/Genome Biology 等，超长期刊名如 ``Nature Communications`` 也不会误当标题）→ `title` 卡 + doc.title；作者行（`_is_author_line`：≥2 个「姓名+编号/字母上标」、无机构词、非编号开头、无句终标点，容忍 `David M. Irwin` 缩写点；限前两页）→ `authors` 卡 + doc.authors（doc.authors 已捕获则去重丢弃）；单位块（`_is_affiliation_block`：编号开头+机构词，长度上限 1500 且剥离「these authors contributed / correspondence and requests」声明句后无散文信号词）→ `affiliations` 卡（显示）+ metadata_pool（首张）；单位碎片（`_is_affiliation_fragment`：≤80 字符的编号/机构词短行）→ 跳过；摘要（作者行后 >120 字符长 body 且含散文信号词 `_ABSTRACT_PROSE_RE`，或 `Abstract/Summary` 词头）→ `abstract_body` 绿色卡；关键词行（`Keywords/Key words/关键词` 前缀）→ `keywords` 卡；`Abstract/Summary/Significance` 小节名 → `abstract_heading` 卡（其后的 body 顺延映射为 abstract_body；UI 标题显示原文小节名，不替换成中文）。**同页续写合并闸**：首页作者行/单位块/单位碎片/关键词行/版头噪声（running head / front-matter noise）与其相邻元素不参与同页合并（否则 Frontiers 的「作者+单位+摘要」会粘成 2461 字大卡、JSE 单位块之间互相粘连、通讯行/日期行会混进摘要词头）；short 无标点行（<40 字符）跳过不关闭状态机；短行 subtitle（期刊名/装饰标签 <20 字符且非章节名 `_SECTION_TITLE_RE`）跳过不关闭；非模板小节标题才关闭状态机进入正文流。主循环另有作者行兜底（限前 2 页，处理 Cell 版式 p2 作者行）、重复标题去重（与 doc.title 完全相同的次页排印标题丢弃）、模板小节名剔除（`Authors and Affiliations` 等，防版头小节进正文流与目录）。旧版 LLM 跨页整合缓存不再使用：状态文件标记 `doc_format: fast`，旧 `structured_document` 首次打开时自动重建（图注/图序从页面文本重新绑定，见 `rebuild_document_fast`）
- 右键菜单: 本地库「重新解析整合」（清页缓存全流程重跑）与分开重跑「重新逐页解析」「重新跨页整合」；Zotero 文献只保留「重新解析整合」+「打开文件位置」
- 长文档问答走 BM25 检索增强（`context_manager` + `retriever`），只发相关段落而非全量截断
- 列表已整合文献以浅绿背景标记

### 两套独立 API

- **多模态**: 图表内容问答（图问答把渲染图 base64 附加为多模态消息，见 `MainWindow._attach_vision_message`）；逐页解析与跨页段落整合由本地完成，不耗 LLM
- **纯文本**: 论文问答（本篇长文档 BM25 检索增强 / 全文献库 RAG 问答）、段落翻译、写作引文核查/风格分析/润色/文献推荐，以及 AI 检索与巡视二级比对

### 全文献库问答（阅读工作台「论文问答」侧栏第二页签）

阅读工作台右侧「论文问答」侧栏为双页签：**本篇论文**（ChatPanel，针对当前打开 PDF 的 BM25 检索增强问答）与**全文献库**（`library_qa_panel.py`，面向整个 Zotero 库的跨文献 RAG 问答）。

- 索引：条目元数据（标题/作者/年份/期刊/摘要/DOI）内存 BM25 轻索引；有 PDF 的条目全文抽段**结构化缓存优先**（states 两阶段整合缓存有效 → 只取 body/abstract_body 段并带章节名，参考文献/页眉页脚天然剔除；否则回退 PyMuPDF 裸文本按页抽段），缓存到 `{data_root}/.paperwb/lib_index/fulltext.json`（键 = Zotero 条目 key，失效判据 = PDF mtime，增量刷新、无变化不重写落盘，INDEX_VERSION=3）
- 后台 `IndexBuildWorker` 启动/Zotero 变更后增量构建（全程 track() 保活、可中断），状态显示于问答输入框下方；构建进行中到达的单篇升级（`refresh_item`）转待办队列、构建收尾统一补抽
- 检索：元数据 + 全文混合，元数据命中加权在前（点名某篇优先），每篇携带最佳 2 段（含页码与章节名，章节词参与 BM25 打分但不污染证据展示）；两种模式都附摘要行（300 字）；「只问库」开关跳过全文只做元数据级问答
- 回答：流式渲染，要求 LLM 标注 [n] 角标；参考文献卡片点击 → `open_pdf_requested` → 直接在阅读器按两阶段管线打开该 PDF（已在阅读工作台，无需切换）
- Zotero 只读铁律不变：索引只读打开原 PDF

### 后台全库预解析（library_preparser.py，与阅读统一解析服务）

- **统一缓存**：阅读与建库共用同一套 `PDFProcessor` / `page_cache` / `states` 缓存（按 PDF 路径哈希键 + mtime 失效）——任一方先解析过，另一方直接复用（`doc_state_is_parsed` 判定）
- **纯本地零 LLM**：`LibraryPreparser` 串行逐篇驱动 Stage 1 + Stage 2（`start_stage2(preliminary=True)`：规则接缝合并写入 `merged_seams_prelim`，states 标记 `seams_final=false`），跨启动按缓存完整性自动续跑；失败篇记忆到 `lib_index/preparse.json`（文件未变不重试）
- **阅读时一次精修**：点开初步整合的文献 → 缓存秒开，同时后台把接缝候选送 LLM 复核（接受者入 `merged_seams` 定稿、否决者移出初步缓存），完成后刷新视图、置 `seams_final=true` 并触发该篇索引 `refresh_item`；未配 API 时规则版即为最终（与旧行为一致）；旧缓存缺 `seams_final` 字段视为已定稿
- **调度**：默认开启（config `preparse_enabled`，库问答面板「后台建库解析」开关即时启停）；索引就绪或启动 60 秒后自动开跑；篇间小憩 1.5s；用户侧解析在途时 `should_yield` 让路；用户点开正在预解析的文献直接接管在途处理器（不重复解析）；重跑/删除缓存路径会同步取消对应预解析
- **索引升级节流**：每篇完成 `engine.refresh_item`（内存态置脏），每 10 篇落盘一次（`flush(reindex=False)`），暂停/完成/关窗时 `flush(reindex=True)` 重建检索器原子换入

### 检索工作台（第二工作台：AI 检索 + 按库推荐 + 定向巡视）

职责划分：检索工作台只负责**文献检索与巡视**；单篇与全库问答都在阅读工作台。顶部导航「检索工作台」`_switch_workspace(1)` 切入（顺序：阅读 → 检索 → 写作），两栏布局（左·AI 检索主区 / 右·巡视面板，见 `workbench_panel.py`）。

**AI 检索（主区，三源 + 两轮闭环）**：自然语言 → LLM 生成检索方案（三源 openalex/pubmed/arxiv，带年份/文献类型过滤与同义词扩展）→ 第 1 轮检索 → `reflect_on_results` 缺口反思（enough 提前终止 / off_topic 剔除 / 第 2 轮补充检索式）→ 合并去重 → 年份过滤与加权排序（年份 0.7 + log 被引 0.3）→ 库内过滤。无 LLM 降级为原文单轮。右栏可整体收起（头部「巡视面板」toggle）。

**按库推荐（library_recommender.py）**：AI 检索面板头部「📚 按库推荐」复选框切换进入（与自然语言检索互斥显示，默认不勾选）；推荐范围为**级联下拉**（`_build_collection_tree` + `_rec_combos` 链：全库 → 顶层集合 → 逐级下钻，选任一级即含其全部子级）。以所选范围文献为种子（≤50，DOI 优先）：① OpenAlex 引文推荐（`resolve_openalex_works` 解析种子 → `recommend_by_citations` 聚合 related_works 与引用者，按关联种子数排序，不耗 LLM）；② LLM 集合画像检索（种子标题 → 归纳方向生成检索式 → 三源检索）。两路合并去重，排除种子与库内已有；卡片带「引文推荐/画像检索」与「被引 N / 关联种子 N」标签。

**定向巡视（literature_scout.py）**：
- 方向（Topic）持久化到 `{data_root}/.paperwb/scout/topics.json`：{name, keywords(英文检索式), collection_key(可选限定集合), interval_hours, limit, enabled, use_llm_match}
- 每个启用方向独立 QTimer（不做文件监听），到点 `ScoutWorker` 后台多源检索；启动 20 秒后补跑到期方向；卡片「立即巡视」手动触发
- 滤重两级：本地零成本（规范化 DOI + 标题归一精确匹配，`reference_match.py`，与写作文献补充共用口径）→ 可选 LLM 批量模糊比对（应对 DOI 缺失/标题改写，json_mode）
- 已推送标识（pmid/arxiv_id/doi 统一口径）存 `seen.json` 不重复推送；巡视结果存 `feed.json`（最近 200 条，跨启动保留），展示于右栏下半「巡视结果」
- 结果落地三条路（Zotero 只读不能直写）：导出 RIS（Zotero 可导入）/ 导出 CSV / 复制引文 / 「🔍」生成检索式打开 PubMed 网页

### 统一文献检索核心（literature_search.py）

- **数据模型统一**：`PubMedPaper`（pubmed_searcher.py）统一 `source`（pubmed/arxiv/openalex）、`arxiv_id`、`cited_by`（OpenAlex 被引数）字段，全项目文献记录共用此模型
- **三源检索**：`MultiSourceSearcher` 按检索方案（plan: [{source, query, year_from?, year_to?, doc_type?}]）路由 PubMed（E-utilities；年份/类型翻译为 `[dp]`/`[pt]` 后缀服务端生效）+ arXiv（Atom API；年份客户端兜底过滤）+ OpenAlex（`openalex.py`，原生 filter）；跨源按 DOI/标题归一合并去重（`merge_papers`）
- **检索式生成 v2**：`generate_search_plan` 让 LLM 把自然语言需求拆解为三源检索式（json_mode，每源 ≤4 条，优先 openalex），要求同义词/缩写扩展，可推断年份时必填 year_from/year_to，综述需求给 doc_type；无 LLM/失败自动降级为原文直接检索
- **两轮闭环**：`reflect_on_results`（REFINE_PLAN_PROMPT）把第 1 轮命中标题（≤40 条）回喂 LLM：输出 enough（提前终止）/ off_topic（不切题剔除）/ 第 2 轮补充检索式；`run_paper_search(rounds=2)` 编排，AI 检索与写作文献补充用两轮，巡视 rounds=1 单轮
- **排序**：`rank_papers` 年份归一 0.7 + log(1+被引) 归一 0.3（被引仅 OpenAlex 提供，缺失按 0）；卡片展示「被引 N」
- **统一后台任务**：`PaperSearchWorker(QThread)`（生成式 → 三源检索 → 两轮反思 → 库内过滤，log/results/error/done 信号）与纯函数 `run_paper_search`（供同步调用复用）
- **调用点**：① 定时巡视 ScoutWorker（关键词 PubMed+arXiv 双源单轮，控制定时请求量）；② 写作「文献补充」对话框（两轮，结果带来源标注 + 「加入推荐流」）；③ 检索工作台「AI 检索」主区（两轮）；④ 检索工作台「按库推荐」（library_recommender 两路）
- **推荐流统一归口**：`ScoutManager.push_to_feed(papers, label)` 供外部推送（文献补充/主动检索），按统一标识去重；写作面板 `feed_requested` 信号 → MainWindow → 检索工作台 `add_to_feed`

### 写作系统

- **WritingCoach**: 知识库(CRUD)→风格分析(六维度+引用密度)→AI辅助(润色/核查/文献补充)
- **UnifiedWriter**: 统一润色+引文核查，支持常规模式和仅核查模式(verify_only)；引文证据按声明检索相关段落，不再整篇塞全文
- **DiffDialog**: 内联 diff 展示 + 导航工具栏 + 逐项接受/拒绝 + 可编辑 diff + AI 对话（非模态）
- **LitSearchDialog**: 双轨文献推荐——LLM 已知文献 + 多源检索词（非模态），结果可推入推荐流
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

- 正式分发物 = Inno Setup 安装向导（`installer/PaperWB.iss`）：许可(MIT)/安装位置（默认 `%LOCALAPPDATA%\Programs\PaperWB`，免管理员，PrivilegesRequiredOverridesAllowed 可改全局）/数据与缓存目录选择页（[Code] 写入 `%APPDATA%\PaperWB\config.json` 的 `data_root`，重装预填旧值，首次启动不再弹窗）/组件选择（「预置离线解析模型」默认勾选，主程序必装不显示）/完成页可选「安装自检」（[Code] Exec `--selftest` 按退出码弹窗）
- **预置模型原理**：安装包把两个 HF 模型缓存目录（`models--docling-project--docling-layout-heron` 版式 + `models--docling-project--docling-models` TableFormer，共约 505 MB）装入 `<安装目录>\models\hub\`；`docling_parser.py` 导入时检测两目录齐全 → `HF_HUB_CACHE`/`HF_HUB_OFFLINE=1` 重定向（setdefault，用户显式设置优先）→ 首次解析完全离线；不齐全则回退在线下载（hf-mirror）。**不用** docling `artifacts_path`（会强制要求 RapidOcr 目录，否则扫描版 PDF 解析抛错）；RapidOCR 小模型已随 rapidocr wheel 收进主程序
- 模型 staging（`stage_models.py`）从本机 `~/.cache/huggingface/hub` 复制：copytree 解引用符号链接、剔除 blobs/.lock，保留 refs+snapshots 标准 HF 缓存布局；本机无缓存时先运行应用解析一次预热
- 构建入口 `installer/build_installer.ps1`：PyInstaller(onedir) → staging → **dist selftest 验收门**（任一 FAIL 中止出包）→ ISCC（版本号从 main.py `setApplicationVersion` 抓取）→ `installer/Output/PaperWB-Setup-<版本>.exe`
- 远程排查日志：`%TEMP%\paperwb_selftest.log`（自检，含 bundled-models 项）+ `%APPDATA%\PaperWB\error.log`（excepthook）+ `faulthandler.log` + Inno `%TEMP%\Setup Log*.txt`；SmartScreen 拦截见 README「下载与安装」

## 测试

- 无测试框架/Lint/Typecheck 配置
- 所有 Python 文件使用 `from __future__ import annotations` 和类型注解
- 测试数据在 `test/` 目录下（含示例 PDF、写作草稿、缓存快照）
- 验收脚本: `test/validate_zotero.py`（Zotero 文献两阶段整合验收，`--count` 可调，默认 20，输出 JSON 报告）与 `test/capture_zotero_screenshots.py`（UI 截图验收，`--count` 可调，默认 20，输出 PNG）
- 自测脚本: `test/selftest_bugfixes.py`（纯逻辑回归）与 `test/selftest_workbench.py`（检索工作台与库内问答核心逻辑：匹配口径/索引/RAG 组装/巡视全链路假 PubMed/OpenAlex 解析与三源路由/检索式 v2 与两轮闭环/按库推荐/UI 离屏构建/两级接缝缓存与后台建库预解析（结构化抽取·prelim/final 状态迁移·队列过滤失败记忆·flush 复用·参考文献修剪），无 LLM 无网络）；`test/smoke_workbench_app.py`（offscreen 全窗口集成冒烟，读真实配置与 Zotero 库但不调 LLM；设 PAPERWB_DISABLE_PREPARSE=1 跳过后台预解析）
