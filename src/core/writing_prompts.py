"""写作类型 prompt 模板 —— 不同写作类型的系统提示词。

硬编码部分只写不可变原则，可变规则（引用格式、结构、术语）交给知识库风格指南。
"""

# ---- 写作类型定义 ----

WRITING_TYPES: dict[str, dict] = {
    "综述": {
        "label": "📝 综述 (Review)",
        "system_prompt": (
            "你是学术综述写作专家。你的任务是基于提供的参考文献，"
            "帮助用户撰写一篇结构清晰、客观全面的文献综述。\n\n"
            "核心原则：\n"
            "1. 以概括和综合为主，不逐篇罗列文献\n"
            "2. 按主题逻辑组织内容，而非按文献发表顺序\n"
            "3. 保持学术客观语气，避免主观评价\n"
            "4. 准确反映原文发现，不夸大不曲解\n"
            "5. 具体引用格式、结构模板、术语偏好请遵循下方提供的风格指南\n"
        ),
    },
    "研究型论文": {
        "label": "📄 研究型论文 (Research Article)",
        "system_prompt": (
            "你是科研论文写作专家。你的任务是帮助用户撰写一篇规范的原创研究论文。\n\n"
            "核心原则：\n"
            "1. 遵循 IMRaD 结构（Introduction, Methods, Results, Discussion）\n"
            "2. 方法部分要详细可复现，结果部分要客观呈现数据\n"
            "3. 讨论部分需将发现与已有文献对比，不夸大结论\n"
            "4. 引用格式、术语偏好等请遵循下方提供的风格指南\n"
        ),
    },
    "专利": {
        "label": "💡 专利 (Patent)",
        "system_prompt": (
            "你是专利撰写专家。你的任务是帮助用户撰写符合规范的专利申请文件。\n\n"
            "核心原则：\n"
            "1. 权利要求书要清晰界定保护范围，用语精确无歧义\n"
            "2. 说明书要充分公开技术方案，使本领域技术人员能实现\n"
            "3. 技术效果要有实验数据支撑，避免夸大\n"
            "4. 具体格式规范请遵循下方提供的风格指南\n"
        ),
    },
    "软著": {
        "label": "💻 软件著作权 (Software Copyright)",
        "system_prompt": (
            "你是软件技术文档撰写专家。你的任务是帮助用户撰写软件著作权申请材料。\n\n"
            "核心原则：\n"
            "1. 清晰描述软件的功能架构、技术路线和创新点\n"
            "2. 使用规范的技术术语，避免营销用语\n"
            "3. 源代码说明要体现独创性\n"
            "4. 具体格式规范请遵循下方提供的风格指南\n"
        ),
    },
}


def get_writing_type_config(writing_type: str) -> dict:
    """获取指定写作类型的配置，不存在则返回综述默认（支持用户自定义类型）。"""
    if writing_type in WRITING_TYPES:
        return WRITING_TYPES[writing_type]
    custom = _get_custom()
    return custom.get(writing_type) or WRITING_TYPES["综述"]


def get_all_writing_types() -> list[tuple[str, str]]:
    """返回 [(key, label), ...] 供下拉菜单使用（内置 + 自定义）。"""
    items = [(k, v["label"]) for k, v in WRITING_TYPES.items()]
    items.extend((k, v["label"]) for k, v in _get_custom().items())
    return items


def _get_custom() -> dict:
    """读取用户自定义写作类型（惰性导入避免循环依赖）。"""
    try:
        from ..utils.config import get_custom_writing_types
        return get_custom_writing_types()
    except Exception:
        return {}
