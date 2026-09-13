"""PaperWB 设置对话框 —— API 接口与本地目录分别配置。"""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QComboBox, QPushButton, QFormLayout, QGroupBox, QMessageBox,
    QTabWidget, QWidget,
    QFileDialog, QCheckBox, QSpinBox,
)
from PySide6.QtCore import Qt, QThread, Signal as QtSignal

from ..core.llm_client import PROVIDERS, VISION_MODELS
from ..utils.config import (
    load_config, save_config, get_vision_api, get_text_api, get_openalex_api_key,
    get_easyscholar_api_key, get_email_config, save_email_config,
)
from ..utils.threads import track


class _TestConnectionWorker(QThread):
    """后台测试 API 连接，避免阻塞 UI。"""

    finished_signal = QtSignal(bool, str)  # (ok, message)

    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self._cfg = cfg

    def run(self):
        if self.isInterruptionRequested():
            return
        try:
            from ..core.llm_client import LLMClient
            client = LLMClient(self._cfg["api_key"], self._cfg["base_url"], self._cfg["model"])
            reply = client.chat_sync(
                [{"role": "user", "content": "请回复：连接测试成功"}],
                timeout=15, max_tokens=50,
            )
            if self.isInterruptionRequested():
                return
            self.finished_signal.emit(True, reply or "")
        except Exception as e:
            if not self.isInterruptionRequested():
                self.finished_signal.emit(False, str(e))


class _TestOpenAlexWorker(QThread):
    """后台测试 OpenAlex 连通性/密钥有效性（密钥可空）。"""

    finished_signal = QtSignal(bool, str)  # (ok, message)

    def __init__(self, api_key: str, parent=None):
        super().__init__(parent)
        self._key = api_key

    def run(self):
        try:
            from ..core.literature_search import test_openalex_connection
            ok, msg = test_openalex_connection(self._key)
            if not self.isInterruptionRequested():
                self.finished_signal.emit(ok, msg)
        except Exception as e:
            if not self.isInterruptionRequested():
                self.finished_signal.emit(False, str(e))


class _TestEasyScholarWorker(QThread):
    """后台测试 EasyScholar 密钥有效性。"""

    finished_signal = QtSignal(bool, str)  # (ok, message)

    def __init__(self, secret_key: str, parent=None):
        super().__init__(parent)
        self._key = secret_key

    def run(self):
        try:
            from ..core.easyscholar import test_easyscholar_connection
            ok, msg = test_easyscholar_connection(self._key)
            if not self.isInterruptionRequested():
                self.finished_signal.emit(ok, msg)
        except Exception as e:
            if not self.isInterruptionRequested():
                self.finished_signal.emit(False, str(e))


class _TestEmailWorker(QThread):
    """后台发送测试邮件。"""

    finished_signal = QtSignal(bool, str)  # (ok, message)

    def __init__(self, cfg: dict, parent=None):
        super().__init__(parent)
        self._cfg = cfg

    def run(self):
        try:
            from ..core.email_notifier import EmailConfig, send_notification_email
            ok, msg = send_notification_email(
                EmailConfig.from_config(self._cfg),
                "【PaperWB】测试邮件",
                "这是一封来自 PaperWB 的测试邮件。\n\n"
                "如果你收到了它，说明邮件通知已配置成功：\n"
                "· 文献巡视发现新文献时可以自动发邮件提醒\n"
                "· 网页追踪（如机构主页通知栏）有更新时也可以自动发邮件\n",
            )
            if not self.isInterruptionRequested():
                self.finished_signal.emit(ok, msg)
        except Exception as e:
            if not self.isInterruptionRequested():
                self.finished_signal.emit(False, str(e))


class EmailNotifyTab(QWidget):
    """邮件通知配置页签（SMTP 发信，供巡视/网页提醒使用）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        desc = QLabel(
            "配置邮箱后，定向巡视发现新文献、网页追踪发现更新时可以自动发邮件提醒你。"
            "密码栏填写邮箱服务商生成的「授权码」（而非登录密码）：\n"
            "· QQ 邮箱：设置 → 账户 → 开启 SMTP 并生成授权码（服务器 smtp.qq.com，端口 465）\n"
            "· 163 邮箱：设置 → POP3/SMTP → 开启并生成授权码（服务器 smtp.163.com，端口 465）\n"
            "· Gmail：开启两步验证后生成应用专用密码（服务器 smtp.gmail.com，端口 465）"
        )
        desc.setObjectName("subtitleLabel")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        group = QGroupBox("SMTP 发信设置")
        form = QFormLayout(group)
        self._enabled_cb = QCheckBox("启用邮件通知")
        self._enabled_cb.setToolTip("总开关：关闭后巡视与网页追踪都不发邮件")
        form.addRow("", self._enabled_cb)
        self._host = QLineEdit()
        self._host.setPlaceholderText("smtp.qq.com")
        form.addRow("SMTP 服务器：", self._host)
        port_row = QWidget()
        port_lay = QHBoxLayout(port_row)
        port_lay.setContentsMargins(0, 0, 0, 0)
        self._port = QSpinBox()
        self._port.setRange(1, 65535)
        port_lay.addWidget(self._port)
        self._ssl_cb = QCheckBox("SSL（465 勾选；587 取消用 STARTTLS）")
        port_lay.addWidget(self._ssl_cb)
        port_lay.addStretch()
        form.addRow("端口：", port_row)
        self._username = QLineEdit()
        self._username.setPlaceholderText("yourname@qq.com")
        form.addRow("发件邮箱：", self._username)
        self._password = QLineEdit()
        self._password.setEchoMode(QLineEdit.EchoMode.Password)
        self._password.setPlaceholderText("邮箱服务商生成的授权码")
        form.addRow("授权码：", self._password)
        self._recipients = QLineEdit()
        self._recipients.setPlaceholderText("接收提醒的邮箱，多个用英文逗号分隔（可与自己相同）")
        form.addRow("收件邮箱：", self._recipients)
        layout.addWidget(group)

        hint = QLabel(
            "授权码保存在本机配置文件中，仅用于向你的邮箱服务商发信。"
            "收件邮箱可以等你拿到目标邮箱地址后再填。")
        hint.setObjectName("subtitleLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch()

    def load(self, cfg: dict) -> None:
        self._enabled_cb.setChecked(bool(cfg.get("enabled")))
        self._host.setText(str(cfg.get("smtp_host", "") or ""))
        try:
            self._port.setValue(int(cfg.get("smtp_port") or 465))
        except (TypeError, ValueError):
            self._port.setValue(465)
        self._ssl_cb.setChecked(bool(cfg.get("use_ssl", True)))
        self._username.setText(str(cfg.get("username", "") or ""))
        self._password.setText(str(cfg.get("password", "") or ""))
        self._recipients.setText(str(cfg.get("recipients", "") or ""))

    def get(self) -> dict:
        return {
            "enabled": self._enabled_cb.isChecked(),
            "smtp_host": self._host.text().strip(),
            "smtp_port": self._port.value(),
            "use_ssl": self._ssl_cb.isChecked(),
            "username": self._username.text().strip(),
            "password": self._password.text(),
            "sender": "",
            "recipients": self._recipients.text().strip(),
        }


class APIConfigTab(QWidget):
    """单个 API 配置标签页。"""

    def __init__(self, tab_name: str, description: str, mark_vision: bool = False,
                 parent=None):
        super().__init__(parent)
        self._tab_name = tab_name
        self._mark_vision = mark_vision
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
        self.provider_combo.addItems(sorted(PROVIDERS.keys()))
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
            display = f"{m}（视觉）" if self._mark_vision and m.lower() in VISION_MODELS else m
            idx = self.model.findText(display)
            if idx >= 0:
                self.model.setCurrentIndex(idx)
            else:
                self.model.setCurrentText(display)

    def _populate_models(self):
        name = self.provider_combo.currentText()
        info = PROVIDERS.get(name, {})
        self.provider_desc.setText(info.get("description", ""))
        models = sorted(info.get("models", []))
        self.model.clear()
        if models:
            if self._mark_vision:
                self.model.addItems(
                    f"{m}（视觉）" if m.lower() in VISION_MODELS else m
                    for m in models
                )
            else:
                self.model.addItems(models)
            self.model.setCurrentIndex(0)
        else:
            self.model.setCurrentText("")

    def get(self) -> dict:
        model = self.model.currentText().strip()
        if self._mark_vision:
            model = model.removesuffix("（视觉）").strip()
        return {
            "provider": self.provider_combo.currentText(),
            "api_key": self.api_key.text().strip(),
            "base_url": self.base_url.text().strip(),
            "model": model,
        }


class OpenAlexTab(QWidget):
    """OpenAlex 检索源配置页签（密钥完全可选）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        desc = QLabel(
            "OpenAlex 免费学术文献库（约 2.5 亿条，全学科覆盖，含被引次数与开放获取链接）。"
            "已用于检索工作台的 AI 检索与按库推荐。密钥完全可选：不填可直接使用基础免费额度；"
            "免费注册后填写密钥可获更高每日额度——实际额度以「测试」按钮返回的剩余额度为准。"
        )
        desc.setObjectName("subtitleLabel")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        group = QGroupBox("API 密钥（可选）")
        form = QFormLayout(group)
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("留空 = 使用免费额度（无需注册）")
        form.addRow("密钥：", self.api_key)
        layout.addWidget(group)

        hint = QLabel(
            "获取密钥：openalex.org 免费注册账号 → 账户设置中生成 API Key。\n"
            "密钥保存在本机配置文件中，仅随文献检索请求发送给 OpenAlex。"
        )
        hint.setObjectName("subtitleLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch()

    def load(self, api_key: str):
        self.api_key.setText(api_key or "")

    def get(self) -> str:
        return self.api_key.text().strip()


class EasyScholarTab(QWidget):
    """EasyScholar 影响因子配置页签（密钥必填才启用）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        desc = QLabel(
            "EasyScholar 开放 API 用于检索结果卡片显示期刊影响因子（JCR IF 与 5 年 IF）。"
            "密钥必填：不填写则检索工作台不启用影响因子显示（卡片不显示 IF）。"
            "免费用户有每日调用额度，同一期刊多篇文献共享一次查询并缓存到本地，不重复消耗额度。"
        )
        desc.setObjectName("subtitleLabel")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        group = QGroupBox("API 密钥")
        form = QFormLayout(group)
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("easyscholar.cc 注册后获取的 secretKey")
        form.addRow("密钥：", self.api_key)
        layout.addWidget(group)

        hint = QLabel(
            "获取密钥：easyscholar.cc 免费注册账号 → 开放接口（openApi）页面复制 secretKey。\n"
            "密钥保存在本机配置文件中，仅随影响因子查询发送给 EasyScholar。"
        )
        hint.setObjectName("subtitleLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch()

    def load(self, secret_key: str):
        self.api_key.setText(secret_key or "")

    def get(self) -> str:
        return self.api_key.text().strip()


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
    """API 接口设置对话框（多模态、纯文本）。"""

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

        self._vision_tab = APIConfigTab(
            "vision",
            "多模态接口：仅用于图表问答（把图表图片连同问题一起发送给模型）。"
            "建议使用视觉模型（下拉中标注「（视觉）」）；不配置时图表问答自动降级为纯文本提问。",
            mark_vision=True,
        )
        self._text_tab = APIConfigTab(
            "text",
            "纯文本接口：论文问答（本篇/全库）、段落翻译、写作（润色/引文核查/风格分析/文献补充）、"
            "文献检索与巡视、跨页段落整合等全部文本功能共用此接口。",
        )
        self.tabs.addTab(self._vision_tab, "多模态")
        self.tabs.addTab(self._text_tab, "纯文本")
        self._openalex_tab = OpenAlexTab()
        self.tabs.addTab(self._openalex_tab, "文献检索源")
        self._easyscholar_tab = EasyScholarTab()
        self.tabs.addTab(self._easyscholar_tab, "影响因子")
        self._email_tab = EmailNotifyTab()
        self.tabs.addTab(self._email_tab, "邮件通知")
        layout.addWidget(self.tabs)
        self._test_worker: _TestConnectionWorker | None = None
        self._openalex_test_worker: _TestOpenAlexWorker | None = None
        self._easyscholar_test_worker: _TestEasyScholarWorker | None = None
        self._email_test_worker: _TestEmailWorker | None = None

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
        self._vision_tab.load(get_vision_api(self._config))
        self._text_tab.load(get_text_api(self._config))
        self._openalex_tab.load(get_openalex_api_key(self._config))
        self._easyscholar_tab.load(get_easyscholar_api_key(self._config))
        self._email_tab.load(get_email_config(self._config))

    def _save(self):
        self._config["vision_api"] = self._vision_tab.get()
        self._config["text_api"] = self._text_tab.get()
        self._config["openalex_api_key"] = self._openalex_tab.get()
        self._config["easyscholar_api_key"] = self._easyscholar_tab.get()
        save_email_config(self._email_tab.get())
        save_config(self._config)
        QMessageBox.information(
            self, "已保存",
            "API 接口设置已保存。"
        )
        self.accept()

    def _test(self):
        current = self.tabs.currentWidget()
        if isinstance(current, OpenAlexTab):
            self._test_openalex(current)
            return
        if isinstance(current, EasyScholarTab):
            self._test_easyscholar(current)
            return
        if isinstance(current, EmailNotifyTab):
            self._test_email(current)
            return
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
        self._test_worker = _TestConnectionWorker(cfg)
        track(self._test_worker)
        self._test_worker.finished_signal.connect(self._on_test_done)
        self._test_worker.start()

    def _test_openalex(self, tab: OpenAlexTab):
        """文献检索源页签：密钥可空，测试连通性与密钥有效性。"""
        if self._openalex_test_worker is not None and self._openalex_test_worker.isRunning():
            return
        self._test_btn.setEnabled(False)
        self._test_btn.setText("测试中...")
        self._openalex_test_worker = _TestOpenAlexWorker(tab.get())
        track(self._openalex_test_worker)
        self._openalex_test_worker.finished_signal.connect(self._on_openalex_test_done)
        self._openalex_test_worker.start()

    def _test_easyscholar(self, tab: EasyScholarTab):
        """影响因子页签：密钥必填，测试密钥有效性与额度。"""
        if self._easyscholar_test_worker is not None and self._easyscholar_test_worker.isRunning():
            return
        if not tab.get():
            QMessageBox.warning(self, "缺少密钥", "请先填写 EasyScholar 密钥。")
            return
        self._test_btn.setEnabled(False)
        self._test_btn.setText("测试中...")
        self._easyscholar_test_worker = _TestEasyScholarWorker(tab.get())
        track(self._easyscholar_test_worker)
        self._easyscholar_test_worker.finished_signal.connect(self._on_easyscholar_test_done)
        self._easyscholar_test_worker.start()

    def _test_email(self, tab: EmailNotifyTab):
        """邮件通知页签：向收件邮箱发一封测试邮件。"""
        if self._email_test_worker is not None and self._email_test_worker.isRunning():
            return
        cfg = tab.get()
        missing = [name for name, val in (
            ("SMTP 服务器", cfg.get("smtp_host")),
            ("发件邮箱", cfg.get("username")),
            ("授权码", cfg.get("password")),
            ("收件邮箱", cfg.get("recipients")),
        ) if not val]
        if missing:
            QMessageBox.warning(self, "信息不全", "请先填写：" + "、".join(missing))
            return
        self._test_btn.setEnabled(False)
        self._test_btn.setText("发送中...")
        self._email_test_worker = _TestEmailWorker(cfg)
        track(self._email_test_worker)
        self._email_test_worker.finished_signal.connect(self._on_email_test_done)
        self._email_test_worker.start()

    def _on_email_test_done(self, ok: bool, msg: str):
        if self.sender() is not self._email_test_worker:
            return
        self._email_test_worker = None
        self._test_btn.setEnabled(True)
        self._test_btn.setText("测试当前接口")
        if ok:
            QMessageBox.information(self, "测试成功", f"测试邮件已发出：{msg}")
        else:
            QMessageBox.critical(self, "发送失败", msg)

    def _on_easyscholar_test_done(self, ok: bool, msg: str):
        if self.sender() is not self._easyscholar_test_worker:
            return
        self._easyscholar_test_worker = None
        self._test_btn.setEnabled(True)
        self._test_btn.setText("测试当前接口")
        if ok:
            QMessageBox.information(self, "测试成功", msg)
        else:
            QMessageBox.critical(self, "测试失败", msg)

    def _on_openalex_test_done(self, ok: bool, msg: str):
        if self.sender() is not self._openalex_test_worker:
            return
        self._openalex_test_worker = None
        self._test_btn.setEnabled(True)
        self._test_btn.setText("测试当前接口")
        if ok:
            QMessageBox.information(self, "测试成功", msg)
        else:
            QMessageBox.critical(self, "测试失败", msg)

    def _on_test_done(self, ok: bool, msg: str):
        if self.sender() is not self._test_worker:
            return
        self._test_worker = None
        self._test_btn.setEnabled(True)
        self._test_btn.setText("测试当前接口")
        if ok:
            QMessageBox.information(self, "测试成功", f"接口连接正常！\n回复：{msg[:200]}")
        else:
            QMessageBox.critical(self, "测试失败", f"连接失败：{msg}")

    def _stop_test_worker(self) -> bool:
        for attr in ("_test_worker", "_openalex_test_worker", "_email_test_worker"):
            worker = getattr(self, attr, None)
            if worker is not None and worker.isRunning():
                worker.requestInterruption()
                if not worker.wait(3_000):
                    return False
            setattr(self, attr, None)
        return True

    def accept(self) -> None:
        if self._stop_test_worker():
            super().accept()

    def reject(self) -> None:
        if self._stop_test_worker():
            super().reject()

    def closeEvent(self, event) -> None:
        if not self._stop_test_worker():
            event.ignore()
            return
        super().closeEvent(event)
