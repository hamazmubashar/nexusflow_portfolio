"""Small HTML building blocks for the glass UI. Every dynamic value goes through esc():
copilot answers, approval cards and activity summaries contain text that originated in
documents and external requests, so nothing is rendered unescaped."""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import streamlit as st

THEME = Path(__file__).with_name("theme.css")
TONE_FOR_STATUS = {"ok": "ok", "degraded": "wait", "not_configured": "wait", "down": "breach", "unknown": ""}


def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def md(text: str) -> str:
    """Make plain text safe for Streamlit markdown widgets: "$" pairs would otherwise render as LaTeX."""
    return str(text).replace("$", "\\$")


def inject_theme() -> None:
    st.markdown(f"<style>{THEME.read_text(encoding='utf-8')}</style>", unsafe_allow_html=True)


def html_block(markup: str) -> None:
    st.markdown(markup, unsafe_allow_html=True)


def money(value, compact: bool = False) -> str:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return "—"
    if compact and abs(d) >= 1_000_000:
        return f"${d / 1_000_000:,.1f}M"
    if compact and abs(d) >= 10_000:
        return f"${d / 1_000:,.0f}K"
    return f"${d:,.2f}"


def pct(value) -> str:
    return "—" if value is None else f"{value:.1f}%"


def ago(ts: str | None) -> str:
    if not ts:
        return ""
    then = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    secs = (datetime.now(timezone.utc) - then).total_seconds()
    if secs < 60:
        return "just now"
    for size, unit in ((86400, "d"), (3600, "h"), (60, "min")):
        if secs >= size:
            return f"{int(secs // size)} {unit} ago"
    return ""


def badge(name: str, status: str, title: str | None = None) -> str:
    tone = TONE_FOR_STATUS.get(status, "")
    return (f'<span class="nf-badge" title="{esc(title or status)}">'
            f'<span class="nf-dot {tone}"></span>{esc(name)}</span>')


def chip(text: str, tone: str = "") -> str:
    return f'<span class="nf-chip {tone}">{esc(text)}</span>'


def topbar(title: str, subtitle: str, badges_html: str) -> None:
    html_block(f'<div class="nf-topbar"><div><div class="nf-title" role="heading" aria-level="1">{esc(title)}</div>'
               f'<p class="nf-subtitle">{esc(subtitle)}</p></div>'
               f'<div class="nf-badges" role="status" aria-label="System status">{badges_html}</div></div>')


def kpi(label: str, value: str, note_html: str = "", hero: bool = False, attention: bool = False,
        status: bool = False) -> str:
    classes = ("nf-glass " + ("nf-kpi-hero" if hero else "nf-kpi") + (" nf-attention" if attention else "")
               + (" nf-kpi-status" if status else ""))
    return (f'<div class="{classes}"><div class="nf-kpi-label">{esc(label)}</div>'
            f'<div class="nf-kpi-value">{esc(value)}</div><div class="nf-kpi-note">{note_html}</div></div>')


def rate_row(label: str, value: float | None, detail: str = "") -> str:
    width = 0 if value is None else max(0.0, min(100.0, value))
    return (f'<div class="nf-rate"><span class="nf-rate-label">{esc(label)}'
            f'{", " + esc(detail) if detail else ""}</span><span class="nf-rate-value">{esc(pct(value))}</span>'
            f'<div class="nf-bar" role="img" aria-label="{esc(label)} {esc(pct(value))}">'
            f'<span style="width:{width:.1f}%"></span></div></div>')


def facts(items: list[tuple[str, str]]) -> str:
    cells = "".join(f'<div><div class="nf-fact-label">{esc(k)}</div><div class="nf-fact-value">{esc(v)}</div></div>'
                    for k, v in items)
    return f'<div class="nf-facts">{cells}</div>'


_MARKER = re.compile(r"\[(S\d+)\]")


def answer_html(text: str) -> str:
    """Escape the answer, then turn [S1] markers into small citation chips."""
    return _MARKER.sub(lambda m: f'<span class="nf-cite-ref">{m.group(1)}</span>', esc(text))


def empty(title: str, body: str) -> None:
    html_block(f'<div class="nf-empty"><strong>{esc(title)}</strong><br>{esc(body)}</div>')
