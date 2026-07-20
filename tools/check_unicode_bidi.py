#!/usr/bin/env python3
"""Run CocoaPDF against the official Unicode bidirectional test corpora.

Unicode publishes two complementary files for UAX #9 conformance:

* ``BidiCharacterTest.txt``: explicit Unicode scalar sequences, including
  paired-bracket cases.
* ``BidiTest.txt``: exhaustive sequences of bidi classes under explicit LTR,
  explicit RTL, and auto paragraph modes.

Examples::

    PYTHONPATH=src python tools/check_unicode_bidi.py BidiCharacterTest.txt
    PYTHONPATH=src python tools/check_unicode_bidi.py BidiTest.txt
    PYTHONPATH=src python tools/check_unicode_bidi.py BidiTest.txt --json out.json

The command exits non-zero on any paragraph-level, resolved-level, or visual
order mismatch.  It does not download the Unicode data and therefore keeps the
runtime dependency-free and reproducible.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Dict, Iterable, Optional, Sequence, Tuple
import unicodedata

from cocoapdf.text.bidi import resolve_text


_TYPE_CHARACTER: Dict[str, str] = {
    "L": "A", "R": "\u05d0", "AL": "\u0627", "EN": "0",
    "ES": "+", "ET": "$", "AN": "\u0660", "CS": ",",
    "NSM": "\u0300", "BN": "\x00", "B": "\u2029", "S": "\t",
    "WS": " ", "ON": "!", "LRE": "\u202a", "LRO": "\u202d",
    "RLE": "\u202b", "RLO": "\u202e", "PDF": "\u202c",
    "LRI": "\u2066", "RLI": "\u2067", "FSI": "\u2068", "PDI": "\u2069",
}
_MODE_BITS = ((1, 0, "ltr"), (2, 1, "rtl"), (4, None, None))


def _parse_levels(value: str) -> Tuple[Optional[int], ...]:
    return tuple(None if item.lower() == "x" else int(item) for item in value.split())


def _character_case(line: str):
    payload = line.split("#", 1)[0].strip()
    if not payload:
        return None
    fields = [field.strip() for field in payload.split(";")]
    if len(fields) < 5:
        raise ValueError("expected five semicolon-separated fields")
    codepoints = tuple(int(item, 16) for item in fields[0].split())
    paragraph = int(fields[1])
    expected_paragraph = int(fields[2])
    levels = _parse_levels(fields[3])
    order = tuple(int(item) for item in fields[4].split())
    return codepoints, paragraph, expected_paragraph, levels, order


def _run_character_tests(lines: Iterable[str], max_failures: int) -> dict:
    total = failures = 0
    examples = []
    for number, raw in enumerate(lines, 1):
        try:
            case = _character_case(raw)
        except Exception as exc:
            failures += 1
            if len(examples) < max_failures:
                examples.append({"line": number, "error": "parse: %s" % exc})
            continue
        if case is None:
            continue
        codepoints, paragraph, expected_paragraph, expected_levels, expected_order = case
        text = "".join(chr(value) for value in codepoints)
        base = "ltr" if paragraph == 0 else "rtl" if paragraph == 1 else None
        result = resolve_text(text, base)
        total += 1
        if not (
            result.paragraph_level == expected_paragraph
            and result.levels == expected_levels
            and result.visual_order == expected_order
        ):
            failures += 1
            if len(examples) < max_failures:
                examples.append({
                    "line": number,
                    "codepoints": ["%04X" % value for value in codepoints],
                    "paragraph_level": [expected_paragraph, result.paragraph_level],
                    "levels": [list(expected_levels), list(result.levels)],
                    "order": [list(expected_order), list(result.visual_order)],
                })
    return {"kind": "BidiCharacterTest", "total": total, "failures": failures, "examples": examples}


def _validate_type_mapping() -> None:
    mismatches = {
        bidi_type: unicodedata.bidirectional(character)
        for bidi_type, character in _TYPE_CHARACTER.items()
        if unicodedata.bidirectional(character) != bidi_type
    }
    if mismatches:
        raise RuntimeError("runtime Unicode database cannot represent bidi classes: %r" % mismatches)


def _run_type_tests(lines: Iterable[str], max_failures: int) -> dict:
    _validate_type_mapping()
    expected_levels: Optional[Tuple[Optional[int], ...]] = None
    expected_order: Optional[Tuple[int, ...]] = None
    total = failures = 0
    examples = []
    for number, raw in enumerate(lines, 1):
        payload = raw.split("#", 1)[0].strip()
        if not payload:
            continue
        if payload.startswith("@Levels:"):
            expected_levels = _parse_levels(payload.partition(":")[2].strip())
            continue
        if payload.startswith("@Reorder:"):
            expected_order = tuple(int(item) for item in payload.partition(":")[2].split())
            continue
        try:
            fields = [field.strip() for field in payload.split(";")]
            if len(fields) != 2:
                raise ValueError("expected bidi-type sequence and paragraph-mode bitset")
            bidi_types = tuple(fields[0].split())
            bitset = int(fields[1])
            if expected_levels is None or expected_order is None:
                raise ValueError("missing @Levels/@Reorder directives")
            text = "".join(_TYPE_CHARACTER[item] for item in bidi_types)
        except Exception as exc:
            failures += 1
            if len(examples) < max_failures:
                examples.append({"line": number, "error": "parse: %s" % exc})
            continue
        for bit, paragraph_level, base in _MODE_BITS:
            if not bitset & bit:
                continue
            result = resolve_text(text, base)
            total += 1
            paragraph_ok = paragraph_level is None or result.paragraph_level == paragraph_level
            if not (paragraph_ok and result.levels == expected_levels and result.visual_order == expected_order):
                failures += 1
                if len(examples) < max_failures:
                    examples.append({
                        "line": number,
                        "types": list(bidi_types),
                        "mode": "auto" if base is None else base,
                        "paragraph_level": [paragraph_level, result.paragraph_level],
                        "levels": [list(expected_levels), list(result.levels)],
                        "order": [list(expected_order), list(result.visual_order)],
                    })
    return {"kind": "BidiTest", "total": total, "failures": failures, "examples": examples}


def run(path: Path, max_failures: int = 25, kind: str = "auto") -> dict:
    lines = path.read_text(encoding="utf-8").splitlines()
    if kind == "auto":
        header = "\n".join(lines[:100])
        kind = "types" if "BidiTest-" in header and "BidiCharacterTest" not in header else "characters"
    if kind == "characters":
        summary = _run_character_tests(lines, max_failures)
    elif kind == "types":
        summary = _run_type_tests(lines, max_failures)
    else:
        raise ValueError("unknown test kind: %s" % kind)
    summary.update({"file": str(path), "unicode_database": unicodedata.unidata_version})
    return summary


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("test_file", type=Path)
    parser.add_argument("--kind", choices=["auto", "characters", "types"], default="auto")
    parser.add_argument("--max-failures", type=int, default=25)
    parser.add_argument("--json", dest="json_path", type=Path)
    args = parser.parse_args(argv)
    summary = run(args.test_file, args.max_failures, args.kind)
    payload = json.dumps(summary, indent=2, ensure_ascii=False) + "\n"
    if args.json_path:
        args.json_path.parent.mkdir(parents=True, exist_ok=True)
        args.json_path.write_text(payload, encoding="utf-8")
    sys.stdout.write(payload)
    return 1 if summary["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
