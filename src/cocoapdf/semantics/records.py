from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple


_LIST_MARKER_RE = re.compile(
    r"^( *)(-|\d+\.|[A-Za-z]+\.)(?:\s+\[([ xX])\])?\s+(.*)$"
)


def semantic_block_record(kind: str, generated: str) -> Dict[str, Any]:
    """Snapshot format-neutral analyzer evidence beside projection text.

    ``generated`` is still retained independently by ``BlockEvent`` for the
    mature Markdown projection. These records prevent semantic graph and HTML
    construction from reparsing a mutable whole-document Markdown result.
    """
    record: Dict[str, Any] = {}
    if kind == "list":
        record["list_records"] = canonical_list_records(generated)
    elif kind == "quote":
        record["quote_records"] = canonical_quote_records(generated)
    if kind == "table":
        record["table_alignments"] = gfm_table_alignments(generated)
    if kind in {
        "columns",
        "form_appearance",
        "callout",
        "equation",
        "table",
    }:
        record["generated_html"] = generated
    return record


def canonical_list_records(markup: str) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for raw_line in str(markup or "").splitlines():
        if not raw_line.strip():
            continue
        match = _LIST_MARKER_RE.match(raw_line.expandtabs(4))
        if match is None:
            records.append(
                {
                    "kind": "continuation",
                    "content": raw_line.strip(),
                }
            )
            continue
        marker_text = match.group(2)[:-1] if match.group(2) != "-" else "-"
        marker: Any = (
            int(marker_text)
            if marker_text.isdigit()
            else marker_text
        )
        records.append(
            {
                "kind": "item",
                "indent": len(match.group(1)),
                "ordered": match.group(2) != "-",
                "marker": marker,
                "task": match.group(3) is not None,
                "checked": bool(
                    match.group(3)
                    and match.group(3).lower() == "x"
                ),
                "content": match.group(4).strip(),
            }
        )
    return records


def canonical_quote_records(markup: str) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for raw_line in str(markup or "").splitlines():
        split = _split_quote_prefix(raw_line)
        if split is None:
            return []
        depth, content = split
        records.append(
            {
                "depth": depth,
                "content": content,
                "fence": bool(
                    re.match(r"^(`{3,}|~{3,})", content.strip())
                ),
            }
        )
    return records


def gfm_table_alignments(markup: str) -> List[str]:
    for raw_line in str(markup or "").splitlines():
        line = raw_line.strip()
        if not (line.startswith("|") and line.endswith("|")):
            continue
        cells = [cell.strip() for cell in line[1:-1].split("|")]
        if not cells or not all(
            re.fullmatch(r":?-{3,}:?", cell) for cell in cells
        ):
            continue
        return [
            "center"
            if cell.startswith(":") and cell.endswith(":")
            else "right"
            if cell.endswith(":")
            else "left"
            if cell.startswith(":")
            else ""
            for cell in cells
        ]
    return []


def _split_quote_prefix(line: str) -> Optional[Tuple[int, str]]:
    position = 0
    depth = 0
    while position < len(line) and line[position] == ">":
        depth += 1
        position += 1
        if position < len(line) and line[position] == " ":
            position += 1
    if depth == 0:
        return None
    return depth, line[position:]
