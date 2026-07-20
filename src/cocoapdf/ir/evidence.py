from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class Evidence:
	kind: str
	confidence: float
	detail: str = ""
	page: Optional[int] = None
	data: Dict[str, Any] = field(default_factory=dict)

	def to_dict(self) -> Dict[str, Any]:
		return {
			"kind": self.kind,
			"confidence": round(float(self.confidence), 4),
			"detail": self.detail,
			"page": self.page,
			"data": self.data,
		}
