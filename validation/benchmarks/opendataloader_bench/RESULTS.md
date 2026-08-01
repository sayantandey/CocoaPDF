# Pinned OpenDataLoader / DP-Bench results

These numbers come from the unmodified official evaluator over all 200
documents. The benchmark checkout was clean at
`7af1d8f4d0c09f51ea1a5c6ba5f66e993286d109`; the only integration changes are
the archived CocoaPDF registry entry, direct dependency pin, license notice,
and byte-identical canonical adapter.

- CocoaPDF commit: `937c403ed3b265a14db802b2ced36b3819d20b0f`
- CocoaPDF tree: `f01a809b4e4c48d17716be2ac510e67b66b6ff28`
- Corpus: 200 PDFs stored as ordinary Git blobs (~36 MB), all with valid
  `%PDF-` headers
- Eligibility: NID 200, TEDS 42, MHS 107
- Completeness: 200 predictions, 0 missing, 0 empty, 0 conversion failures
- Evaluator tests: 31 passed in the locked Python 3.13 environment

## Final scores

| Metric | Previous published result | Final | Change | Enforced floor |
| --- | ---: | ---: | ---: | ---: |
| Overall document-macro | `0.8435980876` | **`0.8696657214`** | `+0.0260676337` | `0.80` |
| NID | `0.8908832818` | **`0.8993297820`** | `+0.0084465002` | `0.80` |
| NID-S | `0.8704161815` | **`0.8822899268`** | `+0.0118737453` | — |
| TEDS | `0.5636823455` | **`0.8061841234`** | `+0.2425017778` | `0.80` |
| TEDS-S | `0.5759866978` | **`0.8110160459`** | `+0.2350293481` | — |
| MHS | `0.7912284341` | **`0.8062168834`** | `+0.0149884493` | `0.80` |
| MHS-S | `0.8810292786` | **`0.8889792725`** | `+0.0079499939` | — |

`overall_mean` is the mean of each document's available metrics, not the mean
of the three aggregate metric means. TEDS concatenates every extracted table
into one synthetic comparison for an eligible document. MHS flattens heading
levels, so it measures heading boundaries and text rather than true hierarchy
depth. These metrics evaluate Markdown, not CocoaPDF's independent HTML output.

## Determinism and timing

The complete conversion/evaluation command ran twice against the same locked
git installation. All 200 Markdown files matched by name, size, and SHA-256;
all aggregate and per-document scores also matched.

| Run | Total conversion time | Per document | Prediction digest |
| --- | ---: | ---: | --- |
| 1 | `175.0555016994 s` | `0.8752775085 s` | `f391c951c935cefac6ffc64dd15a267a9de8292e9c35a36c094732588774730a` |
| 2 | `159.0534393787 s` | `0.7952671969 s` | `f391c951c935cefac6ffc64dd15a267a9de8292e9c35a36c094732588774730a` |

Host: Intel Core i5-10300H, 25,592,647,680 physical bytes. Timing is
hardware-bound and includes CocoaPDF's semantic HTML generation even though the
accuracy evaluator scores Markdown only.

## Why the score is not yet 0.90

The remaining gap is concentrated rather than broad:

1. NID is only `0.0006702180` below `0.90`, but a few severe reading-order
   outliers dominate; document 141 has NID `0.0059447983`.
2. Seven of 42 TEDS-eligible documents still score exactly zero: 110, 119, 122,
   146, 150, 165, and 166. Recovering their producer-specific fill/booktabs
   structures is the largest remaining table opportunity.
3. Three of 107 MHS-eligible documents score exactly zero: 036, 141, and 148;
   these need reading-order repair before broader heading admission.
4. TEDS and MHS now clear `0.80` but only narrowly, so held-out adversarial
   fixtures must accompany every new detector to prevent false-positive gains.
5. The document-macro overall score gives low multi-metric outliers extra
   leverage; improving the shared reading-order failures helps NID, MHS, and
   table recovery together more than output-only normalization would.

## Evidence

The immutable snapshot under `results/7af1d8f4…/` contains exact evaluation
JSON/CSV, completeness, both timing/hash records, the per-document prediction
hash inventory, adapter, minimal integration patch, and full provenance. It
does not redistribute source PDFs, ground truth, or predicted Markdown.
