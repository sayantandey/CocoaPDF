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
    checkout = output_dir.parent / "cocoapdf-main"
    if checkout.exists():
        shutil.rmtree(checkout)
    subprocess.run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            "main",
            "https://github.com/sayantandey/CocoaPDF.git",
            str(checkout),
        ],
        check=True,
    )
    for name in ("src", "tests", "scripts", "validation"):
        shutil.copytree(checkout / name, output_dir / name)
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(checkout / name, output_dir / name)
    commit = subprocess.check_output(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True
    ).strip()
    (output_dir / "source-commit.txt").write_text(commit + "\n", encoding="utf-8")
    (output_dir / "review.html").write_text(
        "<!doctype html><title>CocoaPDF main source export</title>", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
