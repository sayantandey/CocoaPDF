from __future__ import annotations

from typing import Any, Dict, Optional, Tuple


def walk_xrefs(
	document: Any,
	start: int,
) -> Tuple[Dict[Tuple[int, int], Any], Dict[str, Any]]:
	"""Walk newest-to-oldest xref sections.

	Object number, not ``(object, generation)``, is the shadowing identity.
	A newer free entry must therefore suppress an older in-use generation.
	Hybrid xref-stream entries override the compatibility table for their
	object number within the same revision.
	"""
	entries: Dict[Tuple[int, int], Any] = {}
	claimed_object_numbers: set[int] = set()
	trailer: Dict[str, Any] = {}
	current: Optional[int] = start
	seen: set[int] = set()
	while current is not None:
		if current in seen:
			document.warn("XREF_PREV_CYCLE", "offset %d" % current)
			break
		seen.add(current)
		section_entries, section_trailer = document._parse_xref_at(current)
		# Collapse a revision by object number.  The hybrid xref stream is the
		# authoritative source for compressed entries in that revision.
		merged_by_number: Dict[int, Tuple[Tuple[int, int], Any]] = {}
		for key, entry in section_entries.items():
			merged_by_number[key[0]] = (key, entry)
		for key, value in section_trailer.items():
			trailer.setdefault(key, value)
		hybrid = section_trailer.get("XRefStm")
		if isinstance(hybrid, int) and hybrid not in seen:
			stream_entries, stream_trailer = document._parse_xref_at(hybrid)
			for key, entry in stream_entries.items():
				merged_by_number[key[0]] = (key, entry)
			for key, value in stream_trailer.items():
				trailer.setdefault(key, value)
		for object_number, (key, entry) in merged_by_number.items():
			if object_number in claimed_object_numbers:
				continue
			entries[key] = entry
			claimed_object_numbers.add(object_number)
		previous = section_trailer.get("Prev")
		current = previous if isinstance(previous, int) else None
	return entries, trailer
