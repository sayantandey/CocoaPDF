# CocoaPDF

CocoaPDF converts digitally born, structured text-layer PDFs into semantic Markdown, loss-aware HTML, and provenance-rich JSON. It parses PDF bytes directly with Python's standard library, reconciles geometry with Tagged-PDF structure, and preserves every accepted node's source pages, regions, glyphs, MCIDs, confidence, evidence, and warnings. It does not use OCR, AI, or runtime PDF frameworks; raster images are preserved as images with their PDF placement, dimensions, alignment, links, captions, and alternative text.

[**Download for Windows**][download-windows] · [**Download for Linux**][download-linux] · [**Download for macOS**][download-macos]

These stable links always resolve to the newest verified GitHub release. The macOS archive contains separate Apple Silicon and Intel executables.

[download-windows]: https://github.com/sayantandey/CocoaPDF/releases/latest/download/cocoapdf-windows-x86_64.zip
[download-linux]: https://github.com/sayantandey/CocoaPDF/releases/latest/download/cocoapdf-linux-x86_64.tar.gz
[download-macos]: https://github.com/sayantandey/CocoaPDF/releases/latest/download/cocoapdf-macos.tar.gz

## What CocoaPDF produces

One conversion builds a single reconciled semantic document graph and emits all representations from that graph:

```text
PDF bytes and operators
  -> COS objects, streams, resources, fonts, glyphs, paths, images, annotations
  -> normalized source IR
  -> layout, regions, and reading order
  -> Tagged-PDF and geometry reconciliation
  -> semantic document graph
       -> Markdown
       -> HTML
       -> JSON
       -> report and assets
```

The graph contains typed headings, paragraphs, inline styles, links, lists, code, quotations, tables, figures, captions, notes, TOC entries, references, cross-references, form fields, anchors, and page breaks. Markdown is preferred when it can preserve the structure; generated HTML is used where Markdown cannot represent spans, nested table content, vertical text, dimensions, or other semantics safely.

## Scope

CocoaPDF is designed for digitally born PDFs with a usable text layer. It does **not** OCR scanned pages or attempt to read text from raster images. Images are extracted or embedded without semantic text guessing, and the semantic report records `ocr_used: false` and `text_extraction_attempted: false` for image nodes.

Encrypted PDFs are refused unless their bytes can be validated safely. Unsupported or malformed constructs produce warnings and conservative fallbacks rather than fabricated Unicode, cells, destinations, labels, or form values.

## Capabilities

### PDF and text foundation

- Classic xref tables, xref streams, hybrid/incremental chains, object streams, and stream-aware recovery.
- Bounded Flate, ASCII85, ASCIIHex, RunLength, LZW, and pass-through image filters.
- Page trees, inherited resources, content arrays, Form XObjects, marked content, paths, clipping bounds, graphics-state alpha, images, links, and annotations.
- Simple fonts, PDFDocEncoding, WinAnsi and Differences encodings, Standard-14 metrics, ToUnicode CMaps, composite CID fonts, selected predefined Unicode CMaps, ligature normalization, and vertical `DW2`/`W2` metrics.
- Geometry-derived spacing, `TJ` displacements, duplicate/faux-bold suppression, invisible-text handling, superscript/subscript, and source-preserving Unicode bidirectional reordering.

### Structure recovery

- Paragraph joining, hard and soft line breaks, hyphenation repair, headings, inline emphasis, underline/strike/highlight evidence, inline and block code, blockquotes, rules, and nested lists.
- Region-aware reading order for columns, sidebars, callouts, figures, tables, footnote zones, headers, and footers.
- Ruled and precision-gated borderless tables, conservative missing-border span inference, rotated headers, nested cell blocks, typed captions and table notes, per-cell provenance/confidence, and guarded multi-page continuation.
- Figures and captions, raster asset deduplication, PDF placement quads, display dimensions, alignment, image links, Tagged-PDF alternative text, and optional vector-to-SVG approximation.
- Footnotes/endnotes, reference sections, citations, cross-references, PDF outlines, named destinations, visible tables of contents, anchors, and internal links.
- AcroForm field trees with inherited attributes, text/choice/button/signature semantics, safe password redaction, widget provenance, and no action execution.

### Tagged PDF

- `StructTreeRoot`, `RoleMap`, namespaces, `ClassMap`, `ParentTree`, `StructParents`, `StructParent`, MCIDs, MCRs, OBJRs, `/Pg`, `/Stm`, `/ActualText`, `/Alt`, `/E`, `/Lang`, artifacts, and structure attributes.
- Tag semantics are treated as a strong prior, validated against page ownership, object references, marked-content coverage, and geometry.
- Reconciliation can materialize tagged headings, lists, tables, figures, links, TOC entries, captions, and artifacts while retaining geometric fallback for broken or incomplete tag trees.

### Outputs and diagnostics

- CommonMark-oriented Markdown with GFM tables and safe HTML fallbacks.
- Structured HTML emitted independently from the semantic graph.
- Stable semantic JSON plus a conversion report containing provenance, confidence, evidence, warnings, page modes, regions, assets, and graph-validation results.
- `inspect`, `trace`, `overlay`, `diff`, `score`, `bench`, and `explain` diagnostic commands.
- Official Unicode bidi corpus checker for `BidiCharacterTest.txt` and `BidiTest.txt`.

## Releases and versioning

Every merge to protected `main` runs the full suite, builds native Windows x86-64, Linux x86-64, macOS Intel, and macOS Apple Silicon executables, validates their binary headers and runtime version, and publishes one package per operating system. Each release also includes per-binary provenance manifests, `RELEASE.json`, and `SHA256SUMS.txt`. Binaries are not yet platform code-signed or notarized; verify the published checksums when provenance matters.

CocoaPDF uses `MAJOR.MINOR.PATCH`. Compatible fixes labeled `release:patch` (or merged from `fix/`, `bugfix/`, `hotfix/`, or `patch/` branches) advance patch; all other changes advance minor. Major is never inferred: the owner must increment `VERSION_MAJOR` by exactly one and apply the `breaking` label, producing `MAJOR.0.0`.

## Installation from source

CocoaPDF requires Python 3.9 or later and has no runtime dependencies.

```bash
python -m pip install .
```

For repository execution without installation:

```bash
export PYTHONPATH=src                 # Linux/macOS
python -m cocoapdf.cli input.pdf
```

```powershell
$env:PYTHONPATH = "src"              # Windows PowerShell
python -m cocoapdf.cli input.pdf
```

The repository launcher is equivalent:

```bash
python run_cocoapdf.py input.pdf
```

## Common conversions

```bash
# Markdown to stdout
cocoapdf input.pdf

# Markdown file, extracted assets, and diagnostic report
cocoapdf input.pdf -o document.md --assets assets --report report.json

# Semantic HTML
cocoapdf input.pdf --format html -o document.html

# Markdown, HTML, semantic JSON, and report as one output package
cocoapdf input.pdf --format both -o output

# JSON envelope containing the semantic graph, report, Markdown, and HTML
cocoapdf input.pdf --format json -o result.json

# Selected pages with explicit page boundaries
cocoapdf input.pdf --pages 1,3-5 --page-breaks -o excerpt.md
```

When `--format both` targets a directory, CocoaPDF writes:

```text
output/
  document.md
  document.html
  document.json
  report.json
```

Referenced assets are written to `--assets`.

## Image policy

```bash
# Extract image assets and reference them
cocoapdf input.pdf --image-mode reference --assets assets

# Embed image bytes as data URIs
cocoapdf input.pdf --image-mode embed

# Preserve dimensions/alignment automatically when Markdown is insufficient
cocoapdf input.pdf --image-markup auto

# Force dimensionless Markdown images
cocoapdf input.pdf --image-markup markdown

# Force generated HTML image/figure markup
cocoapdf input.pdf --image-markup html
```

`--image-markup auto` is the default. It emits generated HTML when image dimensions, alignment, captions, vertical placement, or links would otherwise be lost.

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
semantic_json = result.semantic.to_dict()
report = result.report
```

## Explainability

```bash
cocoapdf input.pdf --report report.json --explain
cocoapdf input.pdf --show-low-confidence --min-confidence 0.85
cocoapdf explain input.pdf
cocoapdf trace input.pdf --page 1
cocoapdf overlay input.pdf --page 1 -o overlay.svg
cocoapdf inspect input.pdf
```

Every non-container semantic node is expected to carry one or more `SourceRef` records with page number and, where available, glyph IDs, region IDs, MCIDs, PDF object references, and a bounding box. Graph validation errors are reported instead of silently discarded.

## Verification

```bash
export PYTHONPATH=src
python -m unittest discover -s tests -v
python -m compileall -q src tests tools
python tools/check_unicode_bidi.py /path/to/BidiCharacterTest.txt
python tools/check_unicode_bidi.py /path/to/BidiTest.txt
```

The Unicode checker exits non-zero on paragraph-level, resolved-level, or visual-order mismatches and records the runtime Unicode database version.

## Development method

CocoaPDF uses a generate–inspect–verify loop:

1. Generate known Markdown, HTML, Typst, LaTeX, or office sources through materially different PDF producers.
2. Inspect COS objects, streams, operators, fonts, glyphs, graphics, tags, and layout.
3. Convert through CocoaPDF and compare normalized semantic output with locked goldens.
4. Add adversarial near-miss fixtures before changing a detector.
5. Run the complete regression and resource-limit suite after every correction.

## License

CocoaPDF is released under the MIT License. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
