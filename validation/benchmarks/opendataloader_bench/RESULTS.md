# Measured OpenDataLoader / DP-Bench results

All numbers below were produced by the **unmodified official evaluator** over the
complete 200-document corpus. Ground truth, evaluator formulas, eligibility
rules, and document lists were not touched.

- Benchmark commit: `7af1d8f4d0c09f51ea1a5c6ba5f66e993286d109`
- CocoaPDF base commit: `7f0288807620131634eabed49bc78dc1adc37383` (working tree
  dirty; the measured build is the uncommitted tree)
- Corpus: 200 PDFs, real Git LFS payloads (~36 MB), verified `%PDF` headers
- Conversion failures: **0**; missing predictions: **0**

## Before and after

"Before" is the working tree as inherited (the half-complete predecessor work),
converted with stock `ConvertOptions()`. "After" is the same corpus through the
canonical adapter with the changes in this branch.

| Metric | Archived provenance | Before (measured) | After (measured) | Floor |
| --- | ---: | ---: | ---: | ---: |
| overall | 0.6621 | 0.8098 | **0.8436** | 0.80 ✅ |
| nid | 0.7753 | 0.8566 | **0.8909** | 0.80 ✅ |
| nid_s | 0.7514 | 0.8330 | **0.8704** | 0.80 ✅ |
| teds | 0.2135 | 0.5381 | **0.5637** | 0.80 ❌ |
| teds_s | 0.2199 | 0.5529 | **0.5760** | 0.80 ❌ |
| mhs | 0.4867 | 0.7513 | **0.7912** | 0.80 ❌ |
| mhs_s | 0.5936 | 0.8630 | **0.8810** | 0.80 ✅ |

Eligible-document counts (unchanged, they are a property of the ground truth):
`nid_count=200`, `teds_count=42`, `mhs_count=107`.

The archived 0.6621 result predates the inherited work; it is provenance only.
The honest baseline for judging this branch is the "Before" column.

## Corrections to `cocoapdf_odl_accuracy_analysis.md`

The supplied analysis was written against a flattened snapshot and contains
claims that measurement disproves:

1. **"Flat heading projection improves the heading metric."** It cannot.
   `evaluator_heading_level._parse_markdown_structure` appends every heading to
   the tree root and never reads the `#{1,6}` capture group, so MHS is
   level-blind by construction. Flat projection is still worth applying, but
   because `#` characters are part of the NID string, not because of MHS.
2. **"18 failures, 5 errors, 304 tests."** That was an artifact of the flattened
   upload. The complete checkout ran 317 tests with a single pre-existing
   failure before any change here, and 325 tests green after.
3. **"A full-corpus rerun is not possible in this environment."** It is. The
   corpus, LFS payloads, `uv`, and the evaluator all work locally; the only
   obstacle was a TLS trust issue, resolved with `--system-certs`.

## Harness verification

Commit `7f028880` was re-run from a clean `git worktree` and reproduced the
archived numbers to every decimal place (`overall 0.6621265231177883`,
`teds 0.21354055439317537`, `mhs 0.48667938505567837`). The harness is therefore
deterministic, and `examples/README.md` is correct provenance for that commit
rather than stale data. Every improvement recorded here is uncommitted, which is
why it must not be published under that commit's identifier.

Per-document deltas, baseline to final: NID improved on 167 documents and
regressed on 10 (all small, none caused by a spurious table); TEDS improved on
12 and regressed on **0**; MHS improved on 77 and regressed on 11. Table counts
are unchanged in every regressed document, so the new detector introduced no
false-positive tables anywhere in the corpus.

## Remaining gap

TEDS and MHS are below the acceptance floor. The dominant cause is precise and
measurable: **14 of the 42 TEDS-eligible documents still score exactly 0.0**
because CocoaPDF emits no table at all for them, while the ground truth has one.
Their geometry splits into three families:

| Family | Documents | Evidence available |
| --- | --- | --- |
| No rules, no fills (borderless) | 121, 122, 132, 178, 180 | text alignment only |
| Fill-derived cell rectangles | 110, 119, 146, 150, 165, 166 | fill bands, no rules |
| Rules present but unused | 187, 197, 200 | booktabs rules; 187 additionally collapses its rows into one line before table detection can run |

Because TEDS is averaged over only 42 documents, each of those 14 is worth
0.0238 of the metric. Recovering them at even 0.75 average quality would move
TEDS to roughly 0.82; that is the concrete path to the floor, and it is the work
that remains.
