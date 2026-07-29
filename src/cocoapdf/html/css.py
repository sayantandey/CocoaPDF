DEFAULT_CSS = """
body { font-family: system-ui, sans-serif; line-height: 1.5; margin: 0; color: #1f2328; background: #fff; }
.cocoapdf-document { max-width: 72rem; margin: 2rem auto; padding: 0 1rem; }
table { border-collapse: collapse; margin: 1rem 0; max-width: 100%; }
.cocoapdf-table-container { max-width: 100%; overflow-x: auto; }
.cocoapdf-table-container:focus-visible { outline: 2px solid #0969da; outline-offset: 2px; }
th, td { border: 1px solid #999; padding: 0.25rem 0.5rem; }
caption { text-align: start; font-weight: 600; margin-bottom: 0.35rem; }
caption.cocoapdf-caption-bottom { caption-side: bottom; margin-top: 0.35rem; margin-bottom: 0; }
blockquote { border-left: 0.25rem solid #bbb; margin-left: 0; padding-left: 1rem; color: #333; }
pre { background: #f5f5f5; padding: 0.75rem; overflow-x: auto; }
mark { background: #fff59d; }
.cocoapdf-figure { margin-top: 1rem; margin-bottom: 1rem; max-width: 100%; }
.cocoapdf-figure img { display: block; object-fit: contain; }
.cocoapdf-align-left { margin-left: 0; margin-right: auto; text-align: left; }
.cocoapdf-align-center { margin-left: auto; margin-right: auto; text-align: center; }
.cocoapdf-align-right { margin-left: auto; margin-right: 0; text-align: right; }
.cocoapdf-figure figcaption { margin-top: 0.35rem; font-style: italic; color: #444; }
.cocoapdf-columns { columns: 2; column-gap: 2rem; margin: 1rem 0; }
.cocoapdf-columns > * { break-inside: avoid-column; }
.cocoapdf-form-appearance { display: grid; gap: 0.55rem; margin: 1rem 0; }
.cocoapdf-form-appearance label { display: block; }

.cocoapdf-page-break { break-before: page; border: 0; border-top: 1px dashed #bbb; }
.cocoapdf-toc ol { list-style-position: inside; }
.cocoapdf-toc ol ol { margin-left: 1.5rem; }
.cocoapdf-toc-page { float: inline-end; font-variant-numeric: tabular-nums; }
.cocoapdf-table-note { font-size: 0.9em; font-style: italic; }
.cocoapdf-cross-reference { text-decoration: none; }
.cocoapdf-callout, .cocoapdf-sidebar, .cocoapdf-annotation { margin: 1rem 0; }
.cocoapdf-callout { border-left: 0.25rem solid #6b8fb3; padding-left: 1rem; }
.cocoapdf-sidebar, .cocoapdf-annotation { border: 1px solid #c8ccd1; padding: 0.75rem; }
.cocoapdf-form { display: grid; gap: 0.65rem; margin: 1rem 0; }
.cocoapdf-form-field { display: grid; grid-template-columns: minmax(8rem, max-content) 1fr; column-gap: 0.5rem; }
.cocoapdf-form-field-name { font-weight: 600; }
.cocoapdf-form-field-value-evidenced { align-items: center; box-sizing: border-box; display: inline-flex; padding: 0 0.25rem; }
.cocoapdf-equation { overflow-x: auto; margin: 1rem 0; }
li > input[type="checkbox"][disabled] { margin-inline-end: 0.35rem; }
[role="doc-footnote"] { font-size: 0.92em; border-top: 1px solid #ddd; margin-top: 0.75rem; padding-top: 0.5rem; }
@media (max-width: 48rem) {
  .cocoapdf-columns { columns: auto; }
  .cocoapdf-form-field { grid-template-columns: 1fr; }
  .cocoapdf-document img { height: auto !important; }
}
@media print {
  body { color: #000; }
  .cocoapdf-document { max-width: none; margin: 0; padding: 0; }
  .cocoapdf-table-container { overflow: visible; }
  thead { display: table-header-group; }
  tr, figure, blockquote, pre { break-inside: avoid; }
  a { color: inherit; text-decoration: none; }
}
""".strip()
