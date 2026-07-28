from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from ..ir.evidence import Evidence
from ..ir.semantic import NodeFactory, SemanticNode, SourceRef


_INHERITED_KEYS = ("FT", "Ff", "V", "DV", "DA", "Q", "Opt", "MaxLen", "I", "TI")
_ACROFORM_DEFAULT_KEYS = ("DA", "Q")
_MAX_APPEARANCE_STREAM_BYTES = 262_144


def extract_acroform(
    document: Any,
    factory: NodeFactory,
    page_ref_to_num: Dict[Tuple[int, int], int],
    selected_pages: Optional[Set[int]] = None,
) -> Optional[SemanticNode]:
    catalog = document.catalog()
    raw_form = catalog.get("AcroForm") if isinstance(catalog, dict) else None
    form = _resolve(document, raw_form)
    if not isinstance(form, dict):
        return None
    fields = _resolve(document, form.get("Fields"))
    if not isinstance(fields, list):
        fields = []
    form_defaults = {
        key: form[key]
        for key in _ACROFORM_DEFAULT_KEYS
        if key in form
    }
    widget_pages = _widget_page_index(document, page_ref_to_num)
    children: List[SemanticNode] = []
    active: set[Tuple[str, int]] = set()
    for raw in fields:
        children.extend(
            _walk_field(
                document,
                factory,
                raw,
                form_defaults,
                [],
                page_ref_to_num,
                widget_pages,
                active,
            )
        )
    if selected_pages is not None:
        # AcroForm field trees are document-global. Interpret the complete tree
        # first so inheritance and widget identity remain intact, then retain
        # only widgets proven to belong to the selected page slice.
        selected_children: List[SemanticNode] = []
        for child in children:
            selected_sources = [
                source for source in child.sources
                if source.page in selected_pages
            ]
            if not selected_sources:
                # Page-less fields cannot be assigned to a partial document
                # without inventing provenance.
                continue
            child.sources = selected_sources
            widgets = child.attrs.get("widgets")
            if isinstance(widgets, list):
                selected_widgets = [
                    widget for widget in widgets
                    if isinstance(widget, dict) and widget.get("page") in selected_pages
                ]
                child.attrs["widgets"] = selected_widgets
                child.attrs["appearance"] = (
                    _uniform_widget_appearance(selected_widgets)
                    if child.attrs.get("field_type") in {"text", "combo", "listbox"}
                    else None
                )
            selected_children.append(child)
        children = selected_children
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
        appearance = _widget_appearance(document, widget, state, rect)
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
            "appearance": appearance or None,
        })
    password = kind == "text" and bool(flags & (1 << 13))
    value_redacted = password and bool(display_value)
    if value_redacted:
        display_value = "[redacted]"
    alternate_name = _text(document, value.get("TU"))
    mapping_name = _text(document, value.get("TM"))
    uniform_appearance = (
        _uniform_widget_appearance(widget_meta)
        if kind in {"text", "combo", "listbox"}
        else None
    )
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
        # A field may have several widgets with different appearances. HTML is
        # styled only when every retained widget agrees; per-widget evidence
        # always remains available in ``widgets``.
        "appearance": uniform_appearance,
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


def _uniform_widget_appearance(
    widgets: Sequence[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    appearances = [
        widget.get("appearance")
        for widget in widgets
        if isinstance(widget.get("appearance"), dict)
    ]
    if (
        len(appearances) != len(widgets)
        or not appearances
        or any(candidate != appearances[0] for candidate in appearances[1:])
    ):
        return None
    return appearances[0]


def _widget_appearance(
    document: Any,
    widget: Dict[str, Any],
    state: Dict[str, Any],
    rect: Optional[Tuple[float, ...]],
) -> Dict[str, Any]:
    """Recover only explicit, safely renderable widget appearance evidence.

    PDF viewers may synthesize blue focus rectangles, font sizes, and other UI
    chrome when a widget lacks an appearance. Those viewer choices are not PDF
    content and must not leak into deterministic output. The values below come
    only from /DA, /MK, /Q, an /AP normal appearance stream, or /Rect.
    """
    appearance: Dict[str, Any] = {}
    sources: List[str] = []

    default_appearance = _default_appearance_style(
        document,
        widget.get("DA", state.get("DA")),
    )
    characteristics = _appearance_characteristics_style(document, widget)
    normal_stream = _normal_appearance_stream(document, widget)
    normal_appearance = (
        _normal_appearance_style(document, normal_stream)
        if normal_stream is not None
        else {}
    )
    if normal_stream is not None:
        # /AP is the painted normal appearance. Keep /DA and /MK declarations
        # as provenance, but never use an unpainted declaration to fill a
        # visual property missing from an authoritative appearance stream.
        if default_appearance:
            appearance["declared_default_appearance"] = default_appearance
            sources.append("default_appearance")
        if characteristics:
            appearance["declared_appearance_characteristics"] = characteristics
            sources.append("appearance_characteristics")
        appearance.update(normal_appearance)
        sources.append("normal_appearance_stream")
    else:
        if default_appearance:
            appearance.update(default_appearance)
            sources.append("default_appearance")
        if characteristics:
            appearance.update(characteristics)
            sources.append("appearance_characteristics")

    alignment_number = _resolved_number(
        document,
        widget.get("Q", state.get("Q")),
    )
    if alignment_number in (0.0, 1.0, 2.0):
        appearance["text_alignment"] = {
            0.0: "left",
            1.0: "center",
            2.0: "right",
        }[alignment_number]
        sources.append("quadding")

    if not sources:
        return {}
    if rect and len(rect) >= 4:
        width = abs(float(rect[2]) - float(rect[0]))
        height = abs(float(rect[3]) - float(rect[1]))
        if _positive_finite(width) and _positive_finite(height):
            appearance["width_pt"] = round(width, 6)
            appearance["height_pt"] = round(height, 6)
            sources.append("widget_rect")
    appearance["sources"] = sorted(set(sources))
    return appearance


def _default_appearance_style(document: Any, raw: Any) -> Dict[str, Any]:
    value = _resolve(document, raw)
    if isinstance(value, str):
        data = value.encode("latin-1", "replace")
    elif isinstance(value, bytes):
        data = value
    else:
        return {}
    if len(data) > _MAX_APPEARANCE_STREAM_BYTES:
        return {}
    style: Dict[str, Any] = {}
    for operator, operands in _content_operations(data):
        if operator == "Tf" and len(operands) >= 2:
            font_name = operands[-2]
            font_size = _finite_float(operands[-1])
            if isinstance(font_name, str) and font_name:
                style["font_resource"] = font_name.lstrip("/")
            if font_size is not None:
                if font_size > 0:
                    style["font_size_pt"] = round(font_size, 6)
                elif font_size == 0:
                    style["font_size_auto"] = True
        elif operator in {"g", "rg", "k"}:
            color = _operator_color(operator, operands)
            if color is not None:
                style["text_color_rgb"] = color
    return style


def _appearance_characteristics_style(
    document: Any,
    widget: Dict[str, Any],
) -> Dict[str, Any]:
    characteristics = _resolve(document, widget.get("MK"))
    if not isinstance(characteristics, dict):
        return {}
    style: Dict[str, Any] = {}
    background = _pdf_color(document, characteristics.get("BG"))
    border = _pdf_color(document, characteristics.get("BC"))
    if background is not None:
        style["background_color_rgb"] = background
    if border is not None:
        style["border_color_rgb"] = border
    rotation = _resolved_number(document, characteristics.get("R"))
    if rotation is not None and math.isfinite(rotation):
        normalized = int(round(rotation)) % 360
        if normalized in {0, 90, 180, 270}:
            style["rotation"] = normalized
    return style


def _normal_appearance_style(
    document: Any,
    stream: Any,
) -> Dict[str, Any]:
    attrs = getattr(stream, "attrs", None)
    if not isinstance(attrs, dict):
        return {}
    try:
        data = document.decoded_stream(stream)
    except Exception:
        return {}
    if not isinstance(data, bytes) or len(data) > _MAX_APPEARANCE_STREAM_BYTES:
        return {}

    bbox = _numeric_rect(document, attrs.get("BBox"))
    fill_color: Optional[List[float]] = None
    stroke_color: Optional[List[float]] = None
    font_resource: Optional[str] = None
    font_size: Optional[float] = None
    line_width: Optional[float] = None
    pending_rect: Optional[Tuple[float, float, float, float]] = None
    graphics_stack: List[
        Tuple[
            Optional[List[float]],
            Optional[List[float]],
            Optional[str],
            Optional[float],
            Optional[float],
        ]
    ] = []
    style: Dict[str, Any] = {}
    for operator, operands in _content_operations(data):
        if operator == "q":
            if len(graphics_stack) >= 64:
                break
            graphics_stack.append(
                (
                    fill_color,
                    stroke_color,
                    font_resource,
                    font_size,
                    line_width,
                )
            )
        elif operator == "Q" and graphics_stack:
            fill_color, stroke_color, font_resource, font_size, line_width = (
                graphics_stack.pop()
            )
        elif operator in {"g", "rg", "k"}:
            fill_color = _operator_color(operator, operands)
        elif operator in {"G", "RG", "K"}:
            stroke_color = _operator_color(operator.lower(), operands)
        elif operator == "Tf" and len(operands) >= 2:
            candidate_name = operands[-2]
            candidate_size = _finite_float(operands[-1])
            if isinstance(candidate_name, str) and candidate_name:
                font_resource = candidate_name.lstrip("/")
            if candidate_size is not None:
                font_size = candidate_size
        elif operator == "w" and operands:
            candidate_width = _finite_float(operands[-1])
            if candidate_width is not None and candidate_width >= 0:
                line_width = candidate_width
        elif operator == "re" and len(operands) >= 4:
            values = [_finite_float(item) for item in operands[-4:]]
            if all(item is not None for item in values):
                pending_rect = tuple(float(item) for item in values)  # type: ignore[arg-type]
        elif operator in {"f", "F", "f*", "B", "B*", "b", "b*"}:
            if (
                fill_color is not None
                and pending_rect is not None
                and _rect_covers_bbox(pending_rect, bbox)
            ):
                style["background_color_rgb"] = fill_color
            if (
                operator in {"B", "B*", "b", "b*"}
                and stroke_color is not None
                and pending_rect is not None
                and _rect_covers_bbox(pending_rect, bbox)
            ):
                style["border_color_rgb"] = stroke_color
                if line_width is not None:
                    style["border_width_pt"] = round(line_width, 6)
            pending_rect = None
        elif operator in {"S", "s"}:
            if (
                stroke_color is not None
                and pending_rect is not None
                and _rect_covers_bbox(pending_rect, bbox)
            ):
                style["border_color_rgb"] = stroke_color
                if line_width is not None:
                    style["border_width_pt"] = round(line_width, 6)
            pending_rect = None
        elif operator in {"Tj", "TJ", "'", '"'}:
            if fill_color is not None:
                style["text_color_rgb"] = fill_color
            if font_resource:
                style["font_resource"] = font_resource
            if font_size is not None:
                if font_size > 0:
                    style["font_size_pt"] = round(font_size, 6)
                elif font_size == 0:
                    style["font_size_auto"] = True
    return style


def _normal_appearance_stream(document: Any, widget: Dict[str, Any]) -> Any:
    appearance = _resolve(document, widget.get("AP"))
    if not isinstance(appearance, dict):
        return None
    normal = _resolve(document, appearance.get("N"))
    if isinstance(getattr(normal, "attrs", None), dict):
        return normal
    if not isinstance(normal, dict):
        return None
    state = _name(document, widget.get("AS"))
    if state:
        for raw_name, raw_stream in normal.items():
            if _name(document, raw_name) == state:
                candidate = _resolve(document, raw_stream)
                if isinstance(getattr(candidate, "attrs", None), dict):
                    return candidate
        return None
    candidates = []
    for raw_name, raw_stream in normal.items():
        if _name(document, raw_name) == "Off":
            continue
        candidate = _resolve(document, raw_stream)
        if isinstance(getattr(candidate, "attrs", None), dict):
            candidates.append(candidate)
    return candidates[0] if len(candidates) == 1 else None


def _content_operations(data: bytes) -> Any:
    from ..content.tokens import tokenize_content

    operands: List[Any] = []
    try:
        for token in tokenize_content(data):
            operator = getattr(token, "name", None)
            if operator is None:
                if len(operands) < 32:
                    operands.append(token)
                continue
            yield str(operator), operands
            operands = []
    except Exception:
        return


def _operator_color(
    operator: str,
    operands: Sequence[Any],
) -> Optional[List[float]]:
    component_count = {"g": 1, "rg": 3, "k": 4}.get(operator)
    if component_count is None or len(operands) < component_count:
        return None
    values = [
        _finite_float(item)
        for item in operands[-component_count:]
    ]
    if any(value is None for value in values):
        return None
    return _components_to_rgb([float(value) for value in values])


def _pdf_color(document: Any, raw: Any) -> Optional[List[float]]:
    value = _resolve(document, raw)
    if not isinstance(value, list) or len(value) not in {1, 3, 4}:
        return None
    components = [_resolved_number(document, item) for item in value]
    if any(component is None for component in components):
        return None
    return _components_to_rgb([float(component) for component in components])


def _components_to_rgb(components: Sequence[float]) -> Optional[List[float]]:
    if not components or any(not math.isfinite(value) for value in components):
        return None
    values = [min(1.0, max(0.0, value)) for value in components]
    if len(values) == 1:
        red = green = blue = values[0]
    elif len(values) == 3:
        red, green, blue = values
    elif len(values) == 4:
        cyan, magenta, yellow, black = values
        red = 1.0 - min(1.0, cyan + black)
        green = 1.0 - min(1.0, magenta + black)
        blue = 1.0 - min(1.0, yellow + black)
    else:
        return None
    return [round(red, 6), round(green, 6), round(blue, 6)]


def _numeric_rect(
    document: Any,
    raw: Any,
) -> Optional[Tuple[float, float, float, float]]:
    value = _resolve(document, raw)
    if not isinstance(value, list) or len(value) < 4:
        return None
    numbers = [_resolved_number(document, item) for item in value[:4]]
    if any(number is None for number in numbers):
        return None
    return tuple(float(number) for number in numbers)  # type: ignore[arg-type]


def _rect_covers_bbox(
    rect: Tuple[float, float, float, float],
    bbox: Optional[Tuple[float, float, float, float]],
) -> bool:
    if bbox is None:
        return False
    rect_x0, rect_x1 = sorted((rect[0], rect[0] + rect[2]))
    rect_y0, rect_y1 = sorted((rect[1], rect[1] + rect[3]))
    bbox_x0, bbox_x1 = sorted((bbox[0], bbox[2]))
    bbox_y0, bbox_y1 = sorted((bbox[1], bbox[3]))
    rect_area = (rect_x1 - rect_x0) * (rect_y1 - rect_y0)
    bbox_area = (bbox_x1 - bbox_x0) * (bbox_y1 - bbox_y0)
    overlap_width = max(0.0, min(rect_x1, bbox_x1) - max(rect_x0, bbox_x0))
    overlap_height = max(0.0, min(rect_y1, bbox_y1) - max(rect_y0, bbox_y0))
    overlap_area = overlap_width * overlap_height
    return (
        rect_area > 0
        and bbox_area > 0
        and rect_area >= bbox_area * 0.75
        and overlap_area >= bbox_area * 0.75
    )


def _resolved_number(document: Any, raw: Any) -> Optional[float]:
    value = _resolve(document, raw)
    return _finite_float(value)


def _finite_float(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _positive_finite(value: float) -> bool:
    return math.isfinite(value) and value > 0


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
