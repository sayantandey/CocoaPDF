from __future__ import annotations


def is_sidebar_candidate(region: object, page_width: float) -> bool:
	bbox = getattr(region, "bbox", None)
	if bbox is None:
		return False
	return bbox.width < page_width * 0.33 and (bbox.x0 < page_width * 0.12 or bbox.x1 > page_width * 0.88)
