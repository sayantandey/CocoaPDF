from __future__ import annotations

from typing import Iterable, List


def physical_float_order(regions: Iterable[object]) -> List[object]:
	return sorted(regions, key=lambda r: (getattr(r, "page", 0), getattr(getattr(r, "bbox", None), "y0", 0), getattr(getattr(r, "bbox", None), "x0", 0)))
