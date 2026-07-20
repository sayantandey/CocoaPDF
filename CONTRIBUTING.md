# Contributing to CocoaPDF

Open focused pull requests against `main`. Use `fix/`, `bugfix/`, `hotfix/`, or `patch/` branches for compatible corrections; other branches default to a minor release. The `release:patch` and `release:minor` labels override branch/title inference and are mutually exclusive.

Major releases are deliberately manual: increment the integer in `VERSION_MAJOR` by exactly one and apply the `breaking` label. Automation then releases `MAJOR.0.0`. Never edit generated minor or patch numbers in source files.

Before requesting review, run:

```bash
python -m pip install -e .
python scripts/check_repository_invariants.py
python -m unittest discover -s tests -v
python -m compileall -q src tests tools scripts
```

Changes to semantic detection should include positive evidence, an adversarial near-miss, provenance/confidence assertions where applicable, and a full regression run. Do not add producer- or fixture-specific shortcuts.
