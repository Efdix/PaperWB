"""PDF 解析器 —— 精简版：文本提取 + 页面渲染。

保留纯工具性功能供上层使用。
所有智能解析（段落分割、结构识别、图表理解）已迁移至 pdf_processor.py
"""

from __future__ import annotations

import fitz  # PyMuPDF


class PDFParser:
    """PDF 基础解析器 —— 提供文本提取、页面渲染等底层能力。

    不再包含任何智能解析逻辑（列检测、段落合并、标题识别等），
    这些已全部交给 pdf_processor.py 中的视觉 LLM 管线处理。
    """

    def __init__(self, file_path: str) -> None:
        self.file_path = file_path
        self._doc = fitz.open(file_path)
        self._full_text: str | None = None

    @property
    def page_count(self) -> int:
        return len(self._doc)

    @property
    def metadata(self) -> dict:
        return self._doc.metadata

    def get_toc(self) -> list[dict]:
        toc = self._doc.get_toc(simple=False)
        if not toc:
            return []
        result = []
        for item in toc:
            level, title, page = item[0], item[1], item[2]
            if title.strip():
                result.append({"level": level, "title": title.strip(), "page": page})
        return result

    def close(self) -> None:
        self._doc.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def render_page_to_base64(self, page_num: int, dpi: int = 150) -> str:
        import base64
        page = self._doc[page_num - 1]
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat)
        img_bytes = pix.tobytes("png")
        b64 = base64.b64encode(img_bytes).decode("ascii")
        return f"data:image/png;base64,{b64}"

    def render_image_region(self, page_num: int, bbox: tuple, output_path: str, dpi: int = 200) -> None:
        x0, y0, x1, y1 = bbox
        clip = fitz.Rect(x0 - 2, y0 - 2, x1 + 2, y1 + 2)
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        page = self._doc[page_num - 1]
        pix = page.get_pixmap(matrix=mat, clip=clip)
        pix.save(output_path)

    def extract_full_text(self) -> str:
        if self._full_text is not None:
            return self._full_text
        parts = []
        for i, page in enumerate(self._doc, 1):
            text = page.get_text()
            if text.strip():
                parts.append(f"[第 {i} 页]\n{text.strip()}")
        self._full_text = "\n\n".join(parts)
        return self._full_text

    def get_page_text(self, page_num: int) -> str:
        if page_num < 1 or page_num > len(self._doc):
            return ""
        return self._doc[page_num - 1].get_text().strip()
