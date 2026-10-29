"""Structure-aware markdown chunking for the NexusFlow document corpus.

Splitting strategy, in order of preference:
  1. `## ` headings            (SOPs, MSA, SOW)
  2. `---` message separators  (email threads, notices)
  3. paragraphs                (chat transcripts: one timestamped message each)
Any piece over MAX_CHARS is re-packed by paragraph, then by line, so tables
and numbered procedures stay whole wherever they fit.

The document preamble (title block: owner, linked customer/contract/ticket,
status) becomes chunk 0, and every chunk carries a short context header with
doc ID, title, section, path and the document's entity IDs, so a chunk
about "the procedure" still matches a query about "SOP-01" or "CUST-0040".
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

CHUNKER_VERSION = "1"
MAX_CHARS = 1500

ENTITY_RE = re.compile(
    r"\b(?:CUST|CTR|TK|EMP|PAY|AST|PRD|DPT)-\d{4}\b|\bINV-20\d{2}-\d{4}\b|\bSOP-\d{2}\b"
)
SOP_ID_RE = re.compile(r"^(SOP-\d{2})\b")
# The classification line is identical in every file and adds nothing to retrieval
BOILERPLATE_RE = re.compile(r"^\*\*Document classification:\*\*.*$", re.M)

# SOPs and contracts govern; email/chat are scenario narrative (see SOP-01 §5, SOP-02 §5)
AUTHORITY = {"sops": "governing", "legal": "governing", "correspondence": "narrative", "internal_ops": "narrative"}


@dataclass
class Chunk:
    source_file: str
    doc_id: str
    doc_type: str
    title: str
    section: str
    chunk_index: int
    content: str
    metadata: dict = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode()).hexdigest()


def extract_entities(text: str) -> list[str]:
    return sorted(set(ENTITY_RE.findall(text)))


def _pack(text: str, max_chars: int = MAX_CHARS) -> list[str]:
    """Greedy-pack paragraphs (then lines, for oversized paragraphs) into <= max_chars pieces."""
    units: list[str] = []
    for para in re.split(r"\n\s*\n", text.strip()):
        if len(para) <= max_chars:
            units.append(para)
        else:
            units.extend(line for line in para.splitlines() if line.strip())

    pieces, current = [], ""
    for unit in units:
        candidate = f"{current}\n\n{unit}" if current else unit
        if len(candidate) <= max_chars or not current:
            current = candidate
        else:
            pieces.append(current)
            current = unit
    if current:
        pieces.append(current)
    return pieces


def _email_label(block: str) -> str:
    sender = re.search(r"\*\*From:\*\*\s*(\S+)", block)
    date = re.search(r"\*\*Date:\*\*\s*([^\n]+?)\s*$", block, re.M)
    if sender or date:
        return " ".join(p for p in ("Message from " + sender.group(1) if sender else "",
                                     date.group(1) if date else "") if p)
    return "Message"


def split_sections(body: str) -> list[tuple[str, str]]:
    """Return (section_label, text) pairs for the document body (after the preamble)."""
    if re.search(r"^## ", body, re.M):
        parts = re.split(r"^## (.+)$", body, flags=re.M)
        # parts = [before, heading1, text1, heading2, text2, ...]
        sections = [(h.strip(), t.strip()) for h, t in zip(parts[1::2], parts[2::2])]
        if parts[0].strip():
            sections.insert(0, ("Introduction", parts[0].strip()))
        return sections
    if re.search(r"^---\s*$", body, re.M):
        blocks = [b.strip() for b in re.split(r"^---\s*$", body, flags=re.M) if b.strip()]
        return [(_email_label(b), b) for b in blocks]
    is_chat = re.search(r"^\*\*\d{2}:\d{2} UTC", body, re.M)
    return [("Transcript" if is_chat else "Body", body.strip())]


def chunk_document(path: Path, root: Path) -> list[Chunk]:
    text = path.read_text(encoding="utf-8")
    rel = path.relative_to(root).as_posix()
    doc_type = path.relative_to(root).parts[0] if len(path.relative_to(root).parts) > 1 else "general"

    title_match = re.search(r"^# (.+)$", text, re.M)
    title = title_match.group(1).strip() if title_match else path.stem
    sop = SOP_ID_RE.match(title)
    doc_id = sop.group(1) if sop else path.stem

    after_title = text[title_match.end():] if title_match else text
    after_title = BOILERPLATE_RE.sub("", after_title)
    # Preamble = everything before the first section heading or message separator
    split_at = re.search(r"^(## |---\s*$)", after_title, re.M)
    if split_at:
        preamble, body = after_title[: split_at.start()], after_title[split_at.start():]
    else:
        # Chat transcript: first paragraph(s) up to the first timestamped message
        msg = re.search(r"^\*\*\d{2}:\d{2} UTC", after_title, re.M)
        preamble, body = (after_title[: msg.start()], after_title[msg.start():]) if msg else ("", after_title)

    doc_entities = extract_entities(text)
    sections: list[tuple[str, str]] = []
    if preamble.strip():
        sections.append(("Overview", preamble.strip()))
    sections.extend(split_sections(body))

    chunks: list[Chunk] = []
    for label, section_text in sections:
        pieces = _pack(section_text)
        for n, piece in enumerate(pieces, 1):
            section = label if len(pieces) == 1 else f"{label} (part {n}/{len(pieces)})"
            header = (
                f"[{doc_id}] {title} — {section}\n"
                f"Source: {rel} | Type: {doc_type} ({AUTHORITY.get(doc_type, 'reference')})"
                + (f" | Document entities: {', '.join(doc_entities)}" if doc_entities else "")
            )
            chunks.append(Chunk(
                source_file=rel, doc_id=doc_id, doc_type=doc_type, title=title, section=section,
                chunk_index=len(chunks), content=f"{header}\n\n{piece}",
                metadata={
                    "authority": AUTHORITY.get(doc_type, "reference"),
                    "entities": extract_entities(piece),
                    "doc_entities": doc_entities,
                    "char_count": len(piece),
                },
            ))
    return chunks


def iter_documents(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.md") if not any(part.startswith(".") for part in p.parts))
