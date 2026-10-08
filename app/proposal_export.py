"""Export the reviewed proposal response as a paginated Chinese PDF."""

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
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer


FONT_NAME = "STSong-Light"


def _inline(text: str) -> str:
    """Escape model/user text before interpreting only simple Markdown emphasis."""

    value = escape(text)
    value = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", value)
    return value.replace("`", "")


def _styles() -> dict[str, ParagraphStyle]:
    pdfmetrics.registerFont(UnicodeCIDFont(FONT_NAME))
    base = dict(fontName=FONT_NAME, textColor=colors.HexColor("#24313A"),
                wordWrap="CJK", alignment=TA_LEFT, spaceAfter=7)
    return {
        "body": ParagraphStyle("body", fontSize=10, leading=17, **base),
        "title": ParagraphStyle("title", fontSize=19, leading=29, spaceBefore=4,
                                spaceAfter=18, **{k: v for k, v in base.items() if k != "spaceAfter"}),
        "chapter": ParagraphStyle("chapter", fontSize=14, leading=23, spaceBefore=15,
                                  spaceAfter=10, **{k: v for k, v in base.items() if k != "spaceAfter"}),
        "section": ParagraphStyle("section", fontSize=11, leading=19, spaceBefore=11,
                                  spaceAfter=7, **{k: v for k, v in base.items() if k != "spaceAfter"}),
        "quote": ParagraphStyle("quote", fontSize=9, leading=15, leftIndent=15,
                                textColor=colors.HexColor("#52636A"), wordWrap="CJK",
                                fontName=FONT_NAME, spaceAfter=4),
        "bullet": ParagraphStyle("bullet", fontSize=10, leading=17, leftIndent=15,
                                 firstLineIndent=-10, **base),
    }


def _story(markdown: str) -> list[Paragraph | Spacer]:
    """Retain every source line while applying the proposal's heading/list hierarchy."""

    styles = _styles()
    story = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped:
            story.append(Spacer(1, 5))
            continue
        if stripped.startswith("# "):
            kind, content = "title", stripped[2:]
        elif stripped.startswith("## "):
            kind, content = "chapter", stripped[3:]
        elif stripped.startswith(("### ", "#### ")):
            kind, content = "section", stripped.lstrip("# ")
        elif stripped.startswith("> "):
            kind, content = "quote", stripped[2:]
        elif stripped.startswith(("- ", "* ")):
            kind, content = "bullet", "- " + stripped[2:]
        else:
            kind, content = "body", stripped
        story.append(Paragraph(_inline(content), styles[kind]))
    return story


def export_proposal_pdf(markdown: str, destination: str | Path) -> Path:
    """Write the complete user-facing draft; no separate unreviewed facts are added."""

    if not markdown.strip():
        raise ValueError("方案正文不能为空")
    path = Path(destination)
    document = SimpleDocTemplate(
        str(path), pagesize=A4, rightMargin=48, leftMargin=48,
        topMargin=50, bottomMargin=48, title="营销方案初稿",
    )
    document.build(_story(markdown))
    return path
