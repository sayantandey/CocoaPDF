from __future__ import annotations

from typing import Any, Dict, List


def explain_report(report: Dict[str, Any]) -> List[str]:
	lines = ["%s %s" % (report.get("tool", "CocoaPDF"), report.get("version", ""))]
	for region in report.get("regions") or []:
		evidence = ", ".join(ev.get("kind", "") for ev in region.get("evidence", []))
		lines.append(
			"%s page=%s kind=%s confidence=%.2f evidence=%s"
			% (region.get("id"), region.get("page"), region.get("kind"), float(region.get("confidence", 0.0)), evidence)
		)
	return lines
