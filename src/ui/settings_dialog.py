"""PaperWB 设置对话框 —— API 接口与本地目录分别配置。"""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QComboBox, QPushButton, QFormLayout, QGroupBox, QMessageBox,
    QTabWidget, QWidget,
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
    """单个 API 配置标签页。"""

    def __init__(self, tab_name: str, description: str, parent=None):
        super().__init__(parent)
        self._tab_name = tab_name
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        desc = QLabel(description)
        desc.setObjectName("subtitleLabel")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        provider_group = QGroupBox("服务提供商")
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
        form.addRow("接口密钥：", self.api_key)
        self.base_url = QLineEdit()
        self.base_url.setPlaceholderText("https://api.deepseek.com")
        form.addRow("服务地址：", self.base_url)
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.setPlaceholderText("选择或输入模型名")
        form.addRow("模型名称：", self.model)
        layout.addWidget(cfg)

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


class DirectorySettingDialog(QDialog):
    """独立的本地目录设置对话框，不混入 API 配置标签页。"""

    _METADATA = {
        "zotero_data_dir": {
            "title": "Zotero 文献库路径设置",
            "label": "Zotero 数据目录：",
            "placeholder": "自动检测或手动选择 Zotero 数据目录...",
            "hint": "阅读与写作共用此只读 Zotero 数据目录；留空时使用自动检测。",
        },
        "data_root": {
            "title": "缓存文件存储路径设置",
            "label": "数据根目录：",
            "placeholder": "选择数据存储根目录（含 library/ 与 .paperwb/）...",
            "hint": "PDF 论文、解析缓存、对话记录、写作草稿等所有数据均存储在此目录下。",
        },
    }

    def __init__(self, config_key: str, parent=None):
        if config_key not in self._METADATA:
            raise ValueError(f"不支持的目录配置项：{config_key}")
        super().__init__(parent)
        self._config_key = config_key
        self._config = load_config()
        meta = self._METADATA[config_key]
        self.setWindowTitle(meta["title"])
        self.setMinimumWidth(680)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        title = QLabel(meta["title"])
        title.setObjectName("titleLabel")
        layout.addWidget(title)

        form = QFormLayout()
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        self._path_edit = QLineEdit(self._config.get(config_key, ""))
        self._path_edit.setPlaceholderText(meta["placeholder"])
        row_layout.addWidget(self._path_edit)
        browse_btn = QPushButton("浏览")
        browse_btn.setObjectName("iconBtn")
        browse_btn.clicked.connect(self._browse)
        row_layout.addWidget(browse_btn)
        form.addRow(meta["label"], row)
        layout.addLayout(form)

        hint = QLabel(meta["hint"])
        hint.setObjectName("subtitleLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch()

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        save = QPushButton("保存设置")
        save.setObjectName("primaryBtn")
        save.clicked.connect(self._save)
        buttons.addWidget(save)
        layout.addLayout(buttons)

    def _browse(self):
        path = QFileDialog.getExistingDirectory(
            self,
            self.windowTitle(),
            self._path_edit.text(),
        )
        if path:
            self._path_edit.setText(path)

    def _save(self):
        path = self._path_edit.text().strip()
        if self._config_key == "data_root" and not path:
            QMessageBox.warning(self, "路径不能为空", "请选择缓存文件存储路径。")
            return
        if path and self._config_key == "data_root":
            from pathlib import Path
            try:
                Path(path).mkdir(parents=True, exist_ok=True)
            except OSError as e:
                QMessageBox.critical(self, "路径不可用", f"无法创建该目录：{e}")
                return
        self._config[self._config_key] = path
        save_config(self._config)
        self.accept()


class SettingsDialog(QDialog):
    """API 接口设置对话框（解析、翻译、写作）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("API 接口设置")
        self.setMinimumSize(760, 560)
        self.setModal(True)
        self._config = load_config()

        self._setup_ui()
        self._load()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        title = QLabel("API 接口设置")
        title.setObjectName("titleLabel")
        layout.addWidget(title)

        self.tabs = QTabWidget()

        self._parse_tab = APIConfigTab(
            "parse",
            "解析接口：负责跨页段落整合和论文问答（含图片解读）。逐页版式解析由本地模型完成。",
        )
        self._translate_tab = APIConfigTab(
            "translate",
            "翻译接口：将英文段落翻译为中文，可使用轻量快速的模型。",
        )
        self._write_tab = APIConfigTab(
            "write",
            "写作接口：负责引文核查、风格分析、润色和文献补充，建议使用推理能力较强的模型。",
        )
        self.tabs.addTab(self._parse_tab, "解析")
        self.tabs.addTab(self._translate_tab, "翻译")
        self.tabs.addTab(self._write_tab, "写作与引用")
        layout.addWidget(self.tabs)

        btn = QHBoxLayout()
        btn.addStretch()
        self._test_btn = QPushButton("测试当前接口")
        self._test_btn.setObjectName("secondaryBtn")
        self._test_btn.clicked.connect(self._test)
        btn.addWidget(self._test_btn)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        btn.addWidget(cancel)
        save = QPushButton("保存设置")
        save.setObjectName("primaryBtn")
        save.clicked.connect(self._save)
        btn.addWidget(save)
        layout.addLayout(btn)

    def _load(self):
        self._parse_tab.load(self._config.get("parse_api", {}))
        self._translate_tab.load(self._config.get("translate_api", {}))
        self._write_tab.load(self._config.get("write_api", {}))

    def _save(self):
        self._config["parse_api"] = self._parse_tab.get()
        self._config["translate_api"] = self._translate_tab.get()
        self._config["write_api"] = self._write_tab.get()
        save_config(self._config)
        QMessageBox.information(
            self, "已保存",
            "API 接口设置已保存。"
        )
        self.accept()

    def _test(self):
        current = self.tabs.currentWidget()
        if not isinstance(current, APIConfigTab):
            return
        cfg = current.get()
        if not cfg.get("api_key"):
            QMessageBox.warning(self, "缺少接口密钥", "请先填写接口密钥。")
            return
        if not cfg.get("base_url"):
            QMessageBox.warning(self, "缺少服务地址", "请先填写服务地址。")
            return
        self._test_btn.setEnabled(False)
        self._test_btn.setText("测试中...")
        self._test_worker = _TestConnectionWorker(cfg, self)
        self._test_worker.finished_signal.connect(self._on_test_done)
        self._test_worker.start()

    def _on_test_done(self, ok: bool, msg: str):
        self._test_btn.setEnabled(True)
        self._test_btn.setText("测试当前接口")
        if ok:
            QMessageBox.information(self, "测试成功", f"接口连接正常！\n回复：{msg[:200]}")
        else:
            QMessageBox.critical(self, "测试失败", f"连接失败：{msg}")
