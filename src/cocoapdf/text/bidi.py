from __future__ import annotations

"""A deterministic, token-preserving Unicode bidirectional implementation.

The implementation follows the paragraph, explicit-level, weak, neutral,
implicit, and line-reordering phases of UAX #9.  It deliberately operates on
Unicode scalar values after PDF font decoding; it never guesses missing
Unicode.  Token metadata is preserved by splitting tokens into per-character
atoms and coalescing adjacent atoms after visual reordering.

Python's bundled ``unicodedata`` database determines the supported Unicode
version.  The implementation supports embeddings, overrides, isolates,
European/Arabic numbers, neutrals, paired brackets, and mirroring metadata.
"""

from dataclasses import dataclass
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# Normative Bidi_Paired_Bracket data from Unicode 17.0.0.  This is the
# exact opening-to-closing relation used by UAX #9 rule N0; mirroring pairs
# that are not paired brackets intentionally do not belong here. Distributed
# under Unicode-3.0; see THIRD_PARTY_NOTICES.txt.
BIDI_BRACKET_DATA_VERSION = "17.0.0"
_BRACKET_PAIRS = {
    chr(opening): chr(closing)
    for opening, closing in (
        (0x0028, 0x0029), (0x005B, 0x005D), (0x007B, 0x007D),
        (0x0F3A, 0x0F3B), (0x0F3C, 0x0F3D), (0x169B, 0x169C),
        (0x2045, 0x2046), (0x207D, 0x207E), (0x208D, 0x208E),
        (0x2308, 0x2309), (0x230A, 0x230B), (0x2329, 0x232A),
        (0x2768, 0x2769), (0x276A, 0x276B), (0x276C, 0x276D),
        (0x276E, 0x276F), (0x2770, 0x2771), (0x2772, 0x2773),
        (0x2774, 0x2775), (0x27C5, 0x27C6), (0x27E6, 0x27E7),
        (0x27E8, 0x27E9), (0x27EA, 0x27EB), (0x27EC, 0x27ED),
        (0x27EE, 0x27EF), (0x2983, 0x2984), (0x2985, 0x2986),
        (0x2987, 0x2988), (0x2989, 0x298A), (0x298B, 0x298C),
        (0x298D, 0x2990), (0x298F, 0x298E), (0x2991, 0x2992),
        (0x2993, 0x2994), (0x2995, 0x2996), (0x2997, 0x2998),
        (0x29D8, 0x29D9), (0x29DA, 0x29DB), (0x29FC, 0x29FD),
        (0x2E22, 0x2E23), (0x2E24, 0x2E25), (0x2E26, 0x2E27),
        (0x2E28, 0x2E29), (0x2E55, 0x2E56), (0x2E57, 0x2E58),
        (0x2E59, 0x2E5A), (0x2E5B, 0x2E5C), (0x3008, 0x3009),
        (0x300A, 0x300B), (0x300C, 0x300D), (0x300E, 0x300F),
        (0x3010, 0x3011), (0x3014, 0x3015), (0x3016, 0x3017),
        (0x3018, 0x3019), (0x301A, 0x301B), (0xFE59, 0xFE5A),
        (0xFE5B, 0xFE5C), (0xFE5D, 0xFE5E), (0xFF08, 0xFF09),
        (0xFF3B, 0xFF3D), (0xFF5B, 0xFF5D), (0xFF5F, 0xFF60),
        (0xFF62, 0xFF63),
    )
}

_BRACKET_CLOSE = {close: open_ for open_, close in _BRACKET_PAIRS.items()}

_EXPLICIT = {"RLE", "LRE", "RLO", "LRO", "PDF", "RLI", "LRI", "FSI", "PDI"}
_ISOLATE_INIT = {"RLI", "LRI", "FSI"}
_NEUTRALS = {"B", "S", "WS", "ON", "FSI", "LRI", "RLI", "PDI"}
_STRONG = {"L", "R", "AL"}


@dataclass
class _Atom:
    char: str
    token: Dict[str, Any]
    original_index: int
    bidi_type: str
    original_type: str
    level: int = 0
    removed: bool = False


@dataclass(frozen=True)
class BidiResult:
    """Resolved bidi state for conformance tests and provenance reports.

    ``levels`` is indexed by the original Unicode scalar position; X9-removed
    controls are represented by ``None``. ``visual_order`` contains original
    scalar indices after L2 reordering.
    """

    paragraph_level: int
    levels: Tuple[Optional[int], ...]
    visual_order: Tuple[int, ...]
    tokens: Tuple[Dict[str, Any], ...]

    @property
    def text(self) -> str:
        return "".join(str(token.get("text", "")) for token in self.tokens)


def resolve_text(text: str, base_direction: Optional[str] = None) -> BidiResult:
    return resolve_tokens([{"text": text}], base_direction)


def resolve_tokens(tokens: Sequence[Dict[str, Any]], base_direction: Optional[str] = None) -> BidiResult:
    atoms: List[_Atom] = []
    index = 0
    for token in tokens:
        for character in str(token.get("text", "")):
            bidi_type = _bidi_type(character)
            atoms.append(_Atom(character, dict(token), index, bidi_type, bidi_type))
            index += 1
    if not atoms:
        return BidiResult(0, (), (), ())
    paragraph_level = _paragraph_level(atoms, base_direction)
    _resolve_explicit(atoms, paragraph_level)
    visible = [atom for atom in atoms if not atom.removed]
    if visible:
        runs = _isolating_run_sequences(visible)
        for run in runs:
            sor, eor = _sor_eor(run, visible, paragraph_level)
            _resolve_weak(run, sor, eor)
            _resolve_brackets(run, sor)
            _resolve_neutral(run, sor, eor)
            _resolve_implicit(run)
        _reset_whitespace_levels(visible, paragraph_level)
        ordered = _reorder_by_levels(visible)
    else:
        ordered = []
    levels: List[Optional[int]] = [None] * len(atoms)
    for atom in visible:
        levels[atom.original_index] = atom.level
    return BidiResult(
        paragraph_level=paragraph_level,
        levels=tuple(levels),
        visual_order=tuple(atom.original_index for atom in ordered),
        tokens=tuple(_coalesce_atoms(ordered)),
    )


def reorder_text(text: str, base_direction: Optional[str] = None) -> str:
    return resolve_text(text, base_direction).text


def reorder_tokens(tokens: Sequence[Dict[str, Any]], base_direction: Optional[str] = None) -> List[Dict[str, Any]]:
    return list(resolve_tokens(tokens, base_direction).tokens)


def _bidi_type(character: str) -> str:
    value = unicodedata.bidirectional(character)
    return value or "L"


def _paragraph_level(atoms: Sequence[_Atom], requested: Optional[str]) -> int:
    if requested:
        value = requested.lower()
        if value in {"rtl", "r", "1"}:
            return 1
        if value in {"ltr", "l", "0"}:
            return 0
    isolate_depth = 0
    for atom in atoms:
        typ = atom.bidi_type
        if typ in _ISOLATE_INIT:
            isolate_depth += 1
            continue
        if typ == "PDI" and isolate_depth:
            isolate_depth -= 1
            continue
        if isolate_depth:
            continue
        if typ == "L":
            return 0
        if typ in {"R", "AL"}:
            return 1
    return 0


def _least_greater_even(level: int) -> int:
    return level + 2 if level % 2 == 0 else level + 1


def _least_greater_odd(level: int) -> int:
    return level + 1 if level % 2 == 0 else level + 2


def _fsi_direction(atoms: Sequence[_Atom], start: int) -> str:
    depth = 0
    for atom in atoms[start + 1 :]:
        typ = atom.bidi_type
        if typ in _ISOLATE_INIT:
            depth += 1
        elif typ == "PDI":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and typ == "L":
            return "LRI"
        elif depth == 0 and typ in {"R", "AL"}:
            return "RLI"
    return "LRI"


def _resolve_explicit(atoms: List[_Atom], paragraph_level: int) -> None:
    # stack item: (embedding level, override, is_isolate)
    stack: List[Tuple[int, Optional[str], bool]] = [(paragraph_level, None, False)]
    overflow_isolate = 0
    overflow_embedding = 0
    valid_isolate = 0
    for index, atom in enumerate(atoms):
        typ = atom.bidi_type
        current_level, override, _ = stack[-1]
        atom.level = current_level
        if typ == "FSI":
            typ = _fsi_direction(atoms, index)
            atom.bidi_type = typ
        if typ in {"RLE", "LRE", "RLO", "LRO", "RLI", "LRI"}:
            isolate = typ in {"RLI", "LRI"}
            new_level = _least_greater_odd(current_level) if typ in {"RLE", "RLO", "RLI"} else _least_greater_even(current_level)
            if isolate:
                atom.bidi_type = override if override in {"L", "R"} else typ
            else:
                atom.removed = True
            if new_level <= 125 and overflow_isolate == 0 and overflow_embedding == 0:
                new_override = "R" if typ == "RLO" else "L" if typ == "LRO" else None
                stack.append((new_level, new_override, isolate))
                if isolate:
                    valid_isolate += 1
            elif isolate:
                overflow_isolate += 1
            elif overflow_isolate == 0:
                overflow_embedding += 1
            continue
        if typ == "PDI":
            if overflow_isolate:
                overflow_isolate -= 1
            elif valid_isolate:
                overflow_embedding = 0
                while len(stack) > 1:
                    _level, _override, is_isolate = stack.pop()
                    if is_isolate:
                        break
                valid_isolate -= 1
            atom.level = stack[-1][0]
            pdi_override = stack[-1][1]
            atom.bidi_type = pdi_override if pdi_override in {"L", "R"} else "PDI"
            continue
        if typ == "PDF":
            atom.removed = True
            if overflow_isolate:
                continue
            if overflow_embedding:
                overflow_embedding -= 1
            elif len(stack) > 1 and not stack[-1][2]:
                stack.pop()
            continue
        if typ == "B":
            stack = [(paragraph_level, None, False)]
            overflow_isolate = overflow_embedding = valid_isolate = 0
            atom.level = paragraph_level
            continue
        if override in {"L", "R"} and typ != "BN":
            atom.bidi_type = override
        if typ == "BN":
            atom.removed = True


def _isolating_run_sequences(atoms: Sequence[_Atom]) -> List[List[_Atom]]:
    """Compute BD13 isolating run sequences after X9 removal."""
    level_runs: List[List[_Atom]] = []
    for atom in atoms:
        if not level_runs or level_runs[-1][-1].level != atom.level:
            level_runs.append([atom])
        else:
            level_runs[-1].append(atom)
    if not level_runs:
        return []
    run_for_index: Dict[int, int] = {
        atom.original_index: run_index
        for run_index, level_run in enumerate(level_runs)
        for atom in level_run
    }
    initiator_to_pdi, pdi_to_initiator = _matching_isolates(atoms)
    sequences: List[List[_Atom]] = []
    used: set[int] = set()
    for run_index, level_run in enumerate(level_runs):
        if run_index in used:
            continue
        first = level_run[0]
        if first.original_type == "PDI" and first.original_index in pdi_to_initiator:
            continue
        sequence: List[_Atom] = []
        current = run_index
        while current not in used:
            used.add(current)
            current_run = level_runs[current]
            sequence.extend(current_run)
            last = current_run[-1]
            if last.original_type not in _ISOLATE_INIT:
                break
            matching = initiator_to_pdi.get(last.original_index)
            if matching is None:
                break
            next_run = run_for_index.get(matching)
            if next_run is None or next_run in used:
                break
            current = next_run
        sequences.append(sequence)
    # Overflow/ill-formed input can leave a PDI run without a discovered start;
    # include it independently rather than silently dropping characters.
    for run_index, level_run in enumerate(level_runs):
        if run_index not in used:
            sequences.append(list(level_run))
    return sequences


def _matching_isolates(atoms: Sequence[_Atom]) -> Tuple[Dict[int, int], Dict[int, int]]:
    stack: List[int] = []
    initiator_to_pdi: Dict[int, int] = {}
    pdi_to_initiator: Dict[int, int] = {}
    for atom in atoms:
        if atom.original_type in _ISOLATE_INIT:
            stack.append(atom.original_index)
        elif atom.original_type == "PDI" and stack:
            opening = stack.pop()
            initiator_to_pdi[opening] = atom.original_index
            pdi_to_initiator[atom.original_index] = opening
    return initiator_to_pdi, pdi_to_initiator


def _sor_eor(run: Sequence[_Atom], paragraph: Sequence[_Atom], paragraph_level: int) -> Tuple[str, str]:
    positions = {atom.original_index: index for index, atom in enumerate(paragraph)}
    first_position = positions.get(run[0].original_index, 0)
    last_position = positions.get(run[-1].original_index, len(paragraph) - 1)
    before = paragraph[first_position - 1].level if first_position > 0 else paragraph_level
    if run[-1].original_type in _ISOLATE_INIT:
        after = paragraph_level
    else:
        after = paragraph[last_position + 1].level if last_position + 1 < len(paragraph) else paragraph_level
    sor = "R" if max(run[0].level, before) % 2 else "L"
    eor = "R" if max(run[-1].level, after) % 2 else "L"
    return sor, eor


def _resolve_weak(run: List[_Atom], sor: str, eor: str) -> None:
    # W1: NSM inherits previous type.
    previous = sor
    for atom in run:
        if atom.bidi_type == "NSM":
            atom.bidi_type = previous if previous not in (_ISOLATE_INIT | {"PDI"}) else "ON"
        previous = atom.bidi_type
    # W2: EN following AL becomes AN.
    strong = sor
    for atom in run:
        if atom.bidi_type in _STRONG:
            strong = atom.bidi_type
        elif atom.bidi_type == "EN" and strong == "AL":
            atom.bidi_type = "AN"
    # W3: AL becomes R.
    for atom in run:
        if atom.bidi_type == "AL":
            atom.bidi_type = "R"
    # W4: a single ES/CS between matching numbers adopts the number type.
    for i in range(1, len(run) - 1):
        left, current, right = run[i - 1], run[i], run[i + 1]
        if current.bidi_type == "ES" and left.bidi_type == right.bidi_type == "EN":
            current.bidi_type = "EN"
        elif current.bidi_type == "CS" and left.bidi_type == right.bidi_type and left.bidi_type in {"EN", "AN"}:
            current.bidi_type = left.bidi_type
    # W5: ET adjacent to EN becomes EN.
    i = 0
    while i < len(run):
        if run[i].bidi_type != "ET":
            i += 1
            continue
        start = i
        while i < len(run) and run[i].bidi_type == "ET":
            i += 1
        left = run[start - 1].bidi_type if start else sor
        right = run[i].bidi_type if i < len(run) else eor
        if left == "EN" or right == "EN":
            for j in range(start, i):
                run[j].bidi_type = "EN"
    # W6: remaining separators/terminators become ON.
    for atom in run:
        if atom.bidi_type in {"ES", "ET", "CS"}:
            atom.bidi_type = "ON"
    # W7: EN following L becomes L.
    strong = sor
    for atom in run:
        if atom.bidi_type in {"L", "R"}:
            strong = atom.bidi_type
        elif atom.bidi_type == "EN" and strong == "L":
            atom.bidi_type = "L"


def _resolve_brackets(run: List[_Atom], sor: str) -> None:
    # N0 paired bracket algorithm, scoped to each isolating level run.
    stack: List[Tuple[str, int]] = []
    pairs: List[Tuple[int, int]] = []
    for index, atom in enumerate(run):
        char = atom.char
        if char in _BRACKET_PAIRS:
            if len(stack) < 63:
                stack.append((char, index))
        elif char in _BRACKET_CLOSE:
            expected = _BRACKET_CLOSE[char]
            for stack_index in range(len(stack) - 1, -1, -1):
                opening, opening_index = stack[stack_index]
                if opening == expected:
                    pairs.append((opening_index, index))
                    del stack[stack_index:]
                    break
    embedding = "R" if run[0].level % 2 else "L"
    opposite = "L" if embedding == "R" else "R"
    for opening, closing in pairs:
        enclosed = run[opening + 1 : closing]
        types = {atom.bidi_type for atom in enclosed}
        if embedding in types:
            resolved = embedding
        elif opposite in types:
            # N0b: use preceding strong type when opposite is present.
            preceding = sor
            for atom in reversed(run[:opening]):
                if atom.bidi_type in {"L", "R"}:
                    preceding = atom.bidi_type
                    break
            resolved = opposite if preceding == opposite else embedding
        else:
            continue
        run[opening].bidi_type = resolved
        run[closing].bidi_type = resolved
        index = opening + 1
        while index < len(run) and run[index].original_type == "NSM":
            run[index].bidi_type = resolved
            index += 1
        index = closing + 1
        while index < len(run) and run[index].original_type == "NSM":
            run[index].bidi_type = resolved
            index += 1


def _resolve_neutral(run: List[_Atom], sor: str, eor: str) -> None:
    i = 0
    while i < len(run):
        if run[i].bidi_type not in _NEUTRALS:
            i += 1
            continue
        start = i
        while i < len(run) and run[i].bidi_type in _NEUTRALS:
            i += 1
        before = sor if start == 0 else _strong_for_neutral(run[start - 1].bidi_type)
        after = eor if i == len(run) else _strong_for_neutral(run[i].bidi_type)
        resolved = before if before == after else ("R" if run[start].level % 2 else "L")
        for j in range(start, i):
            run[j].bidi_type = resolved


def _strong_for_neutral(typ: str) -> str:
    return "R" if typ in {"R", "EN", "AN"} else "L" if typ == "L" else typ


def _resolve_implicit(run: Iterable[_Atom]) -> None:
    for atom in run:
        if atom.level % 2 == 0:
            if atom.bidi_type == "R":
                atom.level += 1
            elif atom.bidi_type in {"EN", "AN"}:
                atom.level += 2
        else:
            if atom.bidi_type in {"L", "EN", "AN"}:
                atom.level += 1


def _reset_whitespace_levels(atoms: List[_Atom], paragraph_level: int) -> None:
    # L1: reset segment separators, paragraph separators, and trailing WS/BN.
    for atom in atoms:
        if atom.bidi_type in {"B", "S"}:
            atom.level = paragraph_level
    i = len(atoms) - 1
    while i >= 0 and atoms[i].original_type in {"WS", "BN", "B", "S", "FSI", "LRI", "RLI", "PDI"}:
        atoms[i].level = paragraph_level
        i -= 1


def _reorder_by_levels(atoms: List[_Atom]) -> List[_Atom]:
    if not atoms:
        return []
    out = list(atoms)
    highest = max(atom.level for atom in out)
    odd_levels = [atom.level for atom in out if atom.level % 2]
    if not odd_levels:
        return out
    lowest_odd = min(odd_levels)
    for level in range(highest, lowest_odd - 1, -1):
        index = 0
        while index < len(out):
            if out[index].level < level:
                index += 1
                continue
            start = index
            while index < len(out) and out[index].level >= level:
                index += 1
            out[start:index] = reversed(out[start:index])
    return out


def _coalesce_atoms(atoms: Sequence[_Atom]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for atom in atoms:
        if ord(atom.char) in {0x061C, 0x200E, 0x200F, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069}:
            continue
        token = dict(atom.token)
        token["text"] = _mirrored_character(atom.char) if atom.level % 2 else atom.char
        token["bidi_level"] = atom.level
        token["bidi_original_index"] = atom.original_index
        if out and _same_token_metadata(out[-1], token):
            out[-1]["text"] = str(out[-1].get("text", "")) + str(token["text"])
        else:
            out.append(token)
    return out


def _same_token_metadata(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    ignored = {"text", "bidi_original_index"}
    return {key: value for key, value in left.items() if key not in ignored} == {
        key: value for key, value in right.items() if key not in ignored
    }


def _mirrored_character(character: str) -> str:
    if not unicodedata.mirrored(character):
        return character
    if character in _BRACKET_PAIRS:
        return _BRACKET_PAIRS[character]
    if character in _BRACKET_CLOSE:
        return _BRACKET_CLOSE[character]
    return character
