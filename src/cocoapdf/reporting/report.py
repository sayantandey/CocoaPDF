from __future__ import annotations

from typing import Any, Dict

from .._version import __version__
from ..ir.semantic import SemanticDocument


def build_summary(report: Dict[str, Any]) -> Dict[str, Any]:
	regions = report.get("regions") or []
	return {
		"tool": report.get("tool", "CocoaPDF"),
		"version": report.get("version", __version__),
		"pages": report.get("pages", 0),
		"warnings": len(report.get("warnings") or []),
		"regions": len(regions),
		"region_kinds": sorted({region.get("kind", "unknown") for region in regions if isinstance(region, dict)}),
	}


def attach_semantic_document(
	report: Dict[str, Any],
	document: SemanticDocument,
	require_provenance: bool = True,
) -> Dict[str, Any]:
	errors = document.validate(require_provenance=require_provenance)
	report["semantic_schema"] = "cocoapdf.semantic-document/v%s" % document.version
	report["semantic_valid"] = not errors
	report["semantic_errors"] = errors
	report["semantic_document"] = document.to_dict()
	semantic_nodes = [node.to_dict() for node in document.walk()]
	report["semantic_nodes"] = semantic_nodes
	report["semantic_node_count"] = len(semantic_nodes)
	report["semantic_kinds"] = sorted({node["kind"] for node in semantic_nodes})
	# Region nodes predate the semantic graph and remain part of the public
	# explainability contract. Preserve them instead of silently replacing them.
	report["nodes"] = list(report.get("nodes") or []) + semantic_nodes
	if errors:
		report.setdefault("warnings", []).append({
			"code": "SEMANTIC_GRAPH_INVALID",
			"detail": "; ".join(errors[:8]),
		})
	return report
