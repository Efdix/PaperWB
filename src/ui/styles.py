"""PaperWB 全局视觉主题。

整体采用轻量研究工作台风格：中性浅灰背景、白色内容表面、石墨文字，
用单一蓝色表示主要行动，保留低饱和绿色/红色作为状态反馈。
"""


STYLESHEET = """
/* ============================== 基础 ============================== */
QMainWindow {
    background-color: #f5f5f7;
}

QWidget {
    color: #1d1d1f;
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
    background-color: #f5f5f7;
}

QWidget#libraryPanel,
QWidget#zoteroPanel,
QWidget#readerPanel,
QWidget#chatPanel {
    background-color: #ffffff;
}

QWidget#readerContent,
QWidget#chatMessages {
    background-color: #ffffff;
}

QTextEdit#draftEditor {
    background-color: #ffffff;
    color: #1d1d1f;
    border: 1px solid #d1d1d6;
    border-radius: 10px;
    padding: 18px;
    font-size: 14px;
}

QTextEdit#draftEditor:focus {
    border-color: #3478f6;
}

QFrame#editorCard,
QFrame#writingHeader,
QFrame#controlBar {
    background-color: #ffffff;
    border: 1px solid #e5e5ea;
    border-radius: 14px;
}

QFrame#appHeader {
    background-color: #ffffff;
    border: 1px solid #e5e5ea;
    border-radius: 14px;
}

QLabel#brandMark {
    background-color: #e8f1ff;
    color: #2463c5;
    border-radius: 10px;
    font-size: 20px;
    font-weight: 800;
    padding: 8px 10px;
}

QLabel#brandTitle {
    color: #1d1d1f;
    font-size: 19px;
    font-weight: 800;
}

QLabel#brandSubtitle {
    color: #6e6e73;
    font-size: 11px;
}

QLabel#headerHint {
    color: #6e6e73;
    font-size: 12px;
}

QLabel#statusChip {
    background-color: #f2f2f7;
    color: #6e6e73;
    border: 1px solid #d1d1d6;
    border-radius: 13px;
    padding: 5px 10px;
    font-size: 11px;
}

QLabel#statusChip[status="ready"] {
    background-color: #e9f7ef;
    color: #18794e;
    border-color: #b9e4c9;
}

QLabel#statusChip[status="warning"] {
    background-color: #fff4e5;
    color: #9a5a00;
    border-color: #f2d2a3;
}

/* ============================== 工作区导航 ============================== */
QPushButton#workspaceNav {
    background-color: transparent;
    color: #6e6e73;
    border: 1px solid transparent;
    border-radius: 9px;
    padding: 8px 16px;
    font-size: 13px;
    font-weight: 700;
}

QPushButton#workspaceNav:hover {
    background-color: #f2f2f7;
    color: #1d1d1f;
}

QPushButton#workspaceNav:checked {
    background-color: #e8f1ff;
    color: #2463c5;
    border-color: #c5d9ff;
}

QPushButton#headerAction {
    background-color: #3478f6;
    color: #ffffff;
    border: 1px solid #3478f6;
    border-radius: 9px;
    padding: 8px 13px;
    font-weight: 700;
}

QPushButton#headerAction:hover {
    background-color: #2463c5;
    border-color: #2463c5;
}

/* ============================== 按钮 ============================== */
QPushButton {
    background-color: #f2f2f7;
    color: #1d1d1f;
    border: 1px solid #d1d1d6;
    border-radius: 8px;
    padding: 7px 13px;
    min-height: 18px;
    font-size: 12px;
    font-weight: 650;
}

QPushButton:hover {
    background-color: #e8e8ed;
    border-color: #b8b8bd;
}

QPushButton:pressed {
    background-color: #d1d1d6;
}

QPushButton:disabled {
    background-color: #f5f5f7;
    color: #aeaeb2;
    border-color: #e5e5ea;
}

QPushButton#primaryBtn {
    background-color: #3478f6;
    color: #ffffff;
    border-color: #3478f6;
    font-weight: 750;
}

QPushButton#primaryBtn:hover {
    background-color: #2463c5;
    border-color: #2463c5;
}

QPushButton#primaryBtn:disabled {
    background-color: #b7cdf5;
    color: #f7fbfa;
    border-color: #b7cdf5;
}

QPushButton#softBtn {
    background-color: #fff1f0;
    color: #b42318;
    border-color: #f4c7c3;
}

QPushButton#softBtn:hover {
    background-color: #ffe3e0;
    border-color: #e9aaa4;
}

QPushButton#successBtn {
    background-color: #2b8a5a;
    color: #ffffff;
    border-color: #2b8a5a;
    font-weight: 750;
}

QPushButton#successBtn:hover {
    background-color: #216b45;
}

QPushButton#dangerBtn {
    background-color: #fff1f0;
    color: #b42318;
    border-color: #f4c7c3;
}

QPushButton#dangerBtn:hover {
    background-color: #ffe3e0;
}

QPushButton#iconBtn {
    padding-left: 9px;
    padding-right: 9px;
    min-width: 26px;
}

QPushButton#paneToggle {
    padding: 6px 9px;
    font-size: 11px;
    color: #6e6e73;
}

QPushButton#paneToggle:checked {
    background-color: #e8f1ff;
    color: #2463c5;
    border-color: #c5d9ff;
}

/* ============================== 文本与输入 ============================== */
QLabel {
    color: #3a3a3c;
}

QLabel#titleLabel {
    color: #1d1d1f;
    font-size: 16px;
    font-weight: 800;
}

QLabel#sectionLabel {
    color: #1d1d1f;
    font-size: 13px;
    font-weight: 750;
}

QLabel#subtitleLabel {
    color: #6e6e73;
    font-size: 11px;
}

QLabel#eyebrowLabel {
    color: #3478f6;
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
    color: #1d1d1f;
    border: 1px solid #d1d1d6;
    border-radius: 8px;
    padding: 7px 10px;
    selection-background-color: #cfe0ff;
    selection-color: #1d1d1f;
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
    border-color: #3478f6;
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
    background-color: #ffffff;
    color: #1d1d1f;
    border: 1px solid #d1d1d6;
    selection-background-color: #e8f1ff;
    selection-color: #2463c5;
    outline: none;
    padding: 4px;
}

QComboBox QAbstractItemView::item {
    padding: 7px 10px;
    border-radius: 5px;
}

QComboBox QAbstractItemView::item:hover {
    background-color: #f2f2f7;
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
    background-color: #3478f6;
    border-color: #3478f6;
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
    color: #2463c5;
    border-bottom: 2px solid #3478f6;
}

QTabBar::tab:hover:!selected {
    color: #1d1d1f;
    background-color: #f2f2f7;
}

QSplitter::handle {
    background-color: #e5e5ea;
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
    background-color: #a8c5ff;
}

/* ============================== 列表、树、滚动条 ============================== */
QTreeWidget,
QListWidget {
    background-color: #ffffff;
    color: #1d1d1f;
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
    background-color: #f2f2f7;
}

QTreeWidget::item:selected,
QListWidget::item:selected {
    background-color: #e8f1ff;
    color: #2463c5;
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
    background: #c7c7cc;
    min-height: 34px;
    border-radius: 4px;
}

QScrollBar::handle:vertical:hover {
    background: #9bb9ef;
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
    background: #c7c7cc;
    min-width: 34px;
    border-radius: 4px;
}

QScrollBar::add-line:horizontal,
QScrollBar::sub-line:horizontal {
    width: 0;
}

/* ============================== 分组与状态 ============================== */
QGroupBox {
    background-color: #ffffff;
    color: #1d1d1f;
    border: 1px solid #e5e5ea;
    border-radius: 12px;
    margin-top: 10px;
    padding: 17px 11px 11px 11px;
    font-weight: 750;
}

QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 7px;
    color: #3a3a3c;
    background-color: #ffffff;
}

QProgressBar {
    background-color: #e5e5ea;
    border: none;
    border-radius: 5px;
    text-align: center;
    color: transparent;
    height: 8px;
}

QProgressBar::chunk {
    background-color: #3478f6;
    border-radius: 5px;
}

QStatusBar {
    background-color: #ffffff;
    color: #6e6e73;
    border-top: 1px solid #e5e5ea;
    padding: 4px 12px;
    font-size: 11px;
}

QToolTip {
    background-color: #1d1d1f;
    color: #ffffff;
    border: 1px solid #3a3a3c;
    border-radius: 6px;
    padding: 7px 10px;
}

/* ============================== 菜单与弹窗 ============================== */
QMenuBar {
    background-color: #ffffff;
    color: #3a3a3c;
    border: none;
    padding: 3px 8px;
    font-size: 12px;
}

QMenuBar::item {
    padding: 5px 11px;
    border-radius: 6px;
}

QMenuBar::item:selected {
    background-color: #f2f2f7;
}

QMenu {
    background-color: #ffffff;
    color: #1d1d1f;
    border: 1px solid #d1d1d6;
    border-radius: 8px;
    padding: 5px;
}

QMenu::item {
    padding: 7px 28px 7px 13px;
    border-radius: 5px;
}

QMenu::item:selected {
    background-color: #e8f1ff;
    color: #2463c5;
}

QDialog,
QMessageBox {
    background-color: #f5f5f7;
}

QMessageBox QLabel {
    color: #3a3a3c;
    font-size: 13px;
}

/* ============================== 内容卡片 ============================== */
ParagraphCard,
ImageCard,
ClaimResultCard {
    background-color: #ffffff;
    border: 1px solid #e5e5ea;
    border-radius: 12px;
}

ParagraphCard:hover,
ImageCard:hover,
ClaimResultCard:hover {
    border-color: #a8c5ff;
}

ChatBubble {
    background-color: #ffffff;
    border: 1px solid #e5e5ea;
    border-radius: 12px;
}

ChatBubble[role="user"] {
    background-color: #e8f1ff;
    border-color: #c5d9ff;
}

QFrame#chatInput {
    background-color: #ffffff;
    border-top: 1px solid #e5e5ea;
}

/* ============================== 检索工作台 / 库内问答 ============================== */
QWidget#workbenchPanel {
    background-color: #f5f5f7;
}

QFrame#workspaceHeader {
    background-color: #ffffff;
    border: 1px solid #e5e5ea;
    border-radius: 12px;
}

QFrame#topicPanel,
QFrame#qaPanel,
QWidget#qaPanel,
QFrame#aiSearchPanel,
QFrame#feedPanel {
    background-color: #ffffff;
    border: 1px solid #e5e5ea;
    border-radius: 14px;
}

QFrame#topicCard,
QFrame#scoutCard {
    background-color: #ffffff;
    border: 1px solid #e5e5ea;
    border-radius: 12px;
}

QFrame#topicCard:hover,
QFrame#scoutCard:hover {
    border-color: #a8c5ff;
}

QFrame#refCard {
    background-color: #f5f8ff;
    border: 1px solid #d9e5ff;
    border-radius: 12px;
}

QPushButton#refRow {
    background-color: transparent;
    border: none;
    border-radius: 6px;
    padding: 4px 6px;
    text-align: left;
    color: #23586b;
    font-size: 12px;
    font-weight: 600;
}

QPushButton#refRow:hover {
    background-color: #e8f1ff;
    color: #2463c5;
}

QPushButton#refRow:disabled {
    background-color: transparent;
    color: #9da8a5;
}

QPushButton#topicToggle {
    padding: 4px 10px;
    font-size: 11px;
}

QPushButton#topicToggle:checked {
    background-color: #e8f1ff;
    color: #2463c5;
    border-color: #c5d9ff;
}

QLabel#feedChip {
    background-color: #e8f1ff;
    color: #2463c5;
    border: 1px solid #c5d9ff;
    border-radius: 9px;
    padding: 2px 9px;
    font-size: 10px;
    font-weight: 700;
}

QLabel#feedChip[kind="topic"] {
    background-color: #fff4e5;
    color: #9a5a00;
    border-color: #f2d2a3;
}

QLabel#feedChip[kind="library"] {
    background-color: #e9f7ec;
    color: #1d7a3f;
    border-color: #bfe6c9;
}

QFrame#qaInput {
    background-color: #ffffff;
    border-top: 1px solid #e5e5ea;
}

QFrame#writingSideScroll {
    background-color: transparent;
}

QTabWidget#writingInspectorTabs::pane {
    border: 1px solid #e5e5ea;
    border-radius: 10px;
    background-color: #ffffff;
}

QTabWidget#writingInspectorTabs QTabBar::tab {
    padding: 7px 10px;
    margin-right: 2px;
    font-size: 11px;
}

/* ============================== 统计工作台 ============================== */
QWidget#statsPanel {
    background-color: #f5f5f7;
}

QFrame#statsDataPanel,
QFrame#todayCard,
QFrame#heatmapCard,
QFrame#topPapersCard,
QFrame#planCard {
    background-color: #ffffff;
    border: 1px solid #e5e5ea;
    border-radius: 14px;
}

QFrame#todayCell {
    background-color: #f5f8ff;
    border: 1px solid #d9e5ff;
    border-radius: 10px;
}

QLabel#todayValue {
    color: #1d1d1f;
    font-size: 17px;
    font-weight: 700;
}

QLabel#streakChip {
    background-color: #fff4e5;
    color: #9a5a00;
    border: 1px solid #f2d2a3;
    border-radius: 9px;
    padding: 2px 9px;
    font-size: 11px;
    font-weight: 700;
}

QLabel#topRow {
    background-color: #fafafc;
    border: 1px solid #eef0f3;
    border-radius: 8px;
    padding: 5px 8px;
    font-size: 12px;
    color: #1d1d1f;
}

QCheckBox#planTask {
    font-size: 13px;
    color: #1d1d1f;
    padding: 3px 2px;
}

QCheckBox#planTask:checked {
    color: #9a9aa0;
    text-decoration: line-through;
}

QLabel#sectionLabel[overdue="true"] {
    color: #b42318;
}
"""
