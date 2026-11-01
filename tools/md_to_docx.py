#!/usr/bin/env python3
"""把 Markdown 手册转成 Word（.docx）。

支持本项目手册用到的语法子集：标题(#/##/###/####)、段落、列表(- / 1.)、
表格(| … |)、代码块(```)、引用(>)、水平线(---)、行内 **粗体** / `代码` / [链接](url)。

用法：
    python tools/md_to_docx.py --out-dir .. 用户手册.md ../管理员手册.md

依赖：python-docx（仅生成手册时需要，运行主程序不需要）
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

CJK_FONT = "微软雅黑"
MONO_FONT = "Consolas"
INLINE_RE = re.compile(r"(\*\*.+?\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\))")


def set_run_font(run, font: str = CJK_FONT, size: float | None = None, bold: bool | None = None):
    run.font.name = font
    run._element.rPr.rFonts.set(qn("w:eastAsia"), font)
    if size:
        run.font.size = Pt(size)
    if bold is not None:
        run.font.bold = bold


def add_hyperlink(paragraph, url: str, text: str):
    """在段落中插入可点击超链接。"""
    part = paragraph.part
    r_id = part.relate_to(url, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
                          is_external=True)
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), r_id)
    new_run = OxmlElement("w:r")
    r_pr = OxmlElement("w:rPr")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "0563C1")
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    r_pr.append(color)
    r_pr.append(underline)
    new_run.append(r_pr)
    t = OxmlElement("w:t")
    t.text = text
    new_run.append(t)
    hyperlink.append(new_run)
    paragraph._p.append(hyperlink)


def add_inline(paragraph, text: str, size: float = 10.5, base_bold: bool = False, color: RGBColor | None = None):
    """解析行内 **粗体**、`代码`、[链接](url) 并写入段落。"""
    for token in INLINE_RE.split(text):
        if not token:
            continue
        if token.startswith("**") and token.endswith("**"):
            run = paragraph.add_run(token[2:-2])
            set_run_font(run, size=size, bold=True)
            if color:
                run.font.color.rgb = color
        elif token.startswith("`") and token.endswith("`"):
            run = paragraph.add_run(token[1:-1])
            set_run_font(run, font=MONO_FONT, size=size - 0.5)
            run.font.color.rgb = RGBColor(0x9C, 0x27, 0x0C)
        elif token.startswith("[") and "](" in token:
            match = re.match(r"\[([^\]]+)\]\(([^)]+)\)", token)
            if match:
                add_hyperlink(paragraph, match.group(2), match.group(1))
            else:
                paragraph.add_run(token)
        else:
            run = paragraph.add_run(token)
            set_run_font(run, size=size, bold=base_bold)
            if color:
                run.font.color.rgb = color


def shade_cell(cell, hex_color: str = "EEF3F9"):
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_color)
    tc_pr.append(shd)


def add_table(doc: Document, rows: list[list[str]]):
    if not rows:
        return
    cols = max(len(r) for r in rows)
    table = doc.add_table(rows=len(rows), cols=cols)
    table.style = "Table Grid"
    table.autofit = True
    for i, row in enumerate(rows):
        for j in range(cols):
            cell = table.cell(i, j)
            text = row[j] if j < len(row) else ""
            cell.text = ""
            paragraph = cell.paragraphs[0]
            clean = re.sub(r"\*\*|`", "", text)
            add_inline(paragraph, clean, size=8.5, base_bold=(i == 0))
            if i == 0:
                shade_cell(cell)
    doc.add_paragraph()


def setup_document(title: str) -> Document:
    doc = Document()
    # 页面设置
    for section in doc.sections:
        section.top_margin = Cm(2.0)
        section.bottom_margin = Cm(2.0)
        section.left_margin = Cm(2.2)
        section.right_margin = Cm(2.2)
    # 正文样式
    normal = doc.styles["Normal"]
    normal.font.name = CJK_FONT
    normal.font.size = Pt(10.5)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), CJK_FONT)
    normal.paragraph_format.space_after = Pt(4)
    normal.paragraph_format.line_spacing = 1.25
    # 标题样式
    for style_name, size in (("Heading 1", 20), ("Heading 2", 15), ("Heading 3", 12.5), ("Heading 4", 11)):
        style = doc.styles[style_name]
        style.font.name = CJK_FONT
        style.font.size = Pt(size)
        style.font.color.rgb = RGBColor(0x1F, 0x3A, 0x5F)
        style.element.rPr.rFonts.set(qn("w:eastAsia"), CJK_FONT)
    # 页脚
    footer = doc.sections[0].footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = footer.add_run(title)
    set_run_font(run, size=8)
    run.font.color.rgb = RGBColor(0x88, 0x88, 0x88)
    doc.core_properties.title = title
    doc.core_properties.author = "鸦岗降雨预报项目"
    return doc


def convert(md_path: Path, out_path: Path):
    lines = md_path.read_text(encoding="utf-8").splitlines()
    title = md_path.stem
    doc = setup_document(title)
    i = 0
    table_rows: list[list[str]] = []
    in_code = False
    code_lines: list[str] = []

    def flush_table():
        nonlocal table_rows
        if table_rows:
            add_table(doc, table_rows)
            table_rows = []

    while i < len(lines):
        line = lines[i].rstrip()
        stripped = line.strip()

        # 代码块
        if stripped.startswith("```"):
            flush_table()
            if in_code:
                para = doc.add_paragraph()
                para.paragraph_format.left_indent = Cm(0.5)
                run = para.add_run("\n".join(code_lines))
                set_run_font(run, font=MONO_FONT, size=8.5)
                code_lines = []
                in_code = False
            else:
                in_code = True
            i += 1
            continue
        if in_code:
            code_lines.append(line)
            i += 1
            continue

        # 表格
        if stripped.startswith("|"):
            if re.match(r"^\|[\s:|-]+\|$", stripped):
                i += 1
                continue
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            table_rows.append(cells)
            i += 1
            continue
        flush_table()

        if not stripped:
            i += 1
            continue
        if re.match(r"^-{3,}$", stripped):
            i += 1
            continue

        # 标题
        heading = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if heading:
            level = len(heading.group(1))
            text = re.sub(r"\*\*", "", heading.group(2)).strip()
            doc.add_heading(text, level=level)
            i += 1
            continue

        # 引用
        if stripped.startswith(">"):
            para = doc.add_paragraph()
            para.paragraph_format.left_indent = Cm(0.6)
            add_inline(para, stripped.lstrip("> ").strip(), size=9.5, color=RGBColor(0x5A, 0x5A, 0x5A))
            i += 1
            continue

        # 列表
        bullet = re.match(r"^(\s*)[-*]\s+(.*)$", line)
        if bullet:
            indent = len(bullet.group(1))
            style = "List Bullet 2" if indent >= 2 else "List Bullet"
            para = doc.add_paragraph(style=style)
            add_inline(para, bullet.group(2))
            i += 1
            continue
        numbered = re.match(r"^(\s*)\d+\.\s+(.*)$", line)
        if numbered:
            indent = len(numbered.group(1))
            style = "List Number 2" if indent >= 2 else "List Number"
            para = doc.add_paragraph(style=style)
            add_inline(para, numbered.group(2))
            i += 1
            continue

        # 普通段落
        para = doc.add_paragraph()
        add_inline(para, stripped)
        i += 1

    flush_table()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(out_path)
    return out_path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Markdown 转 Word（手册专用）")
    parser.add_argument("files", nargs="+", help="Markdown 文件路径")
    parser.add_argument("--out-dir", default=None, help="输出目录（默认与源文件同目录）")
    args = parser.parse_args(argv)
    out_dir = Path(args.out_dir) if args.out_dir else None
    for name in args.files:
        md_path = Path(name)
        if not md_path.exists():
            print(f"✗ 找不到文件：{md_path}", file=sys.stderr)
            return 2
        out = (out_dir or md_path.parent) / (md_path.stem + ".docx")
        convert(md_path, out)
        print(f"✓ 已生成：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
