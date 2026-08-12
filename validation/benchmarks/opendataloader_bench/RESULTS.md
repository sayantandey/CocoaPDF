# Pinned OpenDataLoader / DP-Bench results

These numbers come from the unmodified official evaluator over all 200
documents. The benchmark checkout was clean at
`7af1d8f4d0c09f51ea1a5c6ba5f66e993286d109`; the only integration changes are
the archived CocoaPDF registry entry, direct dependency pin, license notice,
and byte-identical canonical adapter.

- CocoaPDF commit: `59a544a3cfc6e94e72dce4f22f2b334819c818e8`
- CocoaPDF tree: `4ff2dda282ed2e530478851e1a4dc1b8c04c853a`
- Corpus: 200 PDFs stored as ordinary Git blobs (~36 MB), all with valid
  `%PDF-` headers
- Eligibility: NID 200, TEDS 42, MHS 107
- Completeness: 200 predictions, 0 missing, 0 empty, 0 conversion failures
- Evaluator tests: 31 passed in the locked Python 3.13 environment

## Final scores

| Metric | Previous published result | Final | Change | Enforced floor |
| --- | ---: | ---: | ---: | ---: |
| Overall document-macro | `0.8696657214` | **`0.9020490607`** | `+0.0323833394` | `0.80` |
| NID | `0.8993297820` | **`0.9086028983`** | `+0.0092731163` | `0.80` |
| NID-S | `0.8822899268` | **`0.8897361669`** | `+0.0074462401` | — |
| TEDS | `0.8061841234` | **`0.9251323351`** | `+0.1189482117` | `0.80` |
| TEDS-S | `0.8110160459` | **`0.9300636650`** | `+0.1190476190` | — |
| MHS | `0.8062168834` | **`0.8791989022`** | `+0.0729820188` | `0.80` |
| MHS-S | `0.8889792725` | **`0.9415827438`** | `+0.0526034713` | — |

`overall_mean` is the mean of each document's available metrics, not the mean
of the three aggregate metric means. TEDS concatenates every extracted table
into one synthetic comparison for an eligible document. MHS flattens heading
levels, so it measures heading boundaries and text rather than true hierarchy
depth. These metrics evaluate Markdown, not CocoaPDF's independent HTML output.

## Determinism and timing

The complete conversion/evaluation command ran twice against the same locked
git installation. All 200 Markdown files matched by name, size, and SHA-256;
all aggregate and per-document scores also matched.

| Run | Total conversion time | Average seconds/page | Prediction digest |
| --- | ---: | ---: | --- |
| 1 | `172.0491046906 s` | `0.8602455235 s/page` | `baf27effd492430710ba4ef67d6dc483cef6f4e0562bf6881dc8e0e1eccf4971` |
| 2 | `204.0852847099 s` | `1.0204264235 s/page` | `baf27effd492430710ba4ef67d6dc483cef6f4e0562bf6881dc8e0e1eccf4971` |

Host: Intel Core i5-10300H, 25,592,647,680 physical bytes. Timing is
hardware-bound and includes CocoaPDF's semantic HTML generation even though the
accuracy evaluator scores Markdown only. The pinned corpus has 200 one-page
PDFs, so each value is the total conversion time divided by 200 audited pages
bound to the pinned corpus inventory. Run 1 is the canonical speed observation;
run 2 exists to verify
determinism and is not combined with it into a range.

## Target status and remaining limits

The clean document-macro result exceeds the `0.90` campaign target by
`0.0020490607`. Remaining low-scoring cases are concentrated in content that
the no-OCR contract intentionally cannot recognize (raster-only tables or
headings and lettering drawn as vector outlines), plus a small number of
recoverable complex reading-order and heading-boundary layouts. Future changes
must keep the same evidence-driven admission rules and adversarial near-misses;
in particular, the higher table score must not be traded for false tables on
ground-truth-ineligible documents.

## Evidence

The immutable snapshot under `results/7af1d8f4…/` contains exact evaluation
JSON/CSV, completeness, both timing/hash records, the per-document prediction
hash inventory, adapter, minimal integration patch, and full provenance. It
does not redistribute source PDFs, ground truth, or predicted Markdown.
