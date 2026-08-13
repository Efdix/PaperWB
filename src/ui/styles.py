"""PaperWB 全局视觉主题。

整体采用“研究工作台”风格：暖白纸张背景、深青色导航、墨色正文，
用青绿色表示主要行动，用珊瑚色提示需要注意的状态。
"""


STYLESHEET = """
/* ============================== 基础 ============================== */
QMainWindow {
    background-color: #f4f1eb;
}

QWidget {
    color: #22343a;
    font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
    font-size: 13px;
}

QWidget#appShell,
QWidget#workspaceSurface,
QWidget#writingSurface,
QWidget#libraryPanel,
QWidget#zoteroPanel,
QWidget#readerPanel,
QWidget#chatPanel,
QWidget#writingSidePanel {
    background-color: #f4f1eb;
}

QWidget#libraryPanel,
QWidget#zoteroPanel,
QWidget#readerPanel,
QWidget#chatPanel {
    background-color: #fffdfa;
}

QWidget#readerContent,
QWidget#chatMessages {
    background-color: #fffdfa;
}

QTextEdit#draftEditor {
    background-color: #fffdfa;
    color: #29434a;
    border: 1px solid #dfe6e2;
    border-radius: 10px;
    padding: 18px;
    font-size: 14px;
}

QTextEdit#draftEditor:focus {
    border-color: #54aaa0;
}

QFrame#panelCard,
QFrame#editorCard,
QFrame#writingHeader,
QFrame#controlBar {
    background-color: #fffdfa;
    border: 1px solid #e4e0d8;
    border-radius: 14px;
}

QFrame#appHeader {
    background-color: #173d45;
    border: 1px solid #173d45;
    border-radius: 18px;
}

QLabel#brandMark {
    background-color: #f08b70;
    color: #173d45;
    border-radius: 14px;
    font-size: 20px;
    font-weight: 800;
    padding: 8px 10px;
}

QLabel#brandTitle {
    color: #fffdfa;
    font-size: 19px;
    font-weight: 800;
}

QLabel#brandSubtitle {
    color: #b7d1d0;
    font-size: 11px;
}

QLabel#headerHint {
    color: #c9dedd;
    font-size: 12px;
}

QLabel#statusChip {
    background-color: #25535a;
    color: #d9efeb;
    border: 1px solid #3c6b70;
    border-radius: 13px;
    padding: 5px 10px;
    font-size: 11px;
}

QLabel#statusChip[status="ready"] {
    background-color: #dff2ec;
    color: #176b61;
    border-color: #b9ded4;
}

QLabel#statusChip[status="warning"] {
    background-color: #fff0dc;
    color: #996222;
    border-color: #f0d1a5;
}

/* ============================== 工作区导航 ============================== */
QPushButton#workspaceNav {
    background-color: transparent;
    color: #b7d1d0;
    border: 1px solid transparent;
    border-radius: 9px;
    padding: 8px 16px;
    font-size: 13px;
    font-weight: 700;
}

QPushButton#workspaceNav:hover {
    background-color: #25535a;
    color: #fffdfa;
}

QPushButton#workspaceNav:checked {
    background-color: #e6f3ef;
    color: #176b61;
    border-color: #b9ded4;
}

QPushButton#headerAction {
    background-color: #25535a;
    color: #f5fbfa;
    border: 1px solid #477277;
    border-radius: 9px;
    padding: 8px 13px;
    font-weight: 700;
}

QPushButton#headerAction:hover {
    background-color: #32656a;
    border-color: #72a5a1;
}

/* ============================== 按钮 ============================== */
QPushButton {
    background-color: #e9efed;
    color: #29434a;
    border: 1px solid #d4e0dc;
    border-radius: 8px;
    padding: 7px 13px;
    min-height: 18px;
    font-size: 12px;
    font-weight: 650;
}

QPushButton:hover {
    background-color: #dcebe7;
    border-color: #a9ccc4;
}

QPushButton:pressed {
    background-color: #c9e0da;
}

QPushButton:disabled {
    background-color: #f0efec;
    color: #a6afad;
    border-color: #e7e3dc;
}

QPushButton#primaryBtn {
    background-color: #147c7c;
    color: #ffffff;
    border-color: #147c7c;
    font-weight: 750;
}

QPushButton#primaryBtn:hover {
    background-color: #0e696a;
    border-color: #0e696a;
}

QPushButton#primaryBtn:disabled {
    background-color: #b8d1cd;
    color: #f7fbfa;
    border-color: #b8d1cd;
}

QPushButton#softBtn {
    background-color: #f6e9df;
    color: #a1533f;
    border-color: #f0d0c1;
}

QPushButton#softBtn:hover {
    background-color: #f6ddd1;
    border-color: #e9b8a7;
}

QPushButton#successBtn {
    background-color: #198676;
    color: #ffffff;
    border-color: #198676;
    font-weight: 750;
}

QPushButton#successBtn:hover {
    background-color: #117262;
}

QPushButton#dangerBtn {
    background-color: #f8e4e0;
    color: #b24f4a;
    border-color: #efc5bf;
}

QPushButton#dangerBtn:hover {
    background-color: #f5d5d0;
}

QPushButton#iconBtn {
    padding-left: 9px;
    padding-right: 9px;
    min-width: 26px;
}

/* ============================== 文本与输入 ============================== */
QLabel {
    color: #344b51;
}

QLabel#titleLabel {
    color: #1e3b42;
    font-size: 16px;
    font-weight: 800;
}

QLabel#sectionLabel {
    color: #1e3b42;
    font-size: 13px;
    font-weight: 750;
}

QLabel#subtitleLabel {
    color: #78888a;
    font-size: 11px;
}

QLabel#eyebrowLabel {
    color: #147c7c;
    font-size: 10px;
    font-weight: 800;
    letter-spacing: 1px;
}

QLineEdit,
QTextEdit,
QPlainTextEdit,
QSpinBox,
QComboBox {
    background-color: #ffffff;
    color: #233940;
    border: 1px solid #d9e1de;
    border-radius: 8px;
    padding: 7px 10px;
    selection-background-color: #bfe4dc;
    selection-color: #173d45;
}

QTextEdit,
QPlainTextEdit {
    line-height: 1.7;
}

QLineEdit:focus,
QTextEdit:focus,
QPlainTextEdit:focus,
QSpinBox:focus,
QComboBox:focus {
    background-color: #fffefa;
    border-color: #54aaa0;
}

QLineEdit:disabled,
QTextEdit:disabled,
QPlainTextEdit:disabled,
QComboBox:disabled {
    background-color: #f1f1ee;
    color: #9da8a5;
}

QComboBox {
    padding-right: 28px;
}

QComboBox:hover {
    border-color: #9bcac2;
}

QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 24px;
    border: none;
    background: transparent;
}

QComboBox::down-arrow {
    image: none;
    width: 0;
    height: 0;
    border-left: 5px solid transparent;
    border-right: 5px solid transparent;
    border-top: 6px solid #6d8180;
}

QComboBox QAbstractItemView {
    background-color: #fffdfa;
    color: #29434a;
    border: 1px solid #d9e1de;
    selection-background-color: #dff2ec;
    selection-color: #176b61;
    outline: none;
    padding: 4px;
}

QComboBox QAbstractItemView::item {
    padding: 7px 10px;
    border-radius: 5px;
}

QComboBox QAbstractItemView::item:hover {
    background-color: #eef6f3;
}

QCheckBox,
QRadioButton {
    color: #4c6264;
    spacing: 7px;
}

QCheckBox::indicator,
QRadioButton::indicator {
    width: 16px;
    height: 16px;
    background-color: #ffffff;
    border: 1px solid #c5d3cf;
}

QCheckBox::indicator {
    border-radius: 4px;
}

QRadioButton::indicator {
    border-radius: 8px;
}

QCheckBox::indicator:checked,
QRadioButton::indicator:checked {
    background-color: #147c7c;
    border-color: #147c7c;
}

/* ============================== 标签页与分割器 ============================== */
QTabWidget::pane {
    border: none;
    background: transparent;
}

QTabBar::tab {
    background: transparent;
    color: #78888a;
    border: none;
    padding: 8px 16px;
    margin-right: 4px;
    font-weight: 700;
}

QTabBar::tab:selected {
    color: #147c7c;
    border-bottom: 2px solid #147c7c;
}

QTabBar::tab:hover:!selected {
    color: #29434a;
    background-color: #eef3f0;
}

QSplitter::handle {
    background-color: #dfe8e4;
}

QSplitter::handle:horizontal {
    width: 5px;
    margin: 8px 0;
    border-radius: 2px;
}

QSplitter::handle:vertical {
    height: 5px;
    margin: 0 8px;
    border-radius: 2px;
}

QSplitter::handle:hover {
    background-color: #66aea5;
}

/* ============================== 列表、树、滚动条 ============================== */
QTreeWidget,
QListWidget {
    background-color: #fffdfa;
    color: #29434a;
    border: none;
    outline: none;
}

QTreeWidget::item,
QListWidget::item {
    padding: 7px 9px;
    min-height: 22px;
    border-radius: 7px;
}

QTreeWidget::item:hover,
QListWidget::item:hover {
    background-color: #eef6f3;
}

QTreeWidget::item:selected,
QListWidget::item:selected {
    background-color: #dff2ec;
    color: #176b61;
}

QScrollArea {
    background-color: transparent;
    border: none;
}

QScrollBar:vertical {
    background: transparent;
    width: 9px;
    margin: 3px 0;
}

QScrollBar::handle:vertical {
    background: #c8d7d3;
    min-height: 34px;
    border-radius: 4px;
}

QScrollBar::handle:vertical:hover {
    background: #8bbeb6;
}

QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical {
    height: 0;
}

QScrollBar:horizontal {
    background: transparent;
    height: 9px;
}

QScrollBar::handle:horizontal {
    background: #c8d7d3;
    min-width: 34px;
    border-radius: 4px;
}

QScrollBar::add-line:horizontal,
QScrollBar::sub-line:horizontal {
    width: 0;
}

/* ============================== 分组与状态 ============================== */
QGroupBox {
    background-color: #fffdfa;
    color: #1e3b42;
    border: 1px solid #e2e4df;
    border-radius: 12px;
    margin-top: 10px;
    padding: 17px 11px 11px 11px;
    font-weight: 750;
}

QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 7px;
    color: #2a5358;
    background-color: #fffdfa;
}

QProgressBar {
    background-color: #e7eeeb;
    border: none;
    border-radius: 5px;
    text-align: center;
    color: transparent;
    height: 8px;
}

QProgressBar::chunk {
    background-color: #147c7c;
    border-radius: 5px;
}

QStatusBar {
    background-color: #fffdfa;
    color: #718180;
    border-top: 1px solid #e3e5df;
    padding: 4px 12px;
    font-size: 11px;
}

QToolTip {
    background-color: #173d45;
    color: #f5fbfa;
    border: 1px solid #3e6b70;
    border-radius: 6px;
    padding: 7px 10px;
}

/* ============================== 菜单与弹窗 ============================== */
QMenuBar {
    background-color: #173d45;
    color: #dcebea;
    border: none;
    padding: 3px 8px;
    font-size: 12px;
}

QMenuBar::item {
    padding: 5px 11px;
    border-radius: 6px;
}

QMenuBar::item:selected {
    background-color: #25535a;
}

QMenu {
    background-color: #fffdfa;
    color: #29434a;
    border: 1px solid #d9e1de;
    border-radius: 8px;
    padding: 5px;
}

QMenu::item {
    padding: 7px 28px 7px 13px;
    border-radius: 5px;
}

QMenu::item:selected {
    background-color: #dff2ec;
    color: #176b61;
}

QDialog,
QMessageBox {
    background-color: #f4f1eb;
}

QMessageBox QLabel {
    color: #344b51;
    font-size: 13px;
}

/* ============================== 内容卡片 ============================== */
ParagraphCard,
ImageCard,
ClaimResultCard {
    background-color: #fffdfa;
    border: 1px solid #e5e1d9;
    border-radius: 12px;
}

ParagraphCard:hover,
ImageCard:hover,
ClaimResultCard:hover {
    border-color: #9bcac2;
}

ChatBubble {
    background-color: #fffdfa;
    border: 1px solid #e5e1d9;
    border-radius: 12px;
}

ChatBubble[role="user"] {
    background-color: #e7f3ef;
    border-color: #c6e3dc;
}

QFrame#chatInput {
    background-color: #fffdfa;
    border-top: 1px solid #e4e0d8;
}
"""
