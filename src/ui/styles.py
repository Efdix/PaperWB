"""PaperWB 全局视觉主题。

设计原则（v3 重构）：**内容优先**。
- 顶部只有一条 44px 应用栏，四个工作台不再各带一条标题横幅；
- 每个工作台最多一条 34px 动作行，其余空间全部留给正文/编辑器/结果；
- 面板左右不再留 16px 外白边，内容贴边，横向也多出 32px；
- 字色/边框/圆角统一为一套低对比中性灰 + 单一蓝色主行动。
"""

from string import Template

from .tokens import QSS_TOKENS

_RAW_STYLESHEET = """
/* ============================== 基础 ============================== */
QMainWindow {
    background-color: $bg;
}

QWidget {
    color: $text;
    font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
    font-size: 13px;
}

QWidget#appShell,
QWidget#workspaceSurface,
QWidget#writingSurface,
QWidget#workbenchPanel,
QWidget#statsPanel {
    background-color: $bg;
}

QWidget#libraryPanel,
QWidget#zoteroPanel,
QWidget#readerPanel,
QWidget#chatPanel,
QWidget#readerContent,
QWidget#chatMessages {
    background-color: $surface;
}

/* ============================== 顶部应用栏 ============================== */
QFrame#appBar {
    background-color: $surface;
    border: none;
    border-bottom: 1px solid $border;
}

QLabel#brandMark {
    background-color: $primary_soft;
    color: $primary;
    border-radius: 8px;
    font-size: 15px;
    font-weight: 800;
    padding: 4px 6px;
}

QLabel#brandTitle {
    color: $text;
    font-size: 15px;
    font-weight: 800;
    letter-spacing: 0.2px;
}

QLabel#headerHint {
    color: $text_muted;
    font-size: 11px;
}

QFrame#appBarDivider {
    background-color: $border;
    max-width: 1px;
    border: none;
}

/* 工作台切换：整段式控件（segmented control），比四个独立按钮更省位、更易扫读 */
QFrame#navSegmented {
    background-color: #f1f2f5;
    border: 1px solid #e6e8ec;
    border-radius: 10px;
}

QPushButton#workspaceNav {
    background-color: transparent;
    color: $text_secondary;
    border: none;
    border-radius: 7px;
    padding: 5px 14px;
    margin: 2px;
    font-size: 12px;
    font-weight: 700;
}

QPushButton#workspaceNav:hover {
    background-color: #e7e9ee;
    color: $text;
}

QPushButton#workspaceNav:checked {
    background-color: $surface;
    color: $primary;
    border: 1px solid #d9e2f7;
}

QPushButton#headerAction {
    background-color: $primary;
    color: $surface;
    border: 1px solid $primary;
    border-radius: 8px;
    padding: 6px 13px;
    font-weight: 700;
}

QPushButton#headerAction:hover {
    background-color: $primary_hover;
    border-color: $primary_hover;
}

QPushButton#appMenuBtn {
    background-color: transparent;
    color: $text_secondary;
    border: 1px solid transparent;
    border-radius: 8px;
    padding: 5px 10px;
    font-size: 15px;
}

QPushButton#appMenuBtn:hover,
QPushButton#appMenuBtn:checked {
    background-color: #f1f2f5;
    border-color: $border;
    color: $text;
}

/* 接口状态：紧凑圆点 + 模型名，只占应用栏右侧一小段 */
QLabel#modelChip {
    background-color: $disabled_bg;
    color: $text_muted;
    border: 1px solid #e6e8ec;
    border-radius: 9px;
    padding: 3px 9px;
    font-size: 11px;
}

QLabel#modelChip[status="ready"] {
    background-color: #eaf7ef;
    color: $success;
    border-color: #c5e6d2;
}

QLabel#modelChip[status="warning"] {
    background-color: $warning_bg;
    color: #9a5a00;
    border-color: #f0d8b4;
}

/* 更新提示胶囊：仅在发现新版本时出现，用主行动蓝吸引注意但不抢主行动按钮 */
QPushButton#updateChip {
    background-color: #e8f1fd;
    color: #1c5fb8;
    border: 1px solid #bcd7f5;
    border-radius: 9px;
    padding: 3px 9px;
    font-size: 11px;
}

QPushButton#updateChip:hover {
    background-color: #d9e8fb;
    border-color: #a3c7f0;
}

QLabel#statusChip {
    background-color: $disabled_bg;
    color: $text_secondary;
    border: 1px solid #e6e8ec;
    border-radius: 9px;
    padding: 3px 9px;
    font-size: 11px;
}

/* ============================== 工作台动作行 ============================== */
QFrame#actionBar {
    background-color: $surface;
    border: none;
    border-bottom: 1px solid $pressed;
}

QLabel#actionTitle {
    color: $text;
    font-size: 13px;
    font-weight: 750;
}

QLabel#actionStatus {
    color: $text_muted;
    font-size: 11px;
}

/* ============================== 按钮 ============================== */
QPushButton {
    background-color: $surface;
    color: $text_strong;
    border: 1px solid #d9dce1;
    border-radius: 8px;
    padding: 5px 11px;
    min-height: 16px;
    font-size: 12px;
    font-weight: 650;
}

QPushButton:hover {
    background-color: $hover;
    border-color: #c3c7ce;
}

QPushButton:pressed {
    background-color: $pressed;
}

QPushButton:focus {
    border: 1px solid $primary;
}

QPushButton:disabled {
    background-color: $disabled_bg;
    color: #b0b4bb;
    border-color: $pressed;
}

QPushButton#primaryBtn {
    background-color: $primary;
    color: $surface;
    border-color: $primary;
    font-weight: 750;
}

QPushButton#primaryBtn:hover {
    background-color: $primary_hover;
    border-color: $primary_hover;
}

QPushButton#primaryBtn:disabled {
    background-color: #b9cdf6;
    color: #f7faff;
    border-color: #b9cdf6;
}

/* 次要操作：与普通按钮拉开层级，但不像软删除按钮那样抢眼 */
QPushButton#secondaryBtn {
    background-color: #f7f8fa;
    color: #4a4f57;
    border-color: #e1e4e9;
    font-weight: 600;
}

QPushButton#secondaryBtn:hover {
    background-color: #eef0f4;
    border-color: #cfd4db;
}

QPushButton#secondaryBtn:focus {
    border-color: $primary;
}

QPushButton#softBtn {
    background-color: #fff1f0;
    color: $danger;
    border-color: #f4c7c3;
}

QPushButton#softBtn:hover {
    background-color: #ffe3e0;
    border-color: #e9aaa4;
}

QPushButton#successBtn {
    background-color: $success;
    color: $surface;
    border-color: $success;
    font-weight: 750;
}

QPushButton#successBtn:hover {
    background-color: #186240;
}

QPushButton#dangerBtn {
    background-color: #fff1f0;
    color: $danger;
    border-color: #f4c7c3;
}

QPushButton#dangerBtn:hover {
    background-color: #ffe3e0;
}

QPushButton#iconBtn {
    padding-left: 8px;
    padding-right: 8px;
    min-width: 24px;
}

/* 面板开关：图标按钮，选中即面板可见。
   内边距压到 3px 8px —— 阅读工具栏控件密集，1024px 最小窗口下
   正文区只剩约 400px，每多 2px 内边距都会把按钮挤变形。 */
QPushButton#paneToggle {
    padding: 3px 8px;
    font-size: 11px;
    color: $text_secondary;
    background-color: $surface;
    border: 1px solid #dfe2e7;
}

QPushButton#paneToggle:hover {
    background-color: $hover;
    color: $text;
}

QPushButton#paneToggle:checked {
    background-color: $primary_soft;
    color: $primary;
    border-color: $primary_soft_strong;
}

QPushButton#fontBtn {
    min-width: 26px;
    padding: 3px 6px;
    font-size: 12px;
    font-weight: 800;
}

/* ============================== 文本与输入 ============================== */
QLabel {
    color: $text_strong;
}

QLabel#titleLabel {
    color: $text;
    font-size: 14px;
    font-weight: 800;
}

QLabel#sectionLabel {
    color: $text;
    font-size: 12px;
    font-weight: 750;
}

QLabel#subtitleLabel {
    color: $text_muted;
    font-size: 11px;
}

QLabel#eyebrowLabel {
    color: $primary;
    font-size: 10px;
    font-weight: 800;
    letter-spacing: 1px;
}

QLineEdit,
QTextEdit,
QPlainTextEdit,
QSpinBox,
QComboBox {
    background-color: $surface;
    color: $text;
    border: 1px solid #dcdfe4;
    border-radius: 7px;
    padding: 6px 9px;
    selection-background-color: #cfe0ff;
    selection-color: $text;
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
    border-color: $primary;
}

QLineEdit:disabled,
QTextEdit:disabled,
QPlainTextEdit:disabled,
QComboBox:disabled {
    background-color: $disabled_bg;
    color: #b0b4bb;
}

QComboBox {
    padding-right: 26px;
}

QComboBox:hover {
    border-color: #c3c7ce;
}

QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 22px;
    border: none;
    background: transparent;
}

QComboBox::down-arrow {
    image: none;
    width: 0;
    height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid #6b7078;
}

QComboBox QAbstractItemView {
    background-color: $surface;
    color: $text;
    border: 1px solid #dcdfe4;
    selection-background-color: $primary_soft;
    selection-color: $primary;
    outline: none;
    padding: 4px;
}

QComboBox QAbstractItemView::item {
    padding: 6px 9px;
    border-radius: 5px;
}

QComboBox QAbstractItemView::item:hover {
    background-color: $hover;
}

QCheckBox,
QRadioButton {
    color: #4a4f57;
    spacing: 6px;
    font-size: 12px;
}

QCheckBox::indicator,
QRadioButton::indicator {
    width: 15px;
    height: 15px;
    background-color: $surface;
    border: 1px solid #c9cdd4;
}

QCheckBox::indicator {
    border-radius: 4px;
}

QRadioButton::indicator {
    border-radius: 8px;
}

QCheckBox::indicator:checked,
QRadioButton::indicator:checked {
    background-color: $primary;
    border-color: $primary;
}

/* ============================== 编辑器 ============================== */
QTextEdit#draftEditor {
    background-color: $surface;
    color: $text;
    border: 1px solid $border;
    border-radius: 10px;
    padding: 22px 26px;
    font-size: 14px;
}

QTextEdit#draftEditor:focus {
    border-color: $primary;
}

QFrame#editorCard {
    background-color: $surface;
    border: 1px solid $border;
    border-radius: 12px;
}

/* ============================== 标签页与分割器 ============================== */
QTabWidget::pane {
    border: none;
    background: transparent;
}

QTabBar::tab {
    background: transparent;
    color: #7c818a;
    border: none;
    padding: 7px 13px;
    margin-right: 2px;
    font-weight: 700;
    font-size: 12px;
}

QTabBar::tab:selected {
    color: $primary;
    border-bottom: 2px solid $primary;
}

QTabBar::tab:hover:!selected {
    color: $text;
    background-color: $hover;
}

QSplitter::handle {
    background-color: $border;
}

QSplitter::handle:horizontal {
    width: 3px;
    margin: 0;
}

QSplitter::handle:vertical {
    height: 3px;
    margin: 0;
}

QSplitter::handle:hover {
    background-color: #a8c5ff;
}

/* ============================== 列表、树、滚动条 ============================== */
QTreeWidget,
QListWidget {
    background-color: $surface;
    color: $text;
    border: none;
    outline: none;
}

QTreeWidget::item,
QListWidget::item {
    padding: 5px 8px;
    min-height: 20px;
    border-radius: 6px;
}

QTreeWidget::item:hover,
QListWidget::item:hover {
    background-color: $hover;
}

QTreeWidget::item:selected,
QListWidget::item:selected {
    background-color: $primary_soft;
    color: $primary;
}

QScrollArea {
    background-color: transparent;
    border: none;
}

/* QScrollArea 的 viewport / 内容宿主默认是 Qt 调色板灰底。空列表时
   会露出一整块灰色矩形，看起来像加载失败；统一透明，让容器背景透出来。 */
QScrollArea > QWidget > QWidget,
QAbstractScrollArea::corner {
    background-color: transparent;
}

QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 2px;
}

QScrollBar::handle:vertical {
    background: #ccd0d6;
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
    height: 10px;
    margin: 2px;
}

QScrollBar::handle:horizontal {
    background: #ccd0d6;
    min-width: 34px;
    border-radius: 4px;
}

QScrollBar::add-line:horizontal,
QScrollBar::sub-line:horizontal {
    width: 0;
}

/* ============================== 分组与状态 ============================== */
QGroupBox {
    background-color: $surface;
    color: $text;
    border: 1px solid $border;
    border-radius: 10px;
    margin-top: 9px;
    padding: 15px 10px 10px 10px;
    font-weight: 750;
    font-size: 12px;
}

QGroupBox::title {
    subcontrol-origin: margin;
    left: 11px;
    padding: 0 6px;
    color: $text_strong;
    background-color: $surface;
}

QProgressBar {
    background-color: $pressed;
    border: none;
    border-radius: 4px;
    text-align: center;
    color: transparent;
    height: 6px;
}

QProgressBar::chunk {
    background-color: $primary;
    border-radius: 4px;
}

QStatusBar {
    background-color: $surface;
    color: #7c818a;
    border-top: 1px solid $pressed;
    padding: 2px 10px;
    font-size: 11px;
}

QStatusBar::item {
    border: none;
}

QToolTip {
    background-color: $text;
    color: $surface;
    border: 1px solid $text_strong;
    border-radius: 6px;
    padding: 6px 9px;
    font-size: 12px;
}

/* ============================== 菜单与弹窗 ============================== */
QMenuBar {
    background-color: $surface;
    color: $text_strong;
    border: none;
    padding: 2px 6px;
    font-size: 12px;
}

QMenuBar::item {
    padding: 4px 10px;
    border-radius: 6px;
}

QMenuBar::item:selected {
    background-color: #f1f2f5;
}

QMenu {
    background-color: $surface;
    color: $text;
    border: 1px solid #dcdfe4;
    border-radius: 9px;
    padding: 5px;
}

QMenu::item {
    padding: 7px 26px 7px 12px;
    border-radius: 6px;
    font-size: 12px;
}

QMenu::item:selected {
    background-color: $primary_soft;
    color: $primary;
}

QMenu::separator {
    height: 1px;
    background-color: $pressed;
    margin: 4px 8px;
}

QDialog,
QMessageBox {
    background-color: $bg;
}

QMessageBox QLabel {
    color: $text_strong;
    font-size: 13px;
}

/* ============================== 内容卡片 ============================== */
ParagraphCard,
ImageCard,
ClaimResultCard,
ChatBubble {
    background-color: $surface;
    border: 1px solid #e8eaee;
    border-radius: 10px;
}

ParagraphCard:hover,
ImageCard:hover,
ClaimResultCard:hover {
    border-color: #c3d7fb;
}

ChatBubble[role="user"] {
    background-color: $primary_soft;
    border-color: $primary_soft_strong;
}

QFrame#chatInput,
QFrame#qaInput {
    background-color: $surface;
    border-top: 1px solid $pressed;
}

/* ============================== 检索工作台 ============================== */
QFrame#aiSearchPanel {
    background-color: $surface;
    border: 1px solid $border;
    border-radius: 12px;
}

QFrame#scoutPanel,
QFrame#statsDataPanel,
QFrame#planCard,
QFrame#todayCard,
QFrame#heatmapCard,
QFrame#topPapersCard,
QFrame#writingSidePanel {
    background-color: $surface;
    border: 1px solid $border;
    border-radius: 12px;
}

QWidget#scoutSection,
QFrame#scoutSection {
    background-color: transparent;
    border: none;
}

QFrame#topicCard,
QFrame#scoutCard {
    background-color: $surface;
    border: 1px solid #e8eaee;
    border-radius: 10px;
}

QFrame#topicCard:hover,
QFrame#scoutCard:hover {
    border-color: #c3d7fb;
}

QFrame#refCard {
    background-color: #f7faff;
    border: 1px solid #dbe7fd;
    border-radius: 10px;
}

QPushButton#refRow {
    background-color: transparent;
    border: none;
    border-radius: 6px;
    padding: 4px 6px;
    text-align: left;
    color: #26507f;
    font-size: 12px;
    font-weight: 600;
}

QPushButton#refRow:hover {
    background-color: $primary_soft;
    color: $primary;
}

QPushButton#refRow:disabled {
    background-color: transparent;
    color: #b0b4bb;
}

QPushButton#topicToggle {
    padding: 3px 9px;
    font-size: 11px;
}

QPushButton#topicToggle:checked {
    background-color: $primary_soft;
    color: $primary;
    border-color: $primary_soft_strong;
}

QLabel#feedChip {
    background-color: $primary_soft;
    color: $primary;
    border: 1px solid $primary_soft_strong;
    border-radius: 8px;
    padding: 1px 7px;
    font-size: 10px;
    font-weight: 700;
}

QLabel#feedChip[kind="topic"] {
    background-color: $warning_bg;
    color: #9a5a00;
    border-color: #f0d8b4;
}

QLabel#feedChip[kind="library"] {
    background-color: #eaf7ef;
    color: $success;
    border-color: #c5e6d2;
}

/* ============================== 写作工具检查器 ============================== */
QFrame#writingSideScroll {
    background-color: transparent;
    border: none;
}

QTabWidget#writingInspectorTabs::pane {
    border: none;
    background: transparent;
}

QTabWidget#writingInspectorTabs QTabBar::tab {
    padding: 7px 7px;
    margin-right: 1px;
    font-size: 12px;
}

QTabWidget#writingInspectorTabs QGroupBox {
    background-color: transparent;
    border: none;
    border-radius: 0;
    margin-top: 12px;
    padding: 2px 2px 4px 2px;
    font-weight: 700;
}

QTabWidget#writingInspectorTabs QGroupBox::title {
    left: 2px;
    padding: 0;
    color: $text_muted;
    font-size: 11px;
    letter-spacing: 1px;
    background-color: transparent;
}

QFrame#reviewBar {
    background-color: #f2f8f5;
    border: 1px solid #cfe5d9;
    border-radius: 9px;
}

/* ============================== 统计工作台 ============================== */
QFrame#todayCell {
    background-color: #f7faff;
    border: 1px solid #dbe7fd;
    border-radius: 9px;
}

QLabel#todayValue {
    color: $text;
    font-size: 17px;
    font-weight: 700;
}

QLabel#streakChip {
    background-color: $warning_bg;
    color: #9a5a00;
    border: 1px solid #f0d8b4;
    border-radius: 8px;
    padding: 1px 8px;
    font-size: 11px;
    font-weight: 700;
}

QLabel#topRow {
    background-color: #f7f8fa;
    border: 1px solid #eef0f3;
    border-radius: 7px;
    padding: 5px 8px;
    font-size: 12px;
    color: $text;
}

QCheckBox#planTask {
    font-size: 13px;
    color: $text;
    padding: 3px 2px;
}

/* 任务文字由 WrapCheckBox 内嵌的 QLabel 承载（支持随面板宽度自动折行） */
QLabel#planTaskText {
    font-size: 13px;
    color: $text;
}

QLabel#planTaskText[done="true"] {
    color: #a3a8b0;
    text-decoration: line-through;
}

QLabel#sectionLabel[overdue="true"] {
    color: $danger;
}
"""

STYLESHEET = Template(_RAW_STYLESHEET).substitute(QSS_TOKENS)
