# -*- coding: utf-8 -*-
"""Word 读写核心自测（python-docx 构造含批注文档 → 读写往返）。

覆盖：read_docx 段落/批注/修订解析、write_docx 段落重建 + 批注保留、
无批注文档写回、修订标记检测。
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
    else:
        FAIL.append(f"{name} {detail}")


from docx import Document
from docx.oxml.ns import qn
from docx.opc.part import Part
from docx.opc.packuri import PackURI
from docx.oxml import parse_xml
from lxml import etree

from src.core.docx_io import DocxComment, read_docx, write_docx, has_unsaved_changes


def make_docx_with_comments(path: str, paragraphs: list[str],
                            comments: list[tuple[str, str, str]],
                            ranges: list[tuple[int, int]] | None = None) -> None:
    """构造含批注的测试文档。

    comments: [(author, text, paragraph_index)]
    ranges: 每个批注的段内字符范围 (start, end)；None = 批注锚定到段落开头
    """
    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    doc.save(path)

    xml_parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">',
    ]
    for i, (author, text, _pi) in enumerate(comments):
        xml_parts.append(
            f'<w:comment w:id="{i}" w:author="{author}" w:date="2026-08-01T10:00:00Z">'
            f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:comment>"
        )
    xml_parts.append("</w:comments>")
    comments_xml = parse_xml("".join(xml_parts).encode("utf-8"))
    partname = PackURI("/word/comments.xml")
    content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"
    part = Part(partname, content_type, etree.tostring(comments_xml), doc.part.package)
    doc.part.package.parts.append(part)
    doc.part.relate_to(
        part,
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments",
    )
    for pid, para in enumerate(doc.paragraphs):
        start = para._p.makeelement(qn("w:commentRangeStart"), {qn("w:id"): str(pid)})
        end = para._p.makeelement(qn("w:commentRangeEnd"), {qn("w:id"): str(pid)})
        ref = para._p.makeelement(qn("w:r"), {})
        ref_el = para._p.makeelement(qn("w:commentReference"), {qn("w:id"): str(pid)})
        ref.append(ref_el)
        para._p.insert(0, start)
        para._p.append(end)
        para._p.append(ref)
    doc.save(path)


tmp = Path(tempfile.mkdtemp(prefix="paperwb_docx_test_"))
try:
    # ---------- 1. 读取含批注文档 ----------
    path = tmp / "with_comments.docx"
    make_docx_with_comments(
        str(path),
        ["第一段：鸟类羽色发育的黑色素细胞研究。",
         "第二段：单细胞测序揭示了新的调控通路。"],
        [("导师", "这里需要补充参考文献", 0),
         ("导师", "口语化，建议改写", 1)],
    )
    content = read_docx(str(path))
    check("段落数", len(content.paragraphs) == 2, repr(content.paragraphs))
    check("段落文本", content.paragraphs[0].startswith("第一段"), content.paragraphs[0])
    check("批注数", len(content.comments) == 2, repr(content.comments))
    c0 = content.comments[0]
    check("批注作者", c0.author == "导师", c0.author)
    check("批注文本", c0.text == "这里需要补充参考文献", c0.text)
    check("批注锚定段落", c0.paragraph_index == 0, str(c0.paragraph_index))
    check("批注2锚定段落", content.comments[1].paragraph_index == 1,
          str(content.comments[1].paragraph_index))
    check("无修订标记", not content.has_revisions)
    # 批注锚定在段落开头（commentRangeStart 插在段首）→ 偏移 (0, 0)
    check("批注字符偏移存在", c0.char_start >= 0 and c0.char_end >= 0,
          f"{c0.char_start},{c0.char_end}")

    # ---------- 2. 写回：内容修改 + 批注保留 ----------
    write_docx(str(path), ["修改后的第一段内容。",
                           "第二段：单细胞测序揭示了新的调控通路。"],
               comments=content.comments)
    content2 = read_docx(str(path))
    check("写回内容", content2.paragraphs[0] == "修改后的第一段内容。",
          repr(content2.paragraphs))
    check("写回批注保留", len(content2.comments) == 2, repr(content2.comments))
    check("写回批注文本", content2.comments[0].text == "这里需要补充参考文献",
          content2.comments[0].text)
    check("写回批注锚定", content2.comments[0].paragraph_index == 0,
          str(content2.comments[0].paragraph_index))
    check("写回批注范围有效",
          content2.comments[0].char_start >= 0
          and content2.comments[0].char_end > content2.comments[0].char_start,
          f"{content2.comments[0].char_start},{content2.comments[0].char_end}")

    # ---------- 3. 无批注文档写回 ----------
    path2 = tmp / "plain.docx"
    doc = Document()
    doc.add_paragraph("只有一段。")
    doc.save(str(path2))
    write_docx(str(path2), ["只有一段。"])
    c3 = read_docx(str(path2))
    check("无批注写回", c3.paragraphs == ["只有一段。"] and c3.comments == [],
          repr(c3.paragraphs))

    # ---------- 4. 修订标记检测 ----------
    path3 = tmp / "revised.docx"
    doc = Document()
    p = doc.add_paragraph("修订前文本")
    ins = p._p.makeelement(qn("w:ins"), {})
    r = p._p.makeelement(qn("w:r"), {})
    t = p._p.makeelement(qn("w:t"), {})
    t.text = "新增内容"
    r.append(t)
    ins.append(r)
    p._p.append(ins)
    doc.save(str(path3))
    c4 = read_docx(str(path3))
    check("修订标记检测", c4.has_revisions, str(c4.has_revisions))
    check("修订文本合并", "新增内容" in c4.paragraphs[0], c4.paragraphs[0])

    # ---------- 5. DocxComment.to_dict ----------
    d = DocxComment(comment_id="x", author="A", text="T", paragraph_index=2).to_dict()
    check("批注 to_dict", d["comment_id"] == "x" and d["paragraph_index"] == 2, repr(d))

    # ---------- 6. 原位写回：格式完整保留 ----------
    from docx.shared import Pt
    fmt_path = tmp / "fmt.docx"
    fdoc = Document()
    fp0 = fdoc.add_paragraph()
    fr0 = fp0.add_run("这是加粗标题")
    fr0.bold = True
    fr0.font.size = Pt(16)
    fr0.font.name = "SimSun"
    fp1 = fdoc.add_paragraph()
    fr1 = fp1.add_run("第一段正文内容")
    fr1.italic = True
    fp1.add_run().add_picture(
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "PaperWB.jpg"),
        width=Pt(24),
    )
    fdoc.add_paragraph("第二段")
    fdoc.sections[0].header.paragraphs[0].text = "我的页眉"
    ftable = fdoc.add_table(rows=2, cols=2)
    ftable.cell(0, 0).text = "A1"
    ftable.cell(0, 1).text = "B1"
    ftable.cell(1, 0).text = "A2"
    ftable.cell(1, 1).text = "B2"
    fdoc.save(str(fmt_path))

    write_docx(str(fmt_path), ["这是修改后的加粗标题", "第一段正文已修改", "第二段"])
    fdoc2 = Document(str(fmt_path))
    fparas = fdoc2.paragraphs
    check("写回段落数", len(fparas) == 3, str(len(fparas)))
    _r = fparas[0].runs[0]
    check("字符格式保留(加粗/字号/字体)",
          _r.bold and _r.font.size == Pt(16) and _r.font.name == "SimSun",
          f"bold={_r.bold} size={_r.font.size} font={_r.font.name}")
    check("字符格式后文本替换", fparas[0].text == "这是修改后的加粗标题",
          fparas[0].text)
    check("斜体保留", fparas[1].runs[0].italic and fparas[1].text == "第一段正文已修改",
           repr(fparas[1].runs[0].italic))
    check("行内图片保留", len(fdoc2.inline_shapes) == 1, str(len(fdoc2.inline_shapes)))
    check("页眉保留", fdoc2.sections[0].header.paragraphs[0].text == "我的页眉",
          fdoc2.sections[0].header.paragraphs[0].text)
    check("表格保留", len(fdoc2.tables) == 1 and fdoc2.tables[0].cell(0, 0).text == "A1",
          str(len(fdoc2.tables)))

    # 段落减少：多余段落删除，页眉仍保留
    write_docx(str(fmt_path), ["只剩一段"])
    fdoc3 = Document(str(fmt_path))
    check("段落减少", len(fdoc3.paragraphs) == 1
          and fdoc3.paragraphs[0].text == "只剩一段", str(len(fdoc3.paragraphs)))
    check("删段后页眉保留",
          fdoc3.sections[0].header.paragraphs[0].text == "我的页眉",
          fdoc3.sections[0].header.paragraphs[0].text)

    # 段落增加
    write_docx(str(fmt_path), ["第一段", "第二段", "第三段"])
    fdoc4 = Document(str(fmt_path))
    check("段落增加", len(fdoc4.paragraphs) == 3, str(len(fdoc4.paragraphs)))

    # ---------- 7. 原位写回：批注随段落保留（不重锚定） ----------
    # 复用第 1 节的带批注文档（path），原位写回后批注应保留且锚定正确
    write_docx(str(path), ["第一段：修改后的鸟类研究。",
                           "第二段：单细胞测序揭示了新的调控通路。"])
    c5 = read_docx(str(path))
    check("原位写回批注保留", len(c5.comments) == 2, str(len(c5.comments)))
    check("原位写回批注锚定",
          c5.comments[0].paragraph_index == 0 and c5.comments[1].paragraph_index == 1,
          str([c.paragraph_index for c in c5.comments]))
    check("原位写回内容", c5.paragraphs[0] == "第一段：修改后的鸟类研究。",
          c5.paragraphs[0])

    # ---------- 8. 同段多条批注写回：锚点不互相吞并 ----------
    multi_path = tmp / "multi_comments.docx"
    doc = Document()
    doc.add_paragraph("第一段：鸟类羽色发育的黑色素细胞研究。")
    doc.save(str(multi_path))
    xml_parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">',
        '<w:comment w:id="0" w:author="导师" w:date="2026-08-01T10:00:00Z">'
        "<w:p><w:r><w:t>批注一</w:t></w:r></w:p></w:comment>",
        '<w:comment w:id="1" w:author="导师" w:date="2026-08-01T10:00:00Z">'
        "<w:p><w:r><w:t>批注二</w:t></w:r></w:p></w:comment>",
        "</w:comments>",
    ]
    comments_part = Part(
        PackURI("/word/comments.xml"),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml",
        etree.tostring(parse_xml("".join(xml_parts).encode("utf-8"))),
        doc.part.package,
    )
    doc.part.package.parts.append(comments_part)
    doc.part.relate_to(
        comments_part,
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments",
    )
    p0 = doc.paragraphs[0]._p
    s0 = p0.makeelement(qn("w:commentRangeStart"), {qn("w:id"): "0"})
    s1 = p0.makeelement(qn("w:commentRangeStart"), {qn("w:id"): "1"})
    e0 = p0.makeelement(qn("w:commentRangeEnd"), {qn("w:id"): "0"})
    e1 = p0.makeelement(qn("w:commentRangeEnd"), {qn("w:id"): "1"})
    p0.insert(1, s0)
    p0.insert(2, s1)
    p0.append(e0)
    p0.append(e1)
    doc.save(str(multi_path))

    multi = read_docx(str(multi_path))
    check("同段批注解析", len(multi.comments) == 2, str(len(multi.comments)))
    write_docx(str(multi_path), ["修改后的同一段内容。", "新增第二段"])
    multi2 = read_docx(str(multi_path))
    check("同段批注保存保留", len(multi2.comments) == 2, str(len(multi2.comments)))
    check("同段批注范围有效",
          all(c.char_start >= 0 and c.char_end > c.char_start
              for c in multi2.comments),
          str([(c.char_start, c.char_end) for c in multi2.comments]))

    # ==================================================
    # 9. v2 run 级合并：未变 run 的字符格式逐字节保留
    # ==================================================
    from docx.oxml import OxmlElement
    from lxml import etree as _etree

    def _para_xml(p):
        return _etree.tostring(p._p)

    run_path = tmp / "runs.docx"
    rdoc = Document()
    rp = rdoc.add_paragraph()
    ra = rp.add_run("AAA开头 ")
    rb = rp.add_run("BBB中间句")
    rb.bold = True
    rb.font.name = "Times New Roman"
    rc = rp.add_run(" CCC结尾。")
    rc.italic = True
    rdoc.save(str(run_path))

    before_doc = Document(str(run_path))
    before_xmls = [_para_xml(p) for p in before_doc.paragraphs]
    # 只改中间句（BBB 中间句 → BBB 已改写）
    write_docx(str(run_path), ["AAA开头 BBB已改写 CCC结尾。"])
    adoc = Document(str(run_path))
    ap = adoc.paragraphs[0]
    check("run级：整段文本", ap.text == "AAA开头 BBB已改写 CCC结尾。", ap.text)
    check("run级：未变片段格式(斜体尾段)",
          ap.runs[-1].italic, repr([r.text for r in ap.runs]))
    bold_runs = [r for r in ap.runs if r.bold]
    check("run级：改写处继承加粗格式",
          len(bold_runs) >= 1 and any("已改写" in r.text for r in bold_runs),
          repr([(r.text, r.bold) for r in ap.runs]))

    # ---------- 10. 未变段落零改动 ----------
    zdoc = Document()
    zdoc.add_paragraph("第一段保持不变。")
    zp = zdoc.add_paragraph()
    zr = zp.add_run("第二段会被修改")
    zr.bold = True
    zdoc.add_paragraph("第三段也保持不变。")
    zpath = tmp / "zero.docx"
    zdoc.save(str(zpath))
    zd_before = Document(str(zpath))
    z_before = [_para_xml(p) for p in zd_before.paragraphs]
    write_docx(str(zpath), ["第一段保持不变。", "第二段已修改", "第三段也保持不变。"])
    zd_after = Document(str(zpath))
    z_after = [_para_xml(p) for p in zd_after.paragraphs]
    check("零改动：未变段落 XML 逐字节一致",
          z_before[0] == z_after[0] and z_before[2] == z_after[2])
    check("零改动：改动段文本正确",
          zd_after.paragraphs[1].text == "第二段已修改")
    check("零改动：改动段格式保留", zd_after.paragraphs[1].runs[0].bold,
          str(zd_after.paragraphs[1].runs[0].bold))

    # ---------- 11. 脚注引用保留 ----------
    fn_path = tmp / "footnote.docx"
    fdoc2 = Document()
    fp = fdoc2.add_paragraph()
    fp.add_run("正文含脚注")
    fn_r = OxmlElement("w:r")
    fn_ref = OxmlElement("w:footnoteReference")
    fn_ref.set(qn("w:id"), "1")
    fn_r.append(fn_ref)
    fp._p.append(fn_r)
    fp.add_run("以及后续文本。")
    fdoc2.save(str(fn_path))
    write_docx(str(fn_path), ["正文含脚注（已修改）以及后续文本。"])
    fdoc3 = Document(str(fn_path))
    check("脚注：引用 run 保留",
          fdoc3.paragraphs[0]._p.find(f".//{qn('w:footnoteReference')}") is not None)
    check("脚注：文本更新",
          fdoc3.paragraphs[0].text == "正文含脚注（已修改）以及后续文本。",
          fdoc3.paragraphs[0].text)

    # ---------- 12. 超链接保留且不重复 ----------
    hl_path = tmp / "hyperlink.docx"
    hdoc = Document()
    hp = hdoc.add_paragraph()
    hp.add_run("参见 ")
    hl = OxmlElement("w:hyperlink")
    hl.set(qn("w:anchor"), "sec1")
    hl_r = OxmlElement("w:r")
    hl_t = OxmlElement("w:t")
    hl_t.text = "第三章内容"
    hl_r.append(hl_t)
    hl.append(hl_r)
    hp._p.append(hl)
    hp.add_run(" 的说明。")
    hdoc.save(str(hl_path))
    # 只改链接外的文本
    write_docx(str(hl_path), ["参见（见下文） 第三章内容 的说明。"])
    hdoc2 = Document(str(hl_path))
    hp2 = hdoc2.paragraphs[0]
    hl2 = hp2._p.find(qn("w:hyperlink"))
    check("超链接：元素保留", hl2 is not None)
    check("超链接：文本不重复",
          hp2.text == "参见（见下文） 第三章内容 的说明。", repr(hp2.text))
    check("超链接：内部文本不变",
          hl2.find(f".//{qn('w:t')}").text == "第三章内容")

    # ---------- 13. 简单域 fldSimple 保留 ----------
    fs_path = tmp / "fldsimple.docx"
    sdoc = Document()
    sp = sdoc.add_paragraph()
    sp.add_run("日期：")
    fld = OxmlElement("w:fldSimple")
    fld.set(qn("w:instr"), " DATE ")
    fr = OxmlElement("w:r")
    ft = OxmlElement("w:t")
    ft.text = "2026-09-10"
    fr.append(ft)
    fld.append(fr)
    sp._p.append(fld)
    sdoc.save(str(fs_path))
    write_docx(str(fs_path), ["日期（更新）：2026-09-10"])
    sdoc2 = Document(str(fs_path))
    fld2 = sdoc2.paragraphs[0]._p.find(qn("w:fldSimple"))
    check("简单域：元素保留", fld2 is not None)
    check("简单域：指令保留",
          fld2 is not None and fld2.get(qn("w:instr")) == " DATE ")
    check("简单域：整段文本", sdoc2.paragraphs[0].text == "日期（更新）：2026-09-10",
          repr(sdoc2.paragraphs[0].text))

    # ---------- 14. 复杂域（Zotero 引用域形态）保留 ----------
    def _add_complex_field(p_el, instr: str, result: str) -> None:
        r1 = OxmlElement("w:r")
        fc = OxmlElement("w:fldChar")
        fc.set(qn("w:fldCharType"), "begin")
        r1.append(fc)
        r2 = OxmlElement("w:r")
        it = OxmlElement("w:instrText")
        it.set(qn("xml:space"), "preserve")
        it.text = instr
        r2.append(it)
        r3 = OxmlElement("w:r")
        fc2 = OxmlElement("w:fldChar")
        fc2.set(qn("w:fldCharType"), "separate")
        r3.append(fc2)
        r4 = OxmlElement("w:r")
        t4 = OxmlElement("w:t")
        t4.text = result
        r4.append(t4)
        r5 = OxmlElement("w:r")
        fc3 = OxmlElement("w:fldChar")
        fc3.set(qn("w:fldCharType"), "end")
        r5.append(fc3)
        for r in (r1, r2, r3, r4, r5):
            p_el.append(r)

    cf_path = tmp / "complexfield.docx"
    cdoc = Document()
    cp = cdoc.add_paragraph()
    cp.add_run("相关研究见 ")
    _add_complex_field(cp._p, " ADDIN ZOTERO_ITEM ", "(Zhang, 2025)")
    cp.add_run(" 的综述。")
    cdoc.save(str(cf_path))
    # 只改域外文本：域五件套应原样保留
    write_docx(str(cf_path), ["相关研究（综述部分）见 (Zhang, 2025) 的综述。"])
    cdoc2 = Document(str(cf_path))
    cp2 = cdoc2.paragraphs[0]
    check("复杂域：instrText 保留",
          cp2._p.find(f".//{qn('w:instrText')}") is not None)
    check("复杂域：域字符完整",
          len(cp2._p.findall(f".//{qn('w:fldChar')}")) == 3,
          str(len(cp2._p.findall(f".//{qn('w:fldChar')}"))))
    check("复杂域：整段文本",
          cp2.text == "相关研究（综述部分）见 (Zhang, 2025) 的综述。", repr(cp2.text))

    # ---------- 15. 段落对齐：中间插入继承样式 ----------
    from docx.shared import Pt as _Pt
    al_path = tmp / "align.docx"
    adoc2 = Document()
    adoc2.add_paragraph("标题段", style="Heading 1")
    adoc2.add_paragraph("正文一。")
    adoc2.add_paragraph("正文二。")
    adoc2.save(str(al_path))
    write_docx(str(al_path), ["标题段", "新插入的段落。", "正文一。", "正文二。"])
    adoc3 = Document(str(al_path))
    check("对齐：段落数", len(adoc3.paragraphs) == 4, str(len(adoc3.paragraphs)))
    check("对齐：新段继承前段样式",
          adoc3.paragraphs[1].style.name == "Heading 1",
          adoc3.paragraphs[1].style.name)
    check("对齐：原段样式不错位",
          [p.style.name for p in adoc3.paragraphs]
          == ["Heading 1", "Heading 1", "Normal", "Normal"],
          str([p.style.name for p in adoc3.paragraphs]))

    # 删除中间段
    write_docx(str(al_path), ["标题段", "正文二。"])
    adoc4 = Document(str(al_path))
    check("对齐：删除中间段",
          [p.text for p in adoc4.paragraphs] == ["标题段", "正文二。"],
          str([p.text for p in adoc4.paragraphs]))

    # ---------- 16. 修订模式（track changes）写回 ----------
    # 注：w:ins 包裹的 run 不在 w:p 直接子级，python-docx 的 .text 看不到；
    # 断言一律走 read_docx（与应用同口径，全树遍历）或直接查 XML。
    tc_path = tmp / "track.docx"
    tdoc = Document()
    tdoc.add_paragraph("AAA BBB CCC")
    tdoc.add_paragraph("未涉及的段落。")
    tdoc.save(str(tc_path))
    write_docx(str(tc_path), ["AAA XXX CCC", "未涉及的段落。"],
               track_changes=True)
    tdoc2 = Document(str(tc_path))
    tp0 = tdoc2.paragraphs[0]
    check("修订：w:ins 存在", tp0._p.find(qn("w:ins")) is not None)
    check("修订：w:del 存在", tp0._p.find(qn("w:del")) is not None)
    ins_el = tp0._p.find(qn("w:ins"))
    check("修订：作者标记",
          ins_el is not None and ins_el.get(qn("w:author")) == "PaperWB",
          str(ins_el.get(qn("w:author")) if ins_el is not None else None))
    del_el = tp0._p.find(qn("w:del"))
    check("修订：delText 转换",
          del_el is not None and del_el.find(f".//{qn('w:delText')}") is not None)
    tcontent = read_docx(str(tc_path))
    check("修订：合并读回文本",
          tcontent.paragraphs[0] == "AAA XXX CCC",
          tcontent.paragraphs[0])
    check("修订：has_revisions", tcontent.has_revisions)

    # 修订模式：未变段落零改动（无修订标记）
    tp1 = tdoc2.paragraphs[1]
    check("修订：未变段落无修订标记",
          tp1._p.find(qn("w:ins")) is None and tp1._p.find(qn("w:del")) is None)

    # 修订模式：删除段落 = 段落标记删除（元素保留）
    td_path = tmp / "track_del.docx"
    tddoc = Document()
    tddoc.add_paragraph("段落甲。")
    tddoc.add_paragraph("段落乙。")
    tddoc.add_paragraph("段落丙。")
    tddoc.save(str(td_path))
    write_docx(str(td_path), ["段落甲。", "段落丙。"], track_changes=True)
    tddoc2 = Document(str(td_path))
    check("修订删段：段落元素保留", len(tddoc2.paragraphs) == 3,
          str(len(tddoc2.paragraphs)))
    mid = tddoc2.paragraphs[1]
    check("修订删段：内容为 w:del/delText",
          mid._p.find(qn("w:del")) is not None
          and mid._p.find(f".//{qn('w:delText')}") is not None)
    check("修订删段：段落标记删除",
          mid._p.find(f".//{qn('w:pPr')}/{qn('w:rPr')}/{qn('w:del')}") is not None)
    td_read = read_docx(str(td_path))
    check("修订删段：合并读回",
          td_read.paragraphs == ["段落甲。", "", "段落丙。"],
          str(td_read.paragraphs))

    # 修订模式：新增段落 = 插入修订
    write_docx(str(td_path), ["段落甲。", "新插入段。", "", "段落丙。"],
               track_changes=True)
    td_read2 = read_docx(str(td_path))
    check("修订增段：文本可读",
          td_read2.paragraphs[:2] == ["段落甲。", "新插入段。"],
          str(td_read2.paragraphs))
    tddoc3 = Document(str(td_path))
    new_p = tddoc3.paragraphs[1]
    check("修订增段：段落标记插入",
          new_p._p.find(f".//{qn('w:ins')}") is not None)

    # 修订模式：域结果区改动 → 整段 del+ins 兜底（不产生非法嵌套）
    write_docx(str(cf_path), ["相关研究见 (Zhang et al., 2026) 的综述。"],
               track_changes=True)
    cf_read = read_docx(str(cf_path))
    check("修订域兜底：合并读回",
          cf_read.paragraphs[0] == "相关研究见 (Zhang et al., 2026) 的综述。",
          repr(cf_read.paragraphs))
    cdoc3 = Document(str(cf_path))
    check("修订域兜底：整段为修订",
          cdoc3.paragraphs[0]._p.find(qn("w:del")) is not None
          and cdoc3.paragraphs[0]._p.find(qn("w:ins")) is not None)

    # ---------- 17. has_unsaved_changes ----------
    hu_path = tmp / "unsaved.docx"
    hudoc = Document()
    hudoc.add_paragraph("一致性检查文本。")
    hudoc.save(str(hu_path))
    check("unsaved：一致", not has_unsaved_changes(str(hu_path), "一致性检查文本。"))
    check("unsaved：不一致", has_unsaved_changes(str(hu_path), "别的文本"))

    # ==================================================
    # 18. Word 交互：config 持久化 + WritingPanel 离屏验证
    # ==================================================
    import os as _os
    _os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QMessageBox
    qapp = QApplication.instance() or QApplication([])

    import src.utils.config as cfg_mod
    _cfg_store: dict = {}

    def _fake_load_config():
        return dict(_cfg_store)

    def _fake_save_config(c):
        _cfg_store.clear()
        _cfg_store.update(c)

    cfg_mod.load_config = _fake_load_config
    cfg_mod.save_config = _fake_save_config
    backup_dir = tmp / "docx_backup"

    def _fake_backup_dir():
        backup_dir.mkdir(parents=True, exist_ok=True)
        return backup_dir

    cfg_mod.get_docx_backup_dir = _fake_backup_dir

    from src.utils.config import (
        get_recent_word_files, push_recent_word_file,
        get_word_binding, set_word_binding,
        get_word_track_changes, set_word_track_changes,
    )

    push_recent_word_file("D:/a.docx")
    push_recent_word_file("D:/b.docx")
    push_recent_word_file("D:/a.docx")
    check("交互：最近文件置顶去重",
          get_recent_word_files() == ["D:/a.docx", "D:/b.docx"],
          str(get_recent_word_files()))
    set_word_binding("库A", "D:/thesis.docx", 123.0)
    check("交互：绑定记忆读写",
          get_word_binding("库A") == {"path": "D:/thesis.docx", "mtime": 123.0},
          str(get_word_binding("库A")))
    set_word_binding("库A", "")
    check("交互：绑定清除", get_word_binding("库A") is None)
    set_word_track_changes(True)
    check("交互：修订写回偏好", get_word_track_changes() is True)
    set_word_track_changes(False)

    # WritingPanel 离屏构造（弹窗一律回 No，避免阻塞）
    QMessageBox.question = staticmethod(
        lambda *a, **k: QMessageBox.StandardButton.No)
    from src.ui.writing_panel import WritingPanel
    panel = WritingPanel()
    if "测试库" not in panel._coach.profile_names:
        panel._coach.create_profile("测试库")
    panel._coach.switch_profile("测试库")
    check("交互：新工具栏控件存在",
          all(hasattr(panel, x) for x in (
              "_recent_word_btn", "_save_word_as_btn", "_open_in_word_btn",
              "_track_changes_cb")))
    check("交互：未绑定时按钮禁用",
          not panel._save_word_btn.isEnabled()
          and not panel._open_in_word_btn.isEnabled())

    # _load_word_file 全链路
    panel._load_word_file(str(run_path))  # 复用第 9 节的 runs.docx
    check("交互：打开后编辑器载入",
          panel.editor.toPlainText() == "AAA开头 BBB已改写 CCC结尾。",
          panel.editor.toPlainText())
    check("交互：打开后按钮启用",
          panel._save_word_btn.isEnabled()
          and panel._open_in_word_btn.isEnabled())
    check("交互：打开后 mtime 记录", panel._word_mtime > 0)
    check("交互：打开后绑定记忆",
          (get_word_binding(panel._coach.current_profile.name) or {}).get("path")
          == str(run_path),
          str(get_word_binding(panel._coach.current_profile.name)))
    check("交互：打开后进最近列表",
          str(run_path) in get_recent_word_files())

    panel._rebuild_recent_menu()
    check("交互：最近菜单填充",
          len(panel._recent_word_btn.menu().actions()) >= 1)

    backup = panel._backup_word_file(str(run_path))
    check("交互：自动备份生成", backup and _os.path.exists(backup), backup)

    # 外部修改检测：disk mtime 改动 → 询问（回 No=继续编辑并更新 mtime）
    _os.utime(str(run_path), (panel._word_mtime + 100,) * 2)
    ok = panel._check_external_word_change()
    check("交互：外部修改检测继续编辑", ok and panel._word_mtime > 0)

    # 拖拽处理：txt 载入为纯文本草稿
    drop_txt = tmp / "dropped.md"
    drop_txt.write_text("# 拖入的文本", encoding="utf-8")
    panel._handle_dropped_files([str(drop_txt)])
    check("交互：拖入 md 载入草稿", panel.editor.toPlainText() == "# 拖入的文本",
          panel.editor.toPlainText())
    check("交互：拖入后不绑定 Word", panel._word_path != str(drop_txt))

finally:
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

print()
for name in PASS:
    print(f"[PASS] {name}")
for name in FAIL:
    print(f"[FAIL] {name}")
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
