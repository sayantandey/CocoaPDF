from __future__ import annotations

from typing import Dict


def asset_manifest(assets: Dict[str, bytes]) -> Dict[str, int]:
	return {name: len(data) for name, data in sorted(assets.items())}
