"""API 配置对话框 —— 三套 API（嵌入处理设置和 Zotero 路径）。"""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QComboBox, QPushButton, QFormLayout, QGroupBox, QMessageBox,
    QTabWidget, QWidget, QSpinBox, QRadioButton, QButtonGroup,
    QFileDialog,
)
from PySide6.QtCore import Qt, QThread, Signal as QtSignal

from ..core.llm_client import PROVIDERS
from ..utils.config import load_config, save_config


class _TestConnectionWorker(QThread):
    """后台测试 API 连接，避免阻塞 UI。"""

    finished_signal = QtSignal(bool, str)  # (ok, message)

    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self._cfg = cfg

    def run(self):
        try:
            from ..core.llm_client import LLMClient
            client = LLMClient(self._cfg["api_key"], self._cfg["base_url"], self._cfg["model"])
            reply = client.chat_sync(
                [{"role": "user", "content": "请回复：连接测试成功"}],
                timeout=15, max_tokens=50,
            )
            self.finished_signal.emit(True, reply or "")
        except Exception as e:
            self.finished_signal.emit(False, str(e))


class APIConfigTab(QWidget):
    """单个 API 配置标签页，可选附加底部控件。"""

    def __init__(self, tab_name: str, description: str,
                 footer_widget: QWidget | None = None, parent=None):
        super().__init__(parent)
        self._tab_name = tab_name
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        desc = QLabel(description)
        desc.setObjectName("subtitleLabel")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        provider_group = QGroupBox("提供商")
        pg = QVBoxLayout(provider_group)
        self.provider_combo = QComboBox()
        self.provider_combo.setEditable(True)
        self.provider_combo.addItems(list(PROVIDERS.keys()))
        self.provider_combo.currentTextChanged.connect(self._on_provider)
        pg.addWidget(self.provider_combo)
        self.provider_desc = QLabel()
        self.provider_desc.setObjectName("subtitleLabel")
        self.provider_desc.setWordWrap(True)
        pg.addWidget(self.provider_desc)
        layout.addWidget(provider_group)

        cfg = QGroupBox("连接参数")
        form = QFormLayout(cfg)
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("sk-...")
        form.addRow("API Key:", self.api_key)
        self.base_url = QLineEdit()
        self.base_url.setPlaceholderText("https://api.deepseek.com")
        form.addRow("Base URL:", self.base_url)
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.setPlaceholderText("选择或输入模型名")
        form.addRow("模型:", self.model)
        layout.addWidget(cfg)

        if footer_widget:
            layout.addWidget(footer_widget)

        layout.addStretch()

    def _on_provider(self, name: str):
        info = PROVIDERS.get(name, {})
        self.provider_desc.setText(info.get("description", ""))
        self.base_url.setText(info.get("base_url", ""))
        self._populate_models()

    def load(self, api_cfg: dict):
        p = api_cfg.get("provider", "DeepSeek")
        idx = self.provider_combo.findText(p)
        if idx >= 0:
            self.provider_combo.setCurrentIndex(idx)
        else:
            self.provider_combo.setCurrentIndex(0)
        self.api_key.setText(api_cfg.get("api_key", ""))
        saved_url = api_cfg.get("base_url", "")
        if saved_url:
            self.base_url.setText(saved_url)
        self._populate_models()
        m = api_cfg.get("model", "")
        if m:
            idx = self.model.findText(m)
            if idx >= 0:
                self.model.setCurrentIndex(idx)
            else:
                self.model.setCurrentText(m)

    def _populate_models(self):
        name = self.provider_combo.currentText()
        info = PROVIDERS.get(name, {})
        self.provider_desc.setText(info.get("description", ""))
        models = info.get("models", [])
        self.model.clear()
        if models:
            self.model.addItems(models)
            self.model.setCurrentIndex(0)
        else:
            self.model.setCurrentText("")

    def get(self) -> dict:
        return {
            "provider": self.provider_combo.currentText(),
            "api_key": self.api_key.text().strip(),
            "base_url": self.base_url.text().strip(),
            "model": self.model.currentText().strip(),
        }


class ProcessingSettingsGroup(QGroupBox):
    """Stage 1 处理设置（嵌入阅读-解析标签页底部）。"""

    def __init__(self, parent=None):
        super().__init__("⚙️ PDF 处理设置", parent)
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        self._mode_group = QButtonGroup(self)
        mode_row = QHBoxLayout()
        self._sync_radio = QRadioButton("🔤 同步（逐页顺序）")
        self._sync_radio.setToolTip(
            "一页一页顺序处理。稳定、不触发限流。"
        )
        self._async_radio = QRadioButton("⚡ 异步（并发处理）")
        self._async_radio.setToolTip(
            "多页同时发送。速度更快但可能触发限流。"
        )
        self._mode_group.addButton(self._sync_radio, 0)
        self._mode_group.addButton(self._async_radio, 1)
        mode_row.addWidget(self._sync_radio)
        mode_row.addWidget(self._async_radio)
        mode_row.addStretch()
        layout.addLayout(mode_row)

        # 并发设置（仅异步可见）
        self._concurrency_widget = QWidget()
        conc_layout = QHBoxLayout(self._concurrency_widget)
        conc_layout.setContentsMargins(0, 0, 0, 0)
        conc_label = QLabel("并发页数：")
        conc_label.setStyleSheet("color: #a9b1d6; font-size: 13px;")
        conc_layout.addWidget(conc_label)
        self._concurrency_spin = QSpinBox()
        self._concurrency_spin.setRange(1, 10)
        self._concurrency_spin.setValue(3)
        self._concurrency_spin.setToolTip("同时发送的页数，推荐 2-4")
        self._concurrency_spin.setStyleSheet(
            "QSpinBox { background-color: #24253a; color: #e2e5f2; "
            "border: 1px solid #7aa2f7; border-radius: 4px; "
            "padding: 3px 6px; font-size: 14px; font-weight: bold; }"
            "QSpinBox:focus { border-color: #89b4fa; }"
        )
        conc_layout.addWidget(self._concurrency_spin)
        conc_layout.addStretch()
        layout.addWidget(self._concurrency_widget)

        # 选同步时隐藏并发设置
        self._sync_radio.toggled.connect(
            lambda checked: self._concurrency_widget.setVisible(not checked)
        )

        # 解析引擎选择
        parser_label = QLabel("逐页解析引擎：")
        parser_label.setStyleSheet("color: #a9b1d6; font-size: 13px;")
        layout.addWidget(parser_label)
        self._parser_group = QButtonGroup(self)
        parser_row = QHBoxLayout()
        self._vision_radio = QRadioButton("👁️ 视觉 LLM")
        self._vision_radio.setToolTip("整页渲染图片交给多模态 LLM 解析（准确、贵、慢）")
        self._docling_radio = QRadioButton("⚡ Docling 本地")
        self._docling_radio.setToolTip(
            "本地版式解析（快、免费、离线；首次使用需下载模型）\n"
            "图表描述仍需视觉 LLM，但整体成本大幅下降"
        )
        self._parser_group.addButton(self._vision_radio, 0)
        self._parser_group.addButton(self._docling_radio, 1)
        parser_row.addWidget(self._vision_radio)
        parser_row.addWidget(self._docling_radio)
        parser_row.addStretch()
        layout.addLayout(parser_row)

    def load(self, config: dict):
        mode = config.get("stage1_mode", "async")
        if mode == "sync":
            self._sync_radio.setChecked(True)
        else:
            self._async_radio.setChecked(True)
        concurrency = config.get("stage1_concurrency", 3)
        self._concurrency_spin.setValue(max(1, min(10, concurrency)))
        parser = config.get("stage1_parser", "vision")
        if parser == "docling":
            self._docling_radio.setChecked(True)
        else:
            self._vision_radio.setChecked(True)

    def get(self) -> dict:
        return {
            "stage1_mode": "sync" if self._sync_radio.isChecked() else "async",
            "stage1_concurrency": self._concurrency_spin.value(),
            "stage1_parser": "docling" if self._docling_radio.isChecked() else "vision",
        }


class ZoteroPathGroup(QGroupBox):
    """Zotero 文献库路径设置（嵌入写作标签页底部）。"""

    def __init__(self, parent=None):
        super().__init__("📚 Zotero 文献库", parent)
        layout = QHBoxLayout(self)
        layout.setSpacing(6)

        self._path_edit = QLineEdit()
        self._path_edit.setPlaceholderText("自动检测或手动选择 Zotero 数据目录...")
        self._path_edit.setStyleSheet(
            "QLineEdit { background-color: #24253a; color: #cfd2e3; "
            "border: 1px solid #3b3d54; border-radius: 8px; padding: 4px 8px; }"
        )
        layout.addWidget(self._path_edit)

        browse_btn = QPushButton("📂")
        browse_btn.setFixedWidth(48)
        browse_btn.clicked.connect(self._browse)
        layout.addWidget(browse_btn)

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, "选择 Zotero 数据目录")
        if path:
            self._path_edit.setText(path)

    def load(self, config: dict):
        self._path_edit.setText(config.get("zotero_data_dir", ""))

    def get(self) -> str:
        return self._path_edit.text().strip()


class SettingsDialog(QDialog):
    """API 配置对话框（三标签页：阅读-解析 + 阅读-翻译 + 写作）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("API 配置")
        self.setMinimumSize(680, 560)
        self.setModal(True)
        self._config = load_config()

        self._processing_group = ProcessingSettingsGroup()
        self._zotero_group = ZoteroPathGroup()

        self._setup_ui()
        self._load()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        title = QLabel("API 配置")
        title.setObjectName("titleLabel")
        layout.addWidget(title)

        self.tabs = QTabWidget()

        self._parse_tab = APIConfigTab(
            "parse",
            "📖 阅读-解析 API — PDF 逐页视觉解析、跨页整合、论文问答。\n需使用支持视觉的多模态模型。",
            footer_widget=self._processing_group,
        )
        self._translate_tab = APIConfigTab(
            "translate",
            "📖 阅读-翻译 API — 段落中英对照翻译。可用便宜快速的模型。",
        )
        self._write_tab = APIConfigTab(
            "write",
            "📝 写作 API — 综述引文核查、综述写作辅助。建议使用强推理模型。",
            footer_widget=self._zotero_group,
        )
        self.tabs.addTab(self._parse_tab, "📖 阅读-解析")
        self.tabs.addTab(self._translate_tab, "📖 阅读-翻译")
        self.tabs.addTab(self._write_tab, "📝 写作")
        layout.addWidget(self.tabs)

        btn = QHBoxLayout()
        btn.addStretch()
        self._test_btn = QPushButton("测试当前标签页连接")
        self._test_btn.clicked.connect(self._test)
        btn.addWidget(self._test_btn)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        btn.addWidget(cancel)
        save = QPushButton("保存全部")
        save.setObjectName("primaryBtn")
        save.clicked.connect(self._save)
        btn.addWidget(save)
        layout.addLayout(btn)

    def _load(self):
        self._parse_tab.load(self._config.get("parse_api", {}))
        self._translate_tab.load(self._config.get("translate_api", {}))
        self._write_tab.load(self._config.get("write_api", {}))
        self._processing_group.load(self._config)
        self._zotero_group.load(self._config)

    def _save(self):
        self._config["parse_api"] = self._parse_tab.get()
        self._config["translate_api"] = self._translate_tab.get()
        self._config["write_api"] = self._write_tab.get()
        self._config.update(self._processing_group.get())
        self._config["zotero_data_dir"] = self._zotero_group.get()
        save_config(self._config)
        mode = self._config.get("stage1_mode", "async")
        conc = self._config.get("stage1_concurrency", 3)
        QMessageBox.information(
            self, "已保存",
            f"API 配置已保存。\n处理模式：{'同步（逐页顺序）' if mode == 'sync' else f'异步（{conc}页并发）'}"
        )
        self.accept()

    def _test(self):
        current = self.tabs.currentWidget()
        if not isinstance(current, APIConfigTab):
            return
        cfg = current.get()
        if not cfg.get("api_key"):
            QMessageBox.warning(self, "缺少 API Key", "请先填写 API Key。")
            return
        if not cfg.get("base_url"):
            QMessageBox.warning(self, "缺少 Base URL", "请先填写 Base URL。")
            return
        self._test_btn.setEnabled(False)
        self._test_btn.setText("测试中...")
        self._test_worker = _TestConnectionWorker(cfg, self)
        self._test_worker.finished_signal.connect(self._on_test_done)
        self._test_worker.start()

    def _on_test_done(self, ok: bool, msg: str):
        self._test_btn.setEnabled(True)
        self._test_btn.setText("测试当前标签页连接")
        if ok:
            QMessageBox.information(self, "测试成功", f"API 连接正常！\n回复：{msg[:200]}")
        else:
            QMessageBox.critical(self, "测试失败", f"连接失败：{msg}")
