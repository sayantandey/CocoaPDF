from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional, Set, Tuple

from .evidence import Evidence


SEMANTIC_KINDS: Set[str] = {
	"document", "section", "paragraph", "heading", "text", "strong",
	"emphasis", "code", "link", "list", "item", "quote", "code_block",
	"thematic_break", "table", "table_head", "table_body", "table_row",
	"table_cell", "figure", "caption", "image", "footnote_ref",
	"footnote", "toc", "toc_item", "reference_section", "reference",
	"cross_reference", "outline", "outline_item", "table_note",
	"equation", "callout", "sidebar", "form", "form_field", "annotation",
	"strikethrough", "underline", "superscript", "subscript", "mark",
	"anchor", "page_break",
	"html", "artifact", "unknown",
}


@dataclass(frozen=True)
class SourceRef:
	page: int
	glyph_ids: Tuple[int, ...] = ()
	region_ids: Tuple[str, ...] = ()
	mcids: Tuple[int, ...] = ()
	object_refs: Tuple[str, ...] = ()
	bbox: Optional[Tuple[float, float, float, float]] = None

	def to_dict(self) -> Dict[str, Any]:
		out: Dict[str, Any] = {
			"page": self.page,
			"glyph_ids": list(self.glyph_ids),
			"region_ids": list(self.region_ids),
			"mcids": list(self.mcids),
			"object_refs": list(self.object_refs),
		}
		if self.bbox is not None:
			out["bbox"] = [round(value, 3) for value in self.bbox]
		return out


@dataclass
class SemanticNode:
	id: str
	kind: str
	children: List["SemanticNode"] = field(default_factory=list)
	text: str = ""
	attrs: Dict[str, Any] = field(default_factory=dict)
	confidence: float = 1.0
	evidence: List[Evidence] = field(default_factory=list)
	sources: List[SourceRef] = field(default_factory=list)
	warnings: List[str] = field(default_factory=list)

	def add(self, *nodes: "SemanticNode") -> "SemanticNode":
		self.children.extend(nodes)
		return self

	def walk(self) -> Iterator["SemanticNode"]:
		yield self
		for child in self.children:
			yield from child.walk()

	def source_pages(self) -> List[int]:
		return sorted({source.page for source in self.sources if source.page > 0})

	def to_dict(self) -> Dict[str, Any]:
		return {
			"id": self.id,
			"kind": self.kind,
			"text": self.text,
			"attrs": _json_value(self.attrs),
			"children": [child.to_dict() for child in self.children],
			"confidence": round(float(self.confidence), 4),
			"evidence": [item.to_dict() for item in self.evidence],
			"sources": [source.to_dict() for source in self.sources],
			"source_pages": self.source_pages(),
			"warnings": list(self.warnings),
		}


@dataclass
class SemanticDocument:
	children: List[SemanticNode] = field(default_factory=list)
	metadata: Dict[str, Any] = field(default_factory=dict)
	warnings: List[str] = field(default_factory=list)
	version: str = "1"

	def walk(self) -> Iterator[SemanticNode]:
		for child in self.children:
			yield from child.walk()

	def index(self) -> Dict[str, SemanticNode]:
		return {node.id: node for node in self.walk()}

	def validate(self, require_provenance: bool = True) -> List[str]:
		errors: List[str] = []
		seen: Set[str] = set()
		active: Set[int] = set()

		def visit(node: SemanticNode, path: str) -> None:
			identity = id(node)
			if identity in active:
				errors.append("semantic cycle at %s" % path)
				return
			active.add(identity)
			if not node.id:
				errors.append("missing node id at %s" % path)
			elif node.id in seen:
				errors.append("duplicate node id %s" % node.id)
			seen.add(node.id)
			if node.kind not in SEMANTIC_KINDS:
				errors.append("unknown node kind %s at %s" % (node.kind, path))
			if not 0.0 <= float(node.confidence) <= 1.0:
				errors.append("confidence outside [0,1] at %s" % path)
			if require_provenance and node.kind not in {"document", "section", "artifact", "anchor", "page_break"} and not node.sources:
				errors.append("missing provenance for %s" % node.id)
			for index, child in enumerate(node.children):
				visit(child, "%s/%d" % (path, index))
			active.remove(identity)

		for index, child in enumerate(self.children):
			visit(child, "/%d" % index)
		return errors

	def to_dict(self) -> Dict[str, Any]:
		return {
			"schema": "cocoapdf.semantic-document",
			"version": self.version,
			"metadata": _json_value(self.metadata),
			"children": [child.to_dict() for child in self.children],
			"warnings": list(self.warnings),
		}


class NodeFactory:
	def __init__(self, prefix: str = "node") -> None:
		self.prefix = prefix
		self._next = 1

	def make(self, kind: str, **kwargs: Any) -> SemanticNode:
		node = SemanticNode(id="%s-%d" % (self.prefix, self._next), kind=kind, **kwargs)
		self._next += 1
		return node


def merge_sources(sources: Iterable[SourceRef]) -> List[SourceRef]:
	grouped: Dict[int, Dict[str, Any]] = {}
	for source in sources:
		item = grouped.setdefault(source.page, {"glyphs": set(), "regions": set(), "mcids": set(), "objects": set(), "boxes": []})
		item["glyphs"].update(source.glyph_ids)
		item["regions"].update(source.region_ids)
		item["mcids"].update(source.mcids)
		item["objects"].update(source.object_refs)
		if source.bbox is not None:
			item["boxes"].append(source.bbox)
	out: List[SourceRef] = []
	for page, item in sorted(grouped.items()):
		boxes = item["boxes"]
		bbox = None
		if boxes:
			bbox = (min(box[0] for box in boxes), min(box[1] for box in boxes), max(box[2] for box in boxes), max(box[3] for box in boxes))
		out.append(SourceRef(page=page, glyph_ids=tuple(sorted(item["glyphs"])), region_ids=tuple(sorted(item["regions"])), mcids=tuple(sorted(item["mcids"])), object_refs=tuple(sorted(item["objects"])), bbox=bbox))
	return out


def _json_value(value: Any) -> Any:
	if value is None or isinstance(value, (bool, int, float, str)):
		return value
	if isinstance(value, dict):
		return {str(key): _json_value(item) for key, item in value.items()}
	if isinstance(value, (list, tuple, set)):
		return [_json_value(item) for item in value]
	return str(value)
