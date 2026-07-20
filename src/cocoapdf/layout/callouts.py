from __future__ import annotations


def is_callout_fill(fill: object) -> bool:
	width = getattr(fill, "x1", 0) - getattr(fill, "x0", 0)
	height = getattr(fill, "y1", 0) - getattr(fill, "y0", 0)
	color = getattr(fill, "color", (1.0, 1.0, 1.0))
	return width >= 80 and height >= 18 and max(color) - min(color) <= 0.18
