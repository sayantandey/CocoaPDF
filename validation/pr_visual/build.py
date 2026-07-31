from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--source-commit")
    parser.add_argument("--head-commit")
    args = parser.parse_args()

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "python",
            "-m",
            "pip",
            "download",
            "--no-deps",
            "--dest",
            str(output_dir),
            "apted==1.0.3",
        ],
        check=True,
    )
    source_assets = Path(__file__).resolve().parents[2] / "src" / "cocoapdf" / "assets"
    shutil.copytree(source_assets, output_dir / "assets")
    (output_dir / "review.html").write_text(
        "<!doctype html><title>Benchmark dependencies export</title>", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
