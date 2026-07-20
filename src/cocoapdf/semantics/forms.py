from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..ir.evidence import Evidence
from ..ir.semantic import NodeFactory, SemanticNode, SourceRef


_INHERITED_KEYS = ("FT", "Ff", "V", "DV", "DA", "Q", "Opt", "MaxLen", "I", "TI")


def extract_acroform(document: Any, factory: NodeFactory, page_ref_to_num: Dict[Tuple[int, int], int]) -> Optional[SemanticNode]:
    catalog = document.catalog()
    raw_form = catalog.get("AcroForm") if isinstance(catalog, dict) else None
    form = _resolve(document, raw_form)
    if not isinstance(form, dict):
        return None
    fields = _resolve(document, form.get("Fields"))
    if not isinstance(fields, list):
        fields = []
    widget_pages = _widget_page_index(document, page_ref_to_num)
    children: List[SemanticNode] = []
    active: set[Tuple[str, int]] = set()
    for raw in fields:
        children.extend(_walk_field(document, factory, raw, {}, [], page_ref_to_num, widget_pages, active))
    if not children:
        return None
    sources = [source for child in children for source in child.sources]
    return factory.make(
        "form",
        children=children,
        attrs={
            "need_appearances": bool(_resolve(document, form.get("NeedAppearances"))),
            "signature_flags": int(_number(document, form.get("SigFlags"), 0)),
            "calculation_order_present": bool(_resolve(document, form.get("CO"))),
        },
        confidence=0.99,
        evidence=[Evidence("acroform_field_tree", 0.99, data={"field_count": len(children)})],
        sources=sources,
    )


def _walk_field(
    document: Any,
    factory: NodeFactory,
    raw: Any,
    inherited: Dict[str, Any],
    name_parts: List[str],
    page_ref_to_num: Dict[Tuple[int, int], int],
    widget_pages: Dict[str, int],
    active: set[Tuple[str, int]],
) -> List[SemanticNode]:
    value = _resolve(document, raw)
    if not isinstance(value, dict):
        return []
    identity = _identity(raw, value)
    if identity in active:
        return []
    active.add(identity)
    try:
        state = dict(inherited)
        for key in _INHERITED_KEYS:
            if key in value:
                state[key] = value[key]
        partial = _text(document, value.get("T"))
        parts = name_parts + ([partial] if partial else [])
        kids_raw = _resolve(document, value.get("Kids"))
        kids = kids_raw if isinstance(kids_raw, list) else []
        subtype = _name(document, value.get("Subtype"))
        terminal = bool(state.get("FT")) and (subtype == "Widget" or not kids or all(_name(document, _resolve(document, kid).get("Subtype")) == "Widget" for kid in kids if isinstance(_resolve(document, kid), dict)))
        out: List[SemanticNode] = []
        if terminal:
            out.append(_field_node(document, factory, raw, value, state, parts, kids, page_ref_to_num, widget_pages))
        else:
            for kid in kids:
                out.extend(_walk_field(document, factory, kid, state, parts, page_ref_to_num, widget_pages, active))
        return out
    finally:
        active.remove(identity)


def _field_node(
    document: Any,
    factory: NodeFactory,
    raw: Any,
    value: Dict[str, Any],
    state: Dict[str, Any],
    parts: Sequence[str],
    kids: Sequence[Any],
    page_ref_to_num: Dict[Tuple[int, int], int],
    widget_pages: Dict[str, int],
) -> SemanticNode:
    field_type = _name(document, state.get("FT"))
    flags = int(_number(document, state.get("Ff"), 0))
    kind = _field_kind(field_type, flags)
    raw_value = _resolve(document, state.get("V"))
    display_value = _field_value(document, raw_value)
    default_value = _field_value(document, _resolve(document, state.get("DV")))
    options = _options(document, state.get("Opt"))
    selected_indices = _integer_list(document, state.get("I"))
    if kind in {"combo", "listbox"}:
        selected = _selected_options(options, selected_indices, raw_value)
        if selected:
            display_value = ", ".join(option["display"] for option in selected)
    widget_pairs: List[Tuple[Any, Dict[str, Any]]] = []
    if _name(document, value.get("Subtype")) == "Widget":
        widget_pairs.append((raw, value))
    else:
        for kid in kids:
            widget = _resolve(document, kid)
            if isinstance(widget, dict) and _name(document, widget.get("Subtype")) == "Widget":
                widget_pairs.append((kid, widget))
    sources: List[SourceRef] = []
    widget_meta: List[Dict[str, Any]] = []
    export_states: List[str] = []
    actions_ignored = any(key in value for key in ("A", "AA"))
    for widget_raw, widget in widget_pairs:
        page = _page_number(document, widget.get("P"), page_ref_to_num) or widget_pages.get(_ref_text(widget_raw), 0)
        rect_value = _resolve(document, widget.get("Rect"))
        rect = tuple(float(item) for item in rect_value[:4]) if isinstance(rect_value, list) and len(rect_value) >= 4 else None
        object_ref = _ref_text(widget_raw)
        states = _appearance_states(document, widget)
        export_states.extend(state for state in states if state != "Off")
        actions_ignored = actions_ignored or any(key in widget for key in ("A", "AA"))
        sources.append(SourceRef(page=page, object_refs=(object_ref,), bbox=rect))
        widget_meta.append({
            "object_ref": object_ref,
            "page": page or None,
            "rect": list(rect) if rect else None,
            "appearance_state": _name(document, widget.get("AS")) or None,
            "export_states": states,
            "annotation_flags": int(_number(document, widget.get("F"), 0)),
            "highlight_mode": _name(document, widget.get("H")) or None,
        })
    password = kind == "text" and bool(flags & (1 << 13))
    value_redacted = password and bool(display_value)
    if value_redacted:
        display_value = "[redacted]"
    alternate_name = _text(document, value.get("TU"))
    mapping_name = _text(document, value.get("TM"))
    attrs = {
        "name": ".".join(parts),
        "partial_name": parts[-1] if parts else "",
        "alternate_name": alternate_name or None,
        "mapping_name": mapping_name or None,
        "field_type": kind,
        "pdf_field_type": field_type,
        "value": display_value,
        "default_value": default_value,
        "flags": flags,
        "read_only": bool(flags & 1),
        "required": bool(flags & 2),
        "no_export": bool(flags & 4),
        "multiline": kind == "text" and bool(flags & (1 << 12)),
        "password": password,
        "value_redacted": value_redacted,
        "comb": kind == "text" and bool(flags & (1 << 24)),
        "max_length": int(_number(document, state.get("MaxLen"), 0)) or None,
        "options": options,
        "selected_indices": selected_indices,
        "top_index": int(_number(document, state.get("TI"), 0)) if kind == "listbox" else None,
        "export_states": sorted(set(export_states)),
        "widgets": widget_meta,
        "actions_ignored": actions_ignored,
    }
    if kind in {"checkbox", "radio"}:
        attrs["checked"] = bool(display_value and display_value not in {"Off", "0", "False"})
        attrs["export_value"] = display_value if attrs["checked"] else (export_states[0] if len(set(export_states)) == 1 else None)
    if kind == "signature":
        attrs["signature"] = _signature_metadata(document, raw_value)
    return factory.make(
        "form_field",
        text=display_value,
        attrs=attrs,
        confidence=0.99,
        evidence=[Evidence("acroform_terminal_field", 0.99, detail=kind, data={"object_ref": _ref_text(raw)})],
        sources=sources,
        warnings=["PDF_ACTIONS_NOT_EXECUTED"] if attrs["actions_ignored"] else [],
    )


def _field_kind(field_type: str, flags: int) -> str:
    if field_type == "Tx":
        return "text"
    if field_type == "Ch":
        return "combo" if flags & (1 << 17) else "listbox"
    if field_type == "Sig":
        return "signature"
    if field_type == "Btn":
        if flags & (1 << 16):
            return "pushbutton"
        if flags & (1 << 15):
            return "radio"
        return "checkbox"
    return "unknown"


def _field_value(document: Any, value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(_field_value(document, item) for item in value)
    if isinstance(value, bytes):
        from ..core import decode_pdf_text
        return decode_pdf_text(value)
    return str(value).lstrip("/")


def _options(document: Any, raw: Any) -> List[Dict[str, str]]:
    value = _resolve(document, raw)
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        item = _resolve(document, item)
        if isinstance(item, list) and item:
            export = _field_value(document, item[0])
            display = _field_value(document, item[1] if len(item) > 1 else item[0])
        else:
            export = display = _field_value(document, item)
        out.append({"export": export, "display": display})
    return out


def _integer_list(document: Any, raw: Any) -> List[int]:
    value = _resolve(document, raw)
    if isinstance(value, int) and not isinstance(value, bool):
        return [int(value)]
    if not isinstance(value, list):
        return []
    out: List[int] = []
    for item in value:
        resolved = _resolve(document, item)
        if isinstance(resolved, int) and not isinstance(resolved, bool):
            out.append(int(resolved))
    return out


def _selected_options(options: Sequence[Dict[str, str]], indices: Sequence[int], raw_value: Any) -> List[Dict[str, str]]:
    selected = [options[index] for index in indices if 0 <= index < len(options)]
    if selected:
        return selected
    values = raw_value if isinstance(raw_value, list) else [raw_value]
    wanted = {str(value).lstrip("/") for value in values if value is not None}
    return [option for option in options if option.get("export") in wanted or option.get("display") in wanted]


def _appearance_states(document: Any, widget: Dict[str, Any]) -> List[str]:
    appearance = _resolve(document, widget.get("AP"))
    normal = _resolve(document, appearance.get("N")) if isinstance(appearance, dict) else None
    if not isinstance(normal, dict):
        return []
    return sorted({_name(document, key) for key in normal if _name(document, key)})


def _signature_metadata(document: Any, raw: Any) -> Optional[Dict[str, Any]]:
    value = _resolve(document, raw)
    if not isinstance(value, dict):
        return None
    byte_range = _resolve(document, value.get("ByteRange"))
    return {
        "filter": _name(document, value.get("Filter")) or None,
        "subfilter": _name(document, value.get("SubFilter")) or None,
        "signer_name": _text(document, value.get("Name")) or None,
        "signing_time": _text(document, value.get("M")) or None,
        "reason": _text(document, value.get("Reason")) or None,
        "location": _text(document, value.get("Location")) or None,
        "contact_info": _text(document, value.get("ContactInfo")) or None,
        "byte_range": [int(item) for item in byte_range] if isinstance(byte_range, list) and all(isinstance(item, int) for item in byte_range) else None,
        "contents_present": value.get("Contents") is not None,
        "verified": False,
    }


def _widget_page_index(document: Any, page_ref_to_num: Dict[Tuple[int, int], int]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    try:
        pages = document.pages()
    except Exception:
        return out
    for page_number, page in enumerate(pages, 1):
        annotations = _resolve(document, page.get("Annots")) if isinstance(page, dict) else None
        if not isinstance(annotations, list):
            continue
        for raw in annotations:
            annotation = _resolve(document, raw)
            if isinstance(annotation, dict) and _name(document, annotation.get("Subtype")) == "Widget":
                out[_ref_text(raw)] = page_number
    return out


def _page_number(document: Any, raw: Any, page_ref_to_num: Dict[Tuple[int, int], int]) -> int:
    number = getattr(raw, "num", None)
    generation = int(getattr(raw, "gen", 0) or 0)
    if isinstance(number, int):
        return page_ref_to_num.get((number, generation), page_ref_to_num.get((number, 0), 0))
    return 0


def _resolve(document: Any, value: Any) -> Any:
    try:
        return document.resolve(value)
    except Exception:
        return None


def _number(document: Any, value: Any, default: float) -> float:
    value = _resolve(document, value)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def _name(document: Any, value: Any) -> str:
    return str(_resolve(document, value) or "").lstrip("/")


def _text(document: Any, value: Any) -> str:
    value = _resolve(document, value)
    if isinstance(value, bytes):
        from ..core import decode_pdf_text
        return decode_pdf_text(value)
    return str(value) if isinstance(value, str) else ""


def _ref_text(raw: Any) -> str:
    number = getattr(raw, "num", None)
    generation = int(getattr(raw, "gen", 0) or 0)
    return "%d %d R" % (number, generation) if isinstance(number, int) else "direct:%d" % id(raw)


def _identity(raw: Any, value: Any) -> Tuple[str, int]:
    number = getattr(raw, "num", None)
    generation = int(getattr(raw, "gen", 0) or 0)
    return ("ref-%d-%d" % (number, generation), number) if isinstance(number, int) else ("direct", id(value))
