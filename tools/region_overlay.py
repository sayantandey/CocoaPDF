from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
	sys.path.insert(0, str(SRC))

from cocoapdf.tools import region_overlay_svg


def main() -> int:
	parser = argparse.ArgumentParser(description="Generate a CocoaPDF region overlay SVG.")
	parser.add_argument("pdf")
	parser.add_argument("--page", type=int, default=1)
	parser.add_argument("-o", "--output")
	args = parser.parse_args()
	out = Path(args.output) if args.output else Path("overlay-page-%d.svg" % args.page)
	out.parent.mkdir(parents=True, exist_ok=True)
	out.write_text(region_overlay_svg(Path(args.pdf), args.page), encoding="utf-8")
	print(out)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
