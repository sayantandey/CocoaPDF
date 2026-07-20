from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from ..ir.evidence import Evidence
from ..ir.semantic import NodeFactory, SemanticDocument, SemanticNode, SourceRef


ROLE_TO_KIND = {
	"Document": "document", "DocumentFragment": "section", "Part": "section",
	"Art": "section", "Sect": "section", "Div": "section", "Aside": "sidebar",
	"P": "paragraph", "Title": "heading", "H": "heading", "H1": "heading",
	"H2": "heading", "H3": "heading", "H4": "heading", "H5": "heading",
	"H6": "heading", "L": "list", "LI": "item", "LBody": "section",
	"Lbl": "text", "BlockQuote": "quote", "Quote": "quote", "Code": "code",
	"Em": "emphasis", "Strong": "strong", "Sub": "subscript",
	"Table": "table", "THead": "table_head", "TBody": "table_body",
	"TFoot": "table_body", "TR": "table_row", "TH": "table_cell",
	"TD": "table_cell", "Figure": "figure", "Caption": "caption",
	"Link": "link", "Annot": "annotation", "Form": "form_field",
	"Note": "footnote", "FENote": "footnote", "Reference": "footnote_ref",
	"TOC": "toc", "TOCI": "toc_item", "BibEntry": "reference",
	"Formula": "equation", "Artifact": "artifact", "Span": "text",
	"Ruby": "text", "RB": "text", "RT": "text", "RP": "text",
	"Warichu": "text", "WT": "text", "WP": "text",
}


class TaggedStructureParser:
	def __init__(self, document: Any) -> None:
		self.document = document
		self.factory = NodeFactory("tag")
		self.warnings: List[str] = []
		self.role_map: Dict[str, str] = {}
		self.parent_tree: Dict[int, Any] = {}
		self.class_map: Dict[str, Any] = {}
		self.namespace_role_maps: Dict[str, Dict[str, str]] = {}
		self._active: Set[Tuple[str, int]] = set()
		self.document_language = ""
		self._page_refs, self._direct_pages, self._page_struct_parents = self._page_maps()

	def parse(self) -> SemanticDocument:
		catalog = self.document.catalog()
		root = self._resolve(catalog.get("StructTreeRoot")) if isinstance(catalog, dict) else None
		result = SemanticDocument(metadata={"source": "tagged_pdf"})
		if not isinstance(root, dict):
			result.warnings.append("TAGGED_STRUCTURE_ABSENT")
			return result
		self.document_language = self._pdf_text(root.get("Lang")) or self._pdf_text(catalog.get("Lang"))
		self.role_map = self._read_role_map(root.get("RoleMap"))
		self.class_map = self._read_class_map(root.get("ClassMap"))
		self.namespace_role_maps = self._read_namespaces(root.get("Namespaces"))
		self.parent_tree = self._read_number_tree(root.get("ParentTree"))
		for child in self._as_list(root.get("K")):
			node = self._parse_kid(child, inherited_page=0, inherited_lang=self.document_language, owner_raw=root.get("K"))
			if node is not None:
				result.children.append(node)
		result.metadata.update({
			"role_map": dict(sorted(self.role_map.items())),
			"parent_tree_keys": sorted(self.parent_tree),
			"class_map_keys": sorted(self.class_map),
			"namespace_role_maps": self.namespace_role_maps,
			"language": self.document_language or None,
		})
		result.warnings.extend(self.warnings)
		return result

	def _parse_kid(
		self,
		raw: Any,
		inherited_page: int,
		inherited_lang: str = "",
		owner_raw: Any = None,
	) -> Optional[SemanticNode]:
		if isinstance(raw, int):
			return self._content_ref(raw, inherited_page, "MCID", owner_raw=owner_raw)
		value = self._resolve(raw)
		if isinstance(value, list):
			container = self.factory.make("section", confidence=0.70, evidence=[Evidence("tag_k_array", 0.70, page=inherited_page or None)])
			for child in value:
				node = self._parse_kid(child, inherited_page, inherited_lang, owner_raw)
				if node is not None:
					container.children.append(node)
			return container if container.children else None
		if not isinstance(value, dict):
			self.warnings.append("TAGGED_KID_UNSUPPORTED:%s" % type(value).__name__)
			return None
		identity = self._identity(raw, value)
		if identity in self._active:
			self.warnings.append("TAGGED_STRUCTURE_CYCLE:%s" % (identity,))
			return None
		self._active.add(identity)
		try:
			type_name = self._name(value.get("Type"))
			page = self._page_number(value.get("Pg")) or inherited_page
			if type_name == "MCR":
				mcid = self._integer(value.get("MCID"))
				stream_raw = value.get("Stm")
				stream = self._resolve(stream_raw)
				parent_key = self._integer(stream.attrs.get("StructParents")) if hasattr(stream, "attrs") else None
				stream_ref = self._ref_text(stream_raw) if stream_raw is not None else ""
				return self._content_ref(
					mcid, page, "MCR", parent_key=parent_key, object_ref=stream_ref or None, owner_raw=owner_raw
				) if mcid is not None else None
			if type_name == "OBJR":
				object_raw = value.get("Obj")
				object_ref = self._ref_text(object_raw)
				object_value = self._resolve(object_raw)
				if not page and isinstance(object_value, dict):
					page = self._page_number(object_value.get("P"))
				struct_parent = self._integer(object_value.get("StructParent")) if isinstance(object_value, dict) else None
				validated = self._parent_tree_owner_matches(struct_parent, owner_raw)
				confidence = 0.995 if validated else 0.94
				if struct_parent is not None and not validated:
					self.warnings.append("TAGGED_PARENTTREE_OBJR_MISMATCH:%s" % object_ref)
				return self.factory.make(
					"annotation",
					confidence=confidence,
					evidence=[Evidence("tag_objr", confidence, page=page or None, data={"parent_tree_validated": validated})],
					sources=[SourceRef(page=page, object_refs=(object_ref,))] if page else [],
					attrs={"object_ref": object_ref, "struct_parent": struct_parent, "parent_tree_validated": validated},
				)
			return self._parse_struct_element(value, page, raw, inherited_lang)
		finally:
			self._active.remove(identity)

	def _parse_struct_element(
		self,
		value: Dict[str, Any],
		page: int,
		raw: Any = None,
		inherited_lang: str = "",
	) -> SemanticNode:
		raw_role = self._name(value.get("S")) or "Unknown"
		namespace = self._resolve(value.get("NS"))
		namespace_name = self._pdf_text(namespace.get("NS")) if isinstance(namespace, dict) else ""
		role = self._resolve_role(raw_role, namespace_name)
		kind = ROLE_TO_KIND.get(role, "unknown")
		attrs: Dict[str, Any] = {"tag_role": role, "raw_tag_role": raw_role}
		if namespace_name:
			attrs["namespace"] = namespace_name
		identifier = self._pdf_text(value.get("ID"))
		if identifier:
			attrs["structure_id"] = identifier
		if role.startswith("H") and role[1:].isdigit():
			attrs["level"] = max(1, min(6, int(role[1:])))
		if role in ("TH", "TD"):
			attrs["cell_role"] = role.lower()
		actual_text = self._pdf_text(value.get("ActualText"))
		alt = self._pdf_text(value.get("Alt"))
		expanded = self._pdf_text(value.get("E"))
		language = self._pdf_text(value.get("Lang")) or inherited_lang
		if actual_text:
			attrs["actual_text"] = True
		if alt:
			attrs["alt"] = alt
		if language:
			attrs["lang"] = language
		if expanded:
			attrs["expanded_text"] = expanded
		attributes = self._collect_attributes(value)
		if attributes:
			attrs["structure_attributes"] = attributes
		node = self.factory.make(
			kind,
			text=actual_text or "",
			attrs=attrs,
			confidence=0.97 if kind != "unknown" else 0.55,
			evidence=[Evidence("tagged_role", 0.97 if kind != "unknown" else 0.55, detail=role, page=page or None)],
		)
		for child in self._as_list(value.get("K")):
			parsed = self._parse_kid(child, page, language, raw)
			if parsed is not None:
				node.children.append(parsed)
		if page and not node.sources:
			node.sources.append(SourceRef(page=page))
		return node

	def _content_ref(
		self,
		mcid: Optional[int],
		page: int,
		source: str,
		parent_key: Optional[int] = None,
		object_ref: Optional[str] = None,
		owner_raw: Any = None,
	) -> Optional[SemanticNode]:
		if mcid is None:
			self.warnings.append("TAGGED_MCID_MISSING")
			return None
		validated = False
		if parent_key is None:
			parent_key = self._page_struct_parents.get(page)
		parent_value = self.parent_tree.get(parent_key) if parent_key is not None else None
		if isinstance(parent_value, list) and 0 <= mcid < len(parent_value):
			entry = parent_value[mcid]
			validated = self._same_object(entry, owner_raw) if owner_raw is not None else self._resolve(entry) is not None
		elif parent_key is not None:
			self.warnings.append("TAGGED_PARENTTREE_MCID_MISSING:p%d:%d" % (page, mcid))
		confidence = 0.995 if validated else 0.94
		source_ref = SourceRef(
			page=page,
			mcids=(mcid,),
			object_refs=(object_ref,) if object_ref else (),
		) if page else None
		return self.factory.make(
			"text",
			attrs={"mcid": mcid, "parent_tree_validated": validated, "struct_parents": parent_key, **({"stream_ref": object_ref} if object_ref else {})},
			confidence=confidence,
			evidence=[Evidence("tagged_%s" % source.lower(), confidence, page=page or None, data={"mcid": mcid, "parent_tree_validated": validated})],
			sources=[source_ref] if source_ref is not None else [],
		)

	def _parent_tree_owner_matches(self, key: Optional[int], owner_raw: Any) -> bool:
		if key is None or owner_raw is None or key not in self.parent_tree:
			return False
		return self._same_object(self.parent_tree.get(key), owner_raw)

	def _same_object(self, left: Any, right: Any) -> bool:
		left_num = getattr(left, "num", None)
		right_num = getattr(right, "num", None)
		if isinstance(left_num, int) and isinstance(right_num, int):
			return (left_num, int(getattr(left, "gen", 0) or 0)) == (right_num, int(getattr(right, "gen", 0) or 0))
		return self._resolve(left) is self._resolve(right)

	def _resolve_role(self, role: str, namespace: str = "") -> str:
		seen: Set[str] = set()
		current = role
		role_map = self.namespace_role_maps.get(namespace, self.role_map) if namespace else self.role_map
		while current in role_map:
			if current in seen:
				self.warnings.append("TAGGED_ROLEMAP_CYCLE:%s" % role)
				return "Unknown"
			seen.add(current)
			current = role_map[current]
		return current

	def _read_role_map(self, raw: Any) -> Dict[str, str]:
		value = self._resolve(raw)
		if not isinstance(value, dict):
			return {}
		return {self._name(key): self._name(self._resolve(item)) for key, item in value.items() if self._name(key) and self._name(self._resolve(item))}

	def _read_class_map(self, raw: Any) -> Dict[str, Any]:
		value = self._resolve(raw)
		if not isinstance(value, dict):
			return {}
		return {self._name(key): self._plain(item) for key, item in value.items()}

	def _read_namespaces(self, raw: Any) -> Dict[str, Dict[str, str]]:
		out: Dict[str, Dict[str, str]] = {}
		for item in self._as_list(raw):
			value = self._resolve(item)
			if not isinstance(value, dict):
				continue
			name = self._pdf_text(value.get("NS"))
			if name:
				out[name] = self._read_role_map(value.get("RoleMap"))
		return out

	def _collect_attributes(self, element: Dict[str, Any]) -> Dict[str, Any]:
		out: Dict[str, Any] = {}
		classes = self._resolve(element.get("C"))
		class_items = classes if isinstance(classes, list) else [classes] if classes is not None else []
		for class_name in class_items:
			# ISO 32000 permits revision numbers to be interleaved with class names.
			# They qualify the preceding entry and are not themselves class keys.
			resolved_name = self._resolve(class_name)
			if isinstance(resolved_name, int) and not isinstance(resolved_name, bool):
				continue
			name = self._name(resolved_name)
			if name:
				self._merge_attribute_value(out, self.class_map.get(name))
		self._merge_attribute_value(out, self._resolve(element.get("A")))
		return out

	def _merge_attribute_value(self, out: Dict[str, Any], value: Any) -> None:
		"""Merge a structure-attribute object or attribute-object array.

		Both /A and ClassMap values may be a dictionary or an array containing
		attribute dictionaries with interleaved revision integers.  Keeping this
		logic in one place prevents legal class-map arrays from being silently
		dropped during tagged-table reconciliation.
		"""
		resolved = self._resolve(value)
		if isinstance(resolved, dict):
			plain = self._plain(resolved)
			if isinstance(plain, dict):
				out.update(plain)
			return
		if isinstance(resolved, list):
			for item in resolved:
				item = self._resolve(item)
				if isinstance(item, int) and not isinstance(item, bool):
					continue
				self._merge_attribute_value(out, item)

	def _read_number_tree(self, raw: Any) -> Dict[int, Any]:
		out: Dict[int, Any] = {}
		active: Set[Tuple[str, int]] = set()

		def walk(node_raw: Any) -> None:
			node = self._resolve(node_raw)
			if not isinstance(node, dict):
				return
			identity = self._identity(node_raw, node)
			if identity in active:
				self.warnings.append("TAGGED_PARENTTREE_CYCLE")
				return
			active.add(identity)
			nums = self._resolve(node.get("Nums"))
			if isinstance(nums, list):
				for index in range(0, len(nums) - 1, 2):
					key = self._integer(nums[index])
					if key is not None:
						out[key] = self._resolve(nums[index + 1])
			for child in self._as_list(node.get("Kids")):
				walk(child)
			active.remove(identity)

		walk(raw)
		return out

	def _page_maps(self) -> Tuple[Dict[Tuple[int, int], int], Dict[int, int], Dict[int, int]]:
		refs: Dict[Tuple[int, int], int] = {}
		direct: Dict[int, int] = {}
		struct_parents: Dict[int, int] = {}
		try:
			pages = self.document.pages()
		except Exception:
			return refs, direct, struct_parents
		for index, page in enumerate(pages, 1):
			direct[id(page)] = index
			page_ref = page.get("__page_ref__") if isinstance(page, dict) else None
			number = getattr(page_ref, "num", None)
			generation = getattr(page_ref, "gen", 0)
			if isinstance(number, int):
				refs[(number, int(generation or 0))] = index
				refs.setdefault((number, 0), index)
			value = self._integer(page.get("StructParents")) if isinstance(page, dict) else None
			if value is not None:
				struct_parents[index] = value
		return refs, direct, struct_parents

	def _page_number(self, raw: Any) -> int:
		number = getattr(raw, "num", None)
		generation = getattr(raw, "gen", 0)
		if isinstance(number, int):
			return self._page_refs.get((number, int(generation or 0)), self._page_refs.get((number, 0), 0))
		page = self._resolve(raw)
		return self._direct_pages.get(id(page), 0)

	def _resolve(self, value: Any) -> Any:
		try:
			return self.document.resolve(value)
		except Exception as exc:
			self.warnings.append("TAGGED_RESOLVE_FAILED:%s" % exc)
			return None

	def _as_list(self, value: Any) -> List[Any]:
		resolved = self._resolve(value)
		if resolved is None:
			return []
		return list(resolved) if isinstance(resolved, list) else [value]

	def _integer(self, value: Any) -> Optional[int]:
		resolved = self._resolve(value)
		return int(resolved) if isinstance(resolved, int) and not isinstance(resolved, bool) else None

	def _identity(self, raw: Any, resolved: Any) -> Tuple[str, int]:
		number = getattr(raw, "num", None)
		generation = getattr(raw, "gen", None)
		if isinstance(number, int):
			return ("ref-%d-%d" % (number, int(generation or 0)), number)
		return ("direct", id(resolved))

	def _ref_text(self, raw: Any) -> str:
		number = getattr(raw, "num", None)
		generation = getattr(raw, "gen", None)
		return "%d %d R" % (number, generation or 0) if isinstance(number, int) else "direct:%d" % id(self._resolve(raw))

	def _name(self, value: Any) -> str:
		return str(value or "").lstrip("/")

	def _pdf_text(self, raw: Any) -> str:
		value = self._resolve(raw)
		if isinstance(value, bytes):
			from ..core import decode_pdf_text

			return decode_pdf_text(value)
		return str(value) if isinstance(value, str) else ""

	def _plain(self, value: Any) -> Any:
		value = self._resolve(value)
		if isinstance(value, dict):
			return {self._name(key): self._plain(item) for key, item in value.items()}
		if isinstance(value, list):
			return [self._plain(item) for item in value]
		if isinstance(value, bytes):
			return self._pdf_text(value)
		if value is None or isinstance(value, (bool, int, float, str)):
			return value
		return str(value)


def parse_tagged_structure(document: Any) -> SemanticDocument:
	return TaggedStructureParser(document).parse()
