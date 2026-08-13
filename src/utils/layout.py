"""
共享布局辅助工具 —— 递归计算布局高度，供各 UI 面板的 heightForWidth 使用。
"""

from PySide6.QtWidgets import QHBoxLayout, QLayout


def calc_layout_height(layout: QLayout, inner_width: int) -> int:
    """递归计算布局在给定宽度下所需的高度。

    ``inner_width`` 是布局所在控件可用的总宽度；本函数负责扣除并计入
    布局自己的左右/上下边距。对 QVBoxLayout 累加可见元素高度，对
    QHBoxLayout 取最大子元素高度，并只在可见元素之间计算 spacing。
    """
    if layout is None:
        return 0

    margins = layout.contentsMargins()
    content_width = max(
        inner_width - margins.left() - margins.right(),
        50,
    )
    is_horizontal = isinstance(layout, QHBoxLayout)
    spacing = layout.spacing()
    visible_heights: list[int] = []

    for i in range(layout.count()):
        item = layout.itemAt(i)
        if item is None:
            continue

        child_h = 0
        if widget := item.widget():
            # isVisible() also depends on hidden ancestors; during construction
            # that would make every child look absent and collapse the card.
            if widget.isHidden():
                continue
            if widget.hasHeightForWidth() or widget.sizePolicy().hasHeightForWidth():
                child_h = widget.heightForWidth(content_width)
            elif widget.layout():
                widget_margins = widget.contentsMargins()
                widget_width = max(
                    content_width - widget_margins.left() - widget_margins.right(),
                    50,
                )
                child_h = (
                    widget_margins.top()
                    + widget_margins.bottom()
                    + calc_layout_height(widget.layout(), widget_width)
                )
            else:
                child_h = widget.sizeHint().height()
        elif sub := item.layout():
            child_h = calc_layout_height(sub, content_width)
        elif item.spacerItem():
            continue

        if child_h > 0:
            visible_heights.append(child_h)

    if is_horizontal:
        content_height = max(visible_heights, default=0)
    else:
        content_height = sum(visible_heights)
        if len(visible_heights) > 1:
            content_height += spacing * (len(visible_heights) - 1)
    return margins.top() + content_height + margins.bottom()
