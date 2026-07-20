<div align="center">

<img src="docs/assets/brand/logo/cocoapdf-mark-transparent-256x256.png" alt="CocoaPDF document-and-cocoa-bean mark" width="132">

# CocoaPDF

### Turn structured PDFs into accurate, editable documents—without OCR or AI.

CocoaPDF recovers the text, layout, tables, links, notes, forms, and images already present inside a PDF, then rebuilds them as readable **Markdown**, structured **HTML**, and detailed **JSON**.

<br>

[![Download for Windows](https://img.shields.io/badge/Windows-Download-7A4E2D?style=for-the-badge\&logo=windows\&logoColor=white)][download-windows]
[![Download for Linux](https://img.shields.io/badge/Linux-Download-8B5E3C?style=for-the-badge\&logo=linux\&logoColor=white)][download-linux]
[![Download for macOS](https://img.shields.io/badge/macOS-Download-A06A42?style=for-the-badge\&logo=apple\&logoColor=white)][download-macos]

<br>

![MIT License](docs/assets/brand/badges/cocoapdf-badge-license-mit.svg)
![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-795548?style=flat-square\&logo=python\&logoColor=white)
![Standard-library runtime](docs/assets/brand/badges/cocoapdf-badge-runtime-stdlib.svg)
![No OCR](docs/assets/brand/badges/cocoapdf-badge-no-ocr.svg)
![No AI](docs/assets/brand/badges/cocoapdf-badge-no-ai.svg)
![Markdown and HTML](docs/assets/brand/badges/cocoapdf-badge-output-md-html.svg)

</div>

[download-windows]: https://github.com/sayantandey/CocoaPDF/releases/latest/download/cocoapdf-windows-x86_64.zip
[download-linux]: https://github.com/sayantandey/CocoaPDF/releases/latest/download/cocoapdf-linux-x86_64.tar.gz
[download-macos]: https://github.com/sayantandey/CocoaPDF/releases/latest/download/cocoapdf-macos.tar.gz
[download-checksums]: https://github.com/sayantandey/CocoaPDF/releases/latest/download/SHA256SUMS.txt
[download-manifest]: https://github.com/sayantandey/CocoaPDF/releases/latest/download/RELEASE.json

---

CocoaPDF converts digitally born, structured text-layer PDFs into semantic Markdown, loss-aware HTML, and provenance-rich JSON. It parses PDF bytes directly with Python's standard library, reconciles geometry with Tagged-PDF structure, and preserves every accepted node's source pages, regions, glyphs, MCIDs, confidence, evidence, and warnings. It does not use OCR, AI, or runtime PDF frameworks; raster images are preserved as images with their PDF placement, dimensions, alignment, links, captions, and alternative text.

## At a glance

|                  |                                                                                                    |
| ---------------- | -------------------------------------------------------------------------------------------------- |
| **Designed for** | Digitally created PDFs with selectable text                                                        |
| **Produces**     | Markdown, structured HTML, semantic JSON, reports, and extracted assets                            |
| **Recovers**     | Headings, paragraphs, lists, tables, figures, links, notes, references, forms, and reading order   |
| **Preserves**    | Images, dimensions, placement, alignment, captions, links, alternative text, and source provenance |
| **Runtime**      | Python’s standard library only                                                                     |
| **Does not use** | OCR, AI, machine learning, or runtime PDF frameworks                                               |
| **License**      | MIT                                                                                                |

### Navigation

[How it works](#how-it-works) ·
[Capabilities](#capabilities) ·
[Installation](#installation) ·
[Usage](#usage) ·
[Python API](#python-api) ·
[Diagnostics](#diagnostics-and-explainability) ·
[Development](#development-and-verification) ·
[License](#license)

---

## Why CocoaPDF?

Most PDF text extractors copy characters in roughly visual order. CocoaPDF instead tries to recover the **meaning and structure** of the document.

It distinguishes headings from paragraphs, lists from indented prose, tables from page columns, captions from body text, and page furniture from real content. When Markdown cannot represent a structure safely, CocoaPDF uses a controlled HTML fallback rather than flattening or inventing content.

Every accepted semantic element can retain evidence linking it back to its source page, region, glyphs, marked-content identifiers, PDF objects, confidence, and warnings.

> [!IMPORTANT]
> CocoaPDF is built for **digitally born PDFs with a usable text layer**. It does not read text from scans, screenshots, photographs, or raster images.

---

## How it works

Every output is generated from one reconciled semantic document graph:

```text
PDF bytes and operators
        │
        ▼
COS objects, streams, resources, fonts, glyphs,
graphics, images, annotations, forms, and tags
        │
        ▼
Normalized source representation
        │
        ▼
Layout, regions, reading order, and page structure
        │
        ▼
Tagged-PDF and geometric reconciliation
        │
        ▼
Authoritative semantic document graph
        ├── Markdown
        ├── HTML
        ├── semantic JSON
        └── report and assets
```

The graph may contain:

* headings and paragraphs;
* inline styles and links;
* ordered, unordered, and nested lists;
* quotations and code;
* tables, cells, captions, and notes;
* figures and preserved image assets;
* footnotes and endnotes;
* references, citations, and cross-references;
* outlines and table-of-contents entries;
* form fields and widgets;
* anchors and explicit page boundaries.

Markdown remains the preferred output. Structured HTML is used when Markdown cannot safely express table spans, nested cell content, vertical writing, dimensions, alignment, or other document semantics.

---

## Scope and safety policy

CocoaPDF operates directly on the information already encoded in the PDF.

### Supported document class

CocoaPDF is intended for:

* reports and manuals;
* academic and technical documents;
* business and financial documents;
* exported office documents;
* tagged and accessible PDFs;
* forms with existing field values;
* documents containing tables, figures, notes, references, and multiple columns.

### Deliberately excluded

CocoaPDF does not:

* perform OCR;
* infer text from image pixels;
* use AI or machine-learning models;
* execute JavaScript, form actions, launch actions, or embedded programs;
* invent missing Unicode characters, cells, destinations, labels, or form values;
* submit, reset, calculate, or validate PDF forms.

Raster images are preserved as images. Their placement, dimensions, alignment, links, captions, and alternative text are retained when available.

Encrypted PDFs are refused unless their contents can be validated safely. Unsupported or malformed constructs produce warnings and conservative fallbacks rather than silent fabrication.

---

## Capabilities

### Semantic document recovery

CocoaPDF can reconstruct:

* paragraphs across visually wrapped PDF lines;
* hard and soft line breaks;
* hyphenated line wraps;
* heading levels and numbered headings;
* bold, italic, monospace, underline, strike, highlight, superscript, and subscript evidence;
* inline and fenced code;
* block quotations;
* horizontal separators;
* ordered, unordered, mixed, and nested lists;
* multiple columns, sidebars, callouts, figures, tables, and footnote regions;
* repeated headers, footers, logos, page numbers, and other page furniture.

Reading order is determined from page geometry, regions, tags, and source evidence rather than raw object order alone.

### Tables

CocoaPDF supports both ruled and carefully accepted borderless tables.

Capabilities include:

* grid and lattice detection;
* precision-gated borderless table detection;
* conservative missing-border span inference;
* rowspans and colspans;
* rotated headers;
* multiline and nested cell content;
* typed table captions and notes;
* cell alignment;
* per-cell provenance, evidence, warnings, and confidence;
* guarded continuation across page boundaries;
* GFM output for simple tables;
* structured HTML fallback for tables Markdown cannot represent faithfully.

Article columns, bibliographies, aligned prose, and other table-like layouts are rejected when the evidence is insufficient.

### Figures and images

CocoaPDF preserves raster images without attempting to read text from them.

It can retain:

* the original image bytes or an embedded data URI;
* PDF placement quads;
* displayed width and height;
* page alignment;
* image links;
* figure captions;
* Tagged-PDF alternative text;
* source-page and object provenance;
* repeated-asset deduplication.

A conservative vector-to-SVG approximation is available for supported vector figures.

### Notes, references, and navigation

CocoaPDF can reconstruct:

* footnote and endnote references;
* note definitions and continuation blocks;
* reference and bibliography sections;
* citations;
* references to figures, tables, sections, equations, appendices, and notes;
* PDF outlines and bookmarks;
* visible tables of contents;
* named and direct destinations;
* anchors and internal links.

Targets are linked only when resolution is sufficiently reliable. Unresolved references remain readable text with diagnostic metadata.

### AcroForm semantics

CocoaPDF reads AcroForm field trees without executing field actions.

Supported field semantics include:

* text fields;
* multiline values;
* choice fields;
* selected options;
* checkboxes;
* radio buttons;
* push-button identification;
* signature fields;
* inherited field attributes;
* widget provenance;
* password-value redaction.

CocoaPDF never submits, resets, calculates, validates, imports, launches, or executes a form action.

---

### Tagged PDF support

Tagged-PDF information is treated as a strong semantic prior and checked against page geometry and marked-content ownership.

Supported structures include:

* `StructTreeRoot`;
* global and namespace `RoleMap`;
* `ClassMap`;
* `ParentTree`;
* `StructParents` and `StructParent`;
* MCIDs;
* MCR and OBJR references;
* `/Pg` and `/Stm`;
* `/ActualText`;
* `/Alt`;
* `/E`;
* `/Lang`;
* artifact markers;
* structure attributes;
* list numbering;
* table row and column spans.

Reconciliation can materialize tagged:

* headings;
* paragraphs;
* lists;
* tables;
* figures;
* captions;
* links;
* TOC entries;
* notes;
* artifacts.

Broken or incomplete tag trees fall back to geometric reconstruction rather than overriding credible page evidence.

---

### PDF and text foundation

<details>
<summary><strong>PDF object, stream, graphics, and font support</strong></summary>

#### PDF structure

* Classic cross-reference tables.
* Cross-reference streams.
* Hybrid and incremental chains.
* Object streams.
* Indirect stream lengths.
* Page trees and inherited resources.
* Content arrays.
* Form XObjects.
* Stream-aware malformed-file recovery.
* Configurable object, recursion, decompression, glyph, path, and image limits.

#### Stream filters

* Flate.
* ASCII85.
* ASCIIHex.
* RunLength.
* LZW.
* Pass-through handling for supported image filters.

#### Content and graphics

* Text-state operators.
* Geometry and transformation matrices.
* Paths and painted rectangles.
* Clipping bounds.
* Graphics-state alpha.
* Images and image masks where safely representable.
* Form recursion protection.
* Link and non-link annotation metadata.
* Marked-content properties.
* Inline images.

#### Fonts and Unicode

* Standard-14 fonts and metrics.
* PDFDocEncoding.
* WinAnsi and Differences encodings.
* ToUnicode CMaps.
* Composite CID fonts.
* Selected predefined Unicode CMaps.
* Ligature normalization.
* Vertical `DW2` and `W2` metrics.
* Geometry-derived spacing.
* `TJ` displacements.
* Duplicate and faux-bold suppression.
* Invisible-text handling.
* Unicode bidirectional reordering.

</details>

---

### Bidirectional and vertical text

CocoaPDF includes source-preserving bidirectional processing for mixed left-to-right and right-to-left text.

The implementation handles:

* paragraph direction;
* explicit embeddings and overrides;
* directional isolates;
* weak and neutral resolution;
* paired-bracket behavior;
* implicit embedding levels;
* visual line reordering.

Vertical writing support includes:

* vertical CMaps;
* vertical glyph origins and displacements;
* `DW2` and `W2` metrics;
* vertical `TJ` movement;
* vertical geometry;
* loss-aware HTML using vertical writing modes.

The repository includes a checker for the official Unicode `BidiCharacterTest.txt` and `BidiTest.txt` corpus formats.

---

## Outputs

### Markdown

Markdown output is CommonMark-oriented and uses GFM-compatible tables where appropriate.

Generated HTML is inserted only where Markdown cannot safely preserve the original structure, such as:

* rowspan or colspan tables;
* nested cell content;
* vertical text;
* image sizing and alignment;
* complex figures;
* unsupported native Markdown semantics.

### HTML

HTML is emitted directly from the semantic graph rather than being reconstructed from Markdown.

This preserves:

* typed sections and headings;
* semantic lists;
* table structure;
* cell spans;
* captions and notes;
* figures;
* form semantics;
* anchors and internal navigation;
* vertical writing;
* source and diagnostic metadata where configured.

### JSON and reports

Semantic JSON exposes the document graph in a machine-readable form.

Reports can include:

* semantic nodes;
* source references;
* source pages and regions;
* glyph identifiers;
* MCIDs;
* PDF object references;
* bounding boxes;
* confidence;
* evidence;
* warnings;
* page processing modes;
* extracted assets;
* graph-validation results;
* low-confidence decisions.

For image nodes, the report explicitly records:

```json
{
  "ocr_used": false,
  "text_extraction_attempted": false
}
```

---

## Installation

### Native downloads

Use the platform buttons at the top of this README to download the latest published release.

The published release packages are:

```text
cocoapdf-windows-x86_64.zip
cocoapdf-linux-x86_64.tar.gz
cocoapdf-macos.tar.gz
```

The macOS package contains separate Apple Silicon and Intel executables.
The Linux x86_64 binary uses an Ubuntu 22.04 build baseline for broader glibc compatibility.

Before execution, verify the package against [SHA256SUMS.txt][download-checksums] and inspect the provenance in [RELEASE.json][download-manifest]. Each archive also carries the CocoaPDF brand asset manifest, project license and notice, platform icon, and per-binary provenance manifest. The Windows executable embeds the CocoaPDF icon and product/version metadata.

### Install from source

CocoaPDF requires Python 3.9 or later and has no runtime dependencies.

```bash
python -m pip install .
```

#### Run from the repository

Linux and macOS:

```bash
export PYTHONPATH=src
python -m cocoapdf.cli input.pdf
```

Windows PowerShell:

```powershell
$env:PYTHONPATH = "src"
python -m cocoapdf.cli input.pdf
```

The repository launcher is equivalent:

```bash
python run_cocoapdf.py input.pdf
```

---

## Usage

### Common conversions

```bash
# Convert to Markdown and print to stdout
cocoapdf input.pdf

# Write Markdown, extracted assets, and a diagnostic report
cocoapdf input.pdf \
  -o document.md \
  --assets assets \
  --report report.json

# Produce semantic HTML
cocoapdf input.pdf \
  --format html \
  -o document.html

# Produce a Markdown, HTML, JSON, and report package
cocoapdf input.pdf \
  --format both \
  -o output

# Produce a JSON envelope containing the graph, report, Markdown, and HTML
cocoapdf input.pdf \
  --format json \
  -o result.json

# Convert selected pages and preserve explicit page boundaries
cocoapdf input.pdf \
  --pages 1,3-5 \
  --page-breaks \
  -o excerpt.md
```

When `--format both` targets a directory, CocoaPDF writes:

```text
output/
├── document.md
├── document.html
├── document.json
└── report.json
```

Referenced assets are written to the directory supplied through `--assets`.

---

### Image handling

```bash
# Extract image files and reference them from the output
cocoapdf input.pdf \
  --image-mode reference \
  --assets assets

# Embed image bytes as data URIs
cocoapdf input.pdf \
  --image-mode embed

# Preserve dimensions and alignment when Markdown is insufficient
cocoapdf input.pdf \
  --image-markup auto

# Force dimensionless Markdown image syntax
cocoapdf input.pdf \
  --image-markup markdown

# Force generated HTML figure and image markup
cocoapdf input.pdf \
  --image-markup html
```

`--image-markup auto` is the default. It emits controlled HTML when dimensions, alignment, captions, placement, or links would otherwise be lost.

---

## Python API

```python
from cocoapdf import ConvertOptions, convert_file

result = convert_file(
    "input.pdf",
    ConvertOptions(
        assets_dir="assets",
        image_mode="reference",
        image_markup="auto",
        page_breaks=False,
    ),
)

print(result.markdown)
print(result.html)

semantic_document = result.semantic
semantic_json = semantic_document.to_dict()
report = result.report
```

A single conversion result contains:

```text
result.markdown
result.html
result.semantic
result.report
result.assets
result.warnings
```

---

## Diagnostics and explainability

CocoaPDF is designed to make uncertain decisions visible.

```bash
# Write a detailed report and print explanations
cocoapdf input.pdf \
  --report report.json \
  --explain

# Surface semantic nodes below a confidence threshold
cocoapdf input.pdf \
  --show-low-confidence \
  --min-confidence 0.85

# Explain the reconstructed document
cocoapdf explain input.pdf

# Trace a page through the extraction pipeline
cocoapdf trace input.pdf --page 1

# Draw a page-region overlay
cocoapdf overlay input.pdf --page 1 -o overlay.svg

# Inspect PDF objects, resources, and interpreted content
cocoapdf inspect input.pdf
```

Additional diagnostic commands include:

```bash
cocoapdf diff expected.md actual.md
cocoapdf score v1
cocoapdf bench v1
```

Every accepted non-container semantic node is expected to carry at least one source reference containing a page number and, where available:

* glyph IDs;
* region IDs;
* MCIDs;
* PDF object references;
* bounding boxes.

Graph-validation errors are reported rather than silently discarded.

---

## Development and verification

### Required verification gate

GitHub Actions enforces this gate on every pull request targeting `main` and again on every update to `main`. A merge cannot rely on the checklist alone: branch protection requires the `Version policy`, both endpoint-version `Quality` checks (Python 3.9 and 3.13), and all four native `Binary` checks to pass.

```bash
python -m pip install --disable-pip-version-check -e .
python scripts/check_repository_invariants.py
python -m unittest discover -s tests -v
python -m compileall -q src tests tools scripts
python -m cocoapdf --version
```

### Unicode bidirectional verification

```bash
python tools/check_unicode_bidi.py /path/to/BidiCharacterTest.txt
python tools/check_unicode_bidi.py /path/to/BidiTest.txt
```

The checker exits with a non-zero status when it finds paragraph-level, resolved-level, or visual-order mismatches. It also records the Unicode database version used by the running Python interpreter.

### Development method

CocoaPDF follows a generate–inspect–verify workflow:

1. Create known Markdown, HTML, Typst, LaTeX, or office-document sources.
2. Generate PDFs through materially different producer engines.
3. Inspect objects, streams, operators, fonts, glyphs, graphics, tags, and layout.
4. Convert each PDF through CocoaPDF.
5. Compare normalized semantic output with locked expected output.
6. Add adversarial near-miss fixtures before changing a detector.
7. Run the complete regression and resource-limit suite after every correction.

The objective is not to optimize for one showcase PDF. Each change must improve general PDF behavior without silently damaging another producer dialect.

Semantic detector changes must include positive evidence, an adversarial near-miss, and provenance/confidence assertions where applicable. Producer- or fixture-specific shortcuts are not accepted.

---

## Releases

CocoaPDF uses semantic version numbers in the form:

```text
MAJOR.MINOR.PATCH
```

Every accepted update to `main` is versioned, tested, built natively, smoke-tested, and published automatically. The release class is selected as follows:

* compatible fixes use `release:patch`, a `fix:` PR title, or a `fix/`, `bugfix/`, `hotfix/`, or `patch/` branch and increment `PATCH`;
* all other compatible changes use `release:minor` by default and increment `MINOR` while resetting `PATCH` to zero;
* `MAJOR` can change only when the repository owner increments `VERSION_MAJOR` by exactly one and applies the `breaking` label; automation then publishes `MAJOR.0.0`.

Published releases include:

* native Windows, Linux, and macOS packages;
* per-binary provenance manifests;
* `RELEASE.json`;
* `SHA256SUMS.txt`;
* source archives;
* release notes.

Until platform signing and notarization are available, users should verify published checksums when binary provenance matters.

---

## Project principles

1. **Semantic fidelity over visual text dumping.**
2. **PDF-native evidence before heuristic inference.**
3. **Geometry validates tags; tags inform geometry.**
4. **Markdown when sufficient, HTML when necessary.**
5. **One semantic graph for every output.**
6. **No OCR or text guessing from images.**
7. **No AI or machine-learning dependency.**
8. **No fabricated Unicode or document structure.**
9. **Low-confidence decisions remain inspectable.**
10. **Deterministic output from identical inputs and options.**

---

## License

CocoaPDF is released under the [MIT License](LICENSE).

Project attribution and notices are available in [`NOTICE`](NOTICE).
