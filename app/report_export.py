"""将售中周报和售后总结的 Markdown 导出为中文 PDF。"""

from __future__ import annotations

from html import escape
from pathlib import Path
import re

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


FONT_NAME = "STSong-Light"


def _inline(text: str) -> str:
    value = escape(text)
    value = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", value)
    return value.replace(chr(96), "")


def _styles() -> dict[str, ParagraphStyle]:
    pdfmetrics.registerFont(UnicodeCIDFont(FONT_NAME))
    common = dict(fontName=FONT_NAME, textColor=colors.HexColor("#24313A"), wordWrap="CJK", alignment=TA_LEFT)
    return {
        "body": ParagraphStyle("ops-body", fontSize=9.5, leading=16, spaceAfter=6, **common),
        "title": ParagraphStyle("ops-title", fontSize=18, leading=27, spaceAfter=16, **common),
        "chapter": ParagraphStyle("ops-chapter", fontSize=13, leading=21, spaceBefore=13, spaceAfter=8, **common),
        "section": ParagraphStyle("ops-section", fontSize=11, leading=18, spaceBefore=9, spaceAfter=6, **common),
        "bullet": ParagraphStyle("ops-bullet", fontSize=9.5, leading=16, leftIndent=14, firstLineIndent=-9, spaceAfter=4, **common),
    }


def _table(lines: list[str], styles: dict[str, ParagraphStyle]) -> Table:
    rows = []
    for line in lines:
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        rows.append([Paragraph(_inline(cell), styles["body"]) for cell in cells])
    table = Table(rows, repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), FONT_NAME),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EEF3")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#AAB7C0")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return table


def _story(markdown: str) -> list:
    styles = _styles()
    story: list = []
    lines = markdown.splitlines()
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped.startswith("|") and index + 1 < len(lines) and lines[index + 1].strip().startswith("|---"):
            table_lines = [stripped]
            index += 2
            while index < len(lines) and lines[index].strip().startswith("|"):
                table_lines.append(lines[index].strip())
                index += 1
            story.append(_table(table_lines, styles))
            story.append(Spacer(1, 7))
            continue
        if not stripped:
            story.append(Spacer(1, 4))
        elif stripped.startswith("# "):
            story.append(Paragraph(_inline(stripped[2:]), styles["title"]))
        elif stripped.startswith("## "):
            story.append(Paragraph(_inline(stripped[3:]), styles["chapter"]))
        elif stripped.startswith(("### ", "#### ")):
            story.append(Paragraph(_inline(stripped.lstrip("# ")), styles["section"]))
        elif stripped.startswith(("- ", "* ")):
            story.append(Paragraph("- " + _inline(stripped[2:]), styles["bullet"]))
        else:
            story.append(Paragraph(_inline(stripped), styles["body"]))
        index += 1
    return story


def export_report_pdf(markdown: str, destination: str | Path, *, title: str) -> Path:
    """导出完整报告；不在导出阶段增加任何业务事实。"""

    if not isinstance(markdown, str) or not markdown.strip():
        raise ValueError("报告正文不能为空。")
    path = Path(destination)
    document = SimpleDocTemplate(
        str(path), pagesize=A4, rightMargin=36, leftMargin=36,
        topMargin=42, bottomMargin=42, title=title,
    )
    document.build(_story(markdown))
    return path
