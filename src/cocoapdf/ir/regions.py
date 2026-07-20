from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal

from .evidence import Evidence


RegionKind = Literal[
	"body",
	"column",
	"sidebar",
	"figure",
	"caption",
	"table",
	"footnote",
	"endnote",
	"header",
	"footer",
	"callout",
	"equation",
	"unknown",
]


@dataclass(frozen=True)
class Rect:
	x0: float
	y0: float
	x1: float
	y1: float

	@property
	def width(self) -> float:
		return max(0.0, self.x1 - self.x0)

	@property
	def height(self) -> float:
		return max(0.0, self.y1 - self.y0)

	def to_dict(self) -> Dict[str, float]:
		return {
			"x0": round(self.x0, 3),
			"y0": round(self.y0, 3),
			"x1": round(self.x1, 3),
			"y1": round(self.y1, 3),
		}


@dataclass
class Region:
	id: str
	page: int
	kind: RegionKind
	bbox: Rect
	children: List[Any] = field(default_factory=list)
	confidence: float = 0.0
	evidence: List[Evidence] = field(default_factory=list)
	reading_order_index: int = 0

	def to_dict(self) -> Dict[str, Any]:
		return {
			"id": self.id,
			"page": self.page,
			"kind": self.kind,
			"bbox": self.bbox.to_dict(),
			"children": self.children,
			"confidence": round(float(self.confidence), 4),
			"evidence": [ev.to_dict() for ev in self.evidence],
			"reading_order_index": self.reading_order_index,
		}
