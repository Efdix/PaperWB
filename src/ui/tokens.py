"""PaperWB 视觉 token —— 颜色/字号/间距/圆角的单一来源。

QSS 用 ``$name`` 占位符，由 ``styles.py`` 在导入时用 ``Template`` 替换。
新增样式优先引用这里的语义名，不要继续写十六进制字面量；内联样式里
至少复用颜色常量，避免同一个“次要文字灰”出现五六种近似值。
"""

from __future__ import annotations

# ---- 品牌与语义色 ----
PRIMARY = "#2f6fed"
PRIMARY_HOVER = "#2559c9"
PRIMARY_SOFT = "#eaf1ff"
PRIMARY_SOFT_STRONG = "#cddffb"

TEXT = "#1c1e21"
TEXT_STRONG = "#33373d"
TEXT_SECONDARY = "#61656c"
TEXT_MUTED = "#8b9099"

SURFACE = "#ffffff"
BG = "#f4f5f7"
HOVER = "#f5f6f8"
PRESSED = "#e9ebef"
DISABLED_BG = "#f6f7f9"

BORDER = "#e3e5e9"
BORDER_STRONG = "#d9dce1"

SUCCESS = "#1f7a4d"
WARNING = "#a76d2b"
WARNING_BG = "#fff5e8"
DANGER = "#b42318"

# 阅读区保持独立的暖色/青绿语义，但集中在这里而不是散落在卡片内联样式里
READER_TEXT = "#29434a"
READER_ACCENT = "#278273"
READER_WARM = "#a76d2b"
READER_PAPER = "#fbfaf7"

# ---- 字号（px） ----
FONT_XS = 11
FONT_SM = 12
FONT_BASE = 13
FONT_MD = 14
FONT_LG = 15
FONT_XL = 17
FONT_XXL = 22

# ---- 间距（px） ----
SPACE_XS = 4
SPACE_SM = 6
SPACE_MD = 10
SPACE_LG = 14
SPACE_XL = 20

# ---- 圆角（px） ----
RADIUS_SM = 6
RADIUS_MD = 8
RADIUS_LG = 12

# QSS 占位符 → 值（键名与 `$name` 对应）
QSS_TOKENS: dict[str, str] = {
    "primary": PRIMARY,
    "primary_hover": PRIMARY_HOVER,
    "primary_soft": PRIMARY_SOFT,
    "primary_soft_strong": PRIMARY_SOFT_STRONG,
    "text": TEXT,
    "text_strong": TEXT_STRONG,
    "text_secondary": TEXT_SECONDARY,
    "text_muted": TEXT_MUTED,
    "surface": SURFACE,
    "bg": BG,
    "hover": HOVER,
    "pressed": PRESSED,
    "disabled_bg": DISABLED_BG,
    "border": BORDER,
    "border_strong": BORDER_STRONG,
    "success": SUCCESS,
    "warning": WARNING,
    "warning_bg": WARNING_BG,
    "danger": DANGER,
}
