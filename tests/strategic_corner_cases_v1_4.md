---
title: "CocoaPDF Strategic Corner Cases V1–V4"
fixture_id: "strategic_corner_cases_v1_4"
version: "1.0.0"
purpose: "Synthetic Markdown source used to generate PDF fixtures for CocoaPDF conversion regression testing."
comparison_policy: "Normalize whitespace only where explicitly documented; preserve semantic structure, text, links, tables, assets, and safe HTML fallback."
---

# CocoaPDF Strategic Corner Cases V1–V4

**Fixture contract:** this document intentionally combines common, adversarial, and future-scope cases. It is not a prose document; it is a deterministic test surface. Each section contains visible sentinel labels so conversion failures can be localized.

**Canonical package name:** CocoaPDF. The legacy `pdf2md` name may appear only as an import/CLI compatibility alias.

[[TOC source sentinel: a generated PDF may or may not create a real PDF outline/bookmark tree.]]

---

## 1. Plain Text, Paragraphs, Spacing, and Line Joining

SENTINEL-TEXT-001: A simple paragraph should survive as one Markdown paragraph with normal spaces, punctuation, and sentence boundaries.

SENTINEL-TEXT-002: This paragraph contains deliberately irregular     spacing, a non-breaking space between Cocoa PDF, a narrow no-break space in 12 345, and a thin space around A B. Normalize only where the converter policy explicitly allows it.

SENTINEL-TEXT-003: Soft line wrapping should be joined into one paragraph when the PDF visual line breaks are only layout artifacts. This sentence is intentionally long so that many PDF generators wrap it across several visual lines while the expected Markdown remains one paragraph.

SENTINEL-TEXT-004: Hard line breaks follow:
first hard line  
second hard line  
third hard line

SENTINEL-TEXT-005: Hyphenated wrap target words: micro-
service, co-
operate, re-
entry. Soft hyphen target: in­visible soft hyphen.

SENTINEL-TEXT-006: Literal hyphenation that must not be repaired: state-of-the-art, end-to-end, mother-in-law, twenty-one, non-breaking hyphen A‑B.

SENTINEL-TEXT-007: Dotted leader line for TOC-like detection: Introduction . . . . . . . . . . . . . . . . . 7

---

## 2. Unicode, Encodings, Symbols, and Normalization

SENTINEL-UNICODE-001 Greek uppercase: Α Β Γ Δ Ε Ζ Η Θ Ι Κ Λ Μ Ν Ξ Ο Π Ρ Σ Τ Υ Φ Χ Ψ Ω.

SENTINEL-UNICODE-002 Greek lowercase: α β γ δ ε ζ η θ ι κ λ μ ν ξ ο π ρ σ τ υ φ χ ψ ω; variants: ς ϕ ϑ ϵ.

SENTINEL-UNICODE-003 Latin diacritics composed: café naïve façade coöperate São Tomé Łódź Ćevapi Ångström smörgåsbord.

SENTINEL-UNICODE-004 Latin diacritics decomposed: café naïve façade coöperate São Tomé Ångström.

SENTINEL-UNICODE-005 Ligatures and compatibility: ﬁ ﬂ ﬃ ﬄ ﬀ; plain equivalents: fi fl ffi ffl ff; Roman numerals: Ⅰ Ⅱ Ⅲ Ⅳ Ⅴ Ⅹ.

SENTINEL-UNICODE-006 Math operators: ± × ÷ ≠ ≈ ≡ ≤ ≥ ∑ ∏ √ ∫ ∂ ∇ ∞ ∝ ∈ ∉ ∩ ∪ ⊂ ⊆ ⊕ ⊗ ∴ ∵.

SENTINEL-UNICODE-007 Arrows: ← ↑ → ↓ ↔ ↕ ⇒ ⇐ ⇔ ↦ ↩ ↪ ⟶ ⟵ ⟷.

SENTINEL-UNICODE-008 Currency and legal symbols: $ € £ ¥ ₹ ₿ ¢ ₩ ₪ ₽ © ® ™ § ¶ † ‡ №.

SENTINEL-UNICODE-009 Emoji single code points: 😀 😅 🚀 ☕ 🍫 📄 🧪 ✅ ❌ ⚠️.

SENTINEL-UNICODE-010 Emoji ZWJ and modifiers: 👩‍💻 🧑🏽‍🔬 👨‍👩‍👧‍👦 🏳️‍🌈 🇮🇳 🇺🇸.

SENTINEL-UNICODE-011 CJK: 中文测试 日本語テスト 한국어 테스트 漢字かなカナ.

SENTINEL-UNICODE-012 RTL visible text: עברית שלום עולם. العربية مرحبا بالعالم. Mixed LTR/RTL: ABC עברית 123 العربية XYZ.

SENTINEL-UNICODE-013 Indic and complex shaping: हिन्दी परीक्षण, বাংলা পরীক্ষা, தமிழ் சோதனை, తెలుగు పరీక్ష, ಕನ್ನಡ ಪರೀಕ್ಷೆ.

SENTINEL-UNICODE-014 Other scripts: Кириллица тест, Ελληνικά δοκιμή, Հայերեն փորձարկում, ქართული ტესტი, ไทยทดสอบ.

SENTINEL-UNICODE-015 Invisible/control-like characters named visibly: zero-width joiner [‍], zero-width non-joiner [‌], word joiner [⁠], left-to-right mark [‎], right-to-left mark [‏].

---

## 3. Markdown Escaping and Literal Characters

SENTINEL-ESCAPE-001: Literal Markdown metacharacters in prose: backslash \ backtick ` asterisk * underscore _ braces {} brackets [] parentheses () hash # plus + minus - dot . exclamation ! pipe | angle <tag> ampersand &.

SENTINEL-ESCAPE-002: These should remain prose, not syntax: #notheading, -notlist, +notlist, *not emphasis*, _not emphasis_, 1.not ordered list.

SENTINEL-ESCAPE-003: Entity-looking text: &copy; &amp; &#169; should not be double-decoded unexpectedly.

SENTINEL-ESCAPE-004: Literal HTML-looking text in code: `<div class="x">not active html</div>`.

---

## 4. Heading Hierarchy and Anchors

# H1 Duplicate Title Sentinel

## H2 Numbered and Plain

### H3 1.2.3 Numbered Heading

#### H4 With **Bold** and *Italic* Inline Source

##### H5 ALL CAPS HEADING

###### H6 Small Heading

## H2 Duplicate Title Sentinel

## H2 Duplicate Title Sentinel

<a id="explicit-anchor-v1-4"></a>

SENTINEL-ANCHOR-001: The explicit anchor target above should be available for internal link tests when the Markdown/PDF generator preserves anchors.

---

## 5. Inline Formatting

SENTINEL-INLINE-001: Plain, **bold**, *italic*, ***bold italic***, `inline_code()`, ~~strikethrough~~, <u>underline via HTML fallback</u>, <mark>highlight via HTML fallback</mark>.

SENTINEL-INLINE-002: Mixed styles inside one sentence: alpha **bravo *charlie* delta** echo `foxtrot_golf` hotel.

SENTINEL-INLINE-003: Superscript/subscript: H<sub>2</sub>O, CO<sub>2</sub>, x<sup>2</sup> + y<sup>2</sup> = z<sup>2</sup>, E = mc<sup>2</sup>.

SENTINEL-INLINE-004: Code span with punctuation: `a|b*c_d[0](x){y}`.

SENTINEL-INLINE-005: Backtick stress: ``code span containing ` one backtick`` and ```code span containing `` two backticks```.

SENTINEL-INLINE-006: Faux-style text that should stay plain if source PDF does not encode style: BOLDLOOKINGCAPS italic-looking-words code-looking_word.

---

## 6. Links, Destinations, and Security

SENTINEL-LINK-001: External link: [CocoaPDF external HTTPS](https://example.com/cocoapdf?alpha=1&beta=two#frag).

SENTINEL-LINK-002: Mail link: [Email maintainer](mailto:maintainer@example.com?subject=CocoaPDF%20fixture).

SENTINEL-LINK-003: Telephone link: [Call placeholder](tel:+15551234567).

SENTINEL-LINK-004: Internal link: [Jump to explicit anchor](#explicit-anchor-v1-4).

SENTINEL-LINK-005: URL with parentheses: [URL parentheses](https://example.com/a_(b)_c).

SENTINEL-LINK-006: Bare autolinks: <https://example.org/bare-url> and <mailto:bare@example.org>.

SENTINEL-LINK-SECURITY-001: Unsafe URI must be reported or rendered as plain text, not active: [unsafe javascript](javascript:alert(1)).

SENTINEL-LINK-SECURITY-002: Unsafe file URI must be reported or rendered as plain text, not active: [unsafe local file](file:///etc/passwd).

---

## 7. Lists and Nesting

SENTINEL-LIST-001 unordered list:

- alpha unordered item
- bravo unordered item with **bold** and `code`
- charlie unordered item that wraps in PDF because it contains a long explanation about list continuation, indentation, and visual line recovery across a layout-generated soft wrap.

SENTINEL-LIST-002 ordered list:

1. first ordered item
2. second ordered item
3. third ordered item

SENTINEL-LIST-003 ordered list starting at non-one:

7. item seven
8. item eight
9. item nine

SENTINEL-LIST-004 letter and roman markers source:

a. letter item source
b. second letter item source
iv. roman item source
v. second roman item source

SENTINEL-LIST-005 nested mixed list:

1. parent ordered alpha
   - child unordered alpha
   - child unordered bravo
     1. grandchild ordered one
     2. grandchild ordered two
2. parent ordered bravo
   1. child ordered one
      - deep bullet one
      - deep bullet two
   2. child ordered two

SENTINEL-LIST-006 task list source:

- [x] checked task item
- [ ] unchecked task item
- [X] uppercase checked task item

SENTINEL-LIST-007 singleton bullet that may be prose and should not be hallucinated as list without evidence:
• Singleton bullet-looking prose line.

SENTINEL-LIST-008 dash dialogue that should not become a list:
— This is dialogue with an em dash.
— This is another dialogue line.

---

## 8. Blockquotes

> SENTINEL-QUOTE-001 simple quote line one.
> simple quote line two with **bold** and `code`.

> SENTINEL-QUOTE-002 quote with list:
> - quoted bullet alpha
> - quoted bullet bravo
>
> 1. quoted ordered alpha
> 2. quoted ordered bravo

> SENTINEL-QUOTE-003 nested quote:
> > nested level two
> > > nested level three

> SENTINEL-QUOTE-004 quote with code:
> ```python
> def quoted_code(x):
>     return x + 1
> ```

---

## 9. Code Blocks

SENTINEL-CODE-001 fenced Python:

```python
def alpha(value: int) -> str:
    if value <= 0:
        return "zero-or-negative"
    return f"value={value}"
```

SENTINEL-CODE-002 blank lines, tabs, trailing-looking spaces:

```text
line one

	line with leading tab
    line with four spaces
pipe | backtick ` asterisk * underscore _ brackets [] braces {}
```

SENTINEL-CODE-003 long code line:

```text
abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789__abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789__END
```

SENTINEL-CODE-004 code block containing triple backticks:

````markdown
```python
print("inner fenced block")
```
````

SENTINEL-CODE-005 HTML/XML code:

```html
<section data-case="v1-4"><span>escaped & visible</span></section>
```

---

## 10. Horizontal Rules and Decorative Lines

SENTINEL-RULE-001 rule follows.

---

SENTINEL-RULE-002 text after rule. The line above is semantic thematic break, not a table border.

Heading underline adversarial case
----------------------------------

SENTINEL-RULE-003: The underline above may be rendered as Setext heading by Markdown generators; converter must avoid misclassifying unrelated table borders as thematic breaks.

---

## 11. Tables — GFM, Alignment, Escapes, and Borderless-Like Patterns

SENTINEL-TABLE-001 simple GFM table:

| Name | Qty | Price |
| --- | ---: | ---: |
| cocoa | 3 | 12.50 |
| PDF beans | 11 | 99.99 |
| escaped pipe | 1 | a \| b |

SENTINEL-TABLE-002 alignment table:

| Left aligned | Center aligned | Right aligned |
| :--- | :---: | ---: |
| alpha | bravo | 123 |
| long wrapped cell text that may visually wrap in PDF | centered | 456.78 |

SENTINEL-TABLE-003 numeric and decimal alignment:

| Metric | 2024 | 2025 | Δ |
| --- | ---: | ---: | ---: |
| Revenue | 1,234.50 | 2,345.60 | +1,111.10 |
| Cost | -333.33 | -444.44 | -111.11 |
| Ratio | 0.125 | 0.875 | +0.750 |

SENTINEL-TABLE-004 table-like prose that must not become a table:

Alpha      Bravo      Charlie
This is aligned prose, not a table, because it lacks repeated row semantics and it is a sentence-like paragraph.

SENTINEL-TABLE-005 key-value borderless table source:

| Field | Value |
| --- | --- |
| **Project** | CocoaPDF |
| **Scope** | structured text-layer PDFs |
| **Mode** | Markdown-first with HTML fallback |

---

## 12. Complex HTML Tables Requiring HTML Fallback

SENTINEL-HTML-TABLE-001 complex table:

<table>
  <caption>Complex semantic table with spans, nested content, and code</caption>
  <thead>
    <tr>
      <th rowspan="2">Component</th>
      <th colspan="2">Evidence</th>
      <th rowspan="2">Required output</th>
    </tr>
    <tr>
      <th>Geometry</th>
      <th>Semantic prior</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>Heading</td>
      <td>font size, gap, bold ratio</td>
      <td>tagged H1–H6 when available</td>
      <td>Markdown ATX heading</td>
    </tr>
    <tr>
      <td>Complex cell</td>
      <td colspan="2">
        <ul>
          <li>nested list inside cell</li>
          <li>inline <code>code()</code> inside cell</li>
          <li>Unicode αβγ and emoji ☕</li>
        </ul>
      </td>
      <td>HTML table fallback</td>
    </tr>
    <tr>
      <td>Rowspan example</td>
      <td rowspan="2">shared geometry evidence</td>
      <td>first semantic row</td>
      <td>preserve rowspan</td>
    </tr>
    <tr>
      <td>Rowspan continuation</td>
      <td>second semantic row</td>
      <td>do not flatten silently</td>
    </tr>
  </tbody>
</table>

---

## 13. Figures, Images, Captions, Floats, and SVG

SENTINEL-FIGURE-001 Markdown image with SVG data URI:

![SVG vector figure sentinel VEC-001](data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSI3MjAiIGhlaWdodD0iMjQwIiB2aWV3Qm94PSIwIDAgNzIwIDI0MCI+CiAgPHJlY3QgeD0iMSIgeT0iMSIgd2lkdGg9IjcxOCIgaGVpZ2h0PSIyMzgiIGZpbGw9IiNmZmY1ZGYiIHN0cm9rZT0iI2Q2M2EyYiIgc3Ryb2tlLXdpZHRoPSIzIi8+CiAgPGNpcmNsZSBjeD0iOTgiIGN5PSI5MiIgcj0iNDYiIGZpbGw9IiM4YTRmMzIiLz4KICA8cGF0aCBkPSJNMTgwIDU1IEw2NjAgNTUgTTE4MCA5NSBMNjIwIDk1IE0xODAgMTM1IEw2OTAgMTM1IE0xODAgMTc1IEw1NzAgMTc1IiBzdHJva2U9IiMyZTZhZTYiIHN0cm9rZS13aWR0aD0iOCIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIi8+CiAgPHRleHQgeD0iNTIiIHk9IjE3OCIgZm9udC1zaXplPSIyMiIgZm9udC1mYW1pbHk9InNlcmlmIiBmaWxsPSIjMjQxNTBmIj5TVkcgRklHVVJFPC90ZXh0PgogIDx0ZXh0IHg9IjE4MCIgeT0iMjIwIiBmb250LXNpemU9IjI0IiBmb250LWZhbWlseT0ibW9ub3NwYWNlIiBmaWxsPSIjMTcyMDJhIj52ZWN0b3Igc2VudGluZWw6IFZFQy0wMDEgzrHOss6zIOKGkiBNYXJrZG93bjwvdGV4dD4KPC9zdmc+)

*Figure 1. SVG vector figure with cocoa mark, blue lines, and visible vector sentinel text.*

<figure id="fig-html-svg">
  <img alt="HTML figure SVG sentinel VEC-HTML-001" src="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSI3MjAiIGhlaWdodD0iMjQwIiB2aWV3Qm94PSIwIDAgNzIwIDI0MCI+CiAgPHJlY3QgeD0iMSIgeT0iMSIgd2lkdGg9IjcxOCIgaGVpZ2h0PSIyMzgiIGZpbGw9IiNmZmY1ZGYiIHN0cm9rZT0iI2Q2M2EyYiIgc3Ryb2tlLXdpZHRoPSIzIi8+CiAgPGNpcmNsZSBjeD0iOTgiIGN5PSI5MiIgcj0iNDYiIGZpbGw9IiM4YTRmMzIiLz4KICA8cGF0aCBkPSJNMTgwIDU1IEw2NjAgNTUgTTE4MCA5NSBMNjIwIDk1IE0xODAgMTM1IEw2OTAgMTM1IE0xODAgMTc1IEw1NzAgMTc1IiBzdHJva2U9IiMyZTZhZTYiIHN0cm9rZS13aWR0aD0iOCIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIi8+CiAgPHRleHQgeD0iNTIiIHk9IjE3OCIgZm9udC1zaXplPSIyMiIgZm9udC1mYW1pbHk9InNlcmlmIiBmaWxsPSIjMjQxNTBmIj5TVkcgRklHVVJFPC90ZXh0PgogIDx0ZXh0IHg9IjE4MCIgeT0iMjIwIiBmb250LXNpemU9IjI0IiBmb250LWZhbWlseT0ibW9ub3NwYWNlIiBmaWxsPSIjMTcyMDJhIj52ZWN0b3Igc2VudGluZWw6IFZFQy0wMDEgzrHOss6zIOKGkiBNYXJrZG93bjwvdGV4dD4KPC9zdmc+" />
  <figcaption>Figure 2. HTML figure with explicit figcaption and vector source.</figcaption>
</figure>

SENTINEL-FIGURE-002 inline icon-like image should not destroy surrounding text:
before-icon ![tiny vector icon](data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSI3MjAiIGhlaWdodD0iMjQwIiB2aWV3Qm94PSIwIDAgNzIwIDI0MCI+CiAgPHJlY3QgeD0iMSIgeT0iMSIgd2lkdGg9IjcxOCIgaGVpZ2h0PSIyMzgiIGZpbGw9IiNmZmY1ZGYiIHN0cm9rZT0iI2Q2M2EyYiIgc3Ryb2tlLXdpZHRoPSIzIi8+CiAgPGNpcmNsZSBjeD0iOTgiIGN5PSI5MiIgcj0iNDYiIGZpbGw9IiM4YTRmMzIiLz4KICA8cGF0aCBkPSJNMTgwIDU1IEw2NjAgNTUgTTE4MCA5NSBMNjIwIDk1IE0xODAgMTM1IEw2OTAgMTM1IE0xODAgMTc1IEw1NzAgMTc1IiBzdHJva2U9IiMyZTZhZTYiIHN0cm9rZS13aWR0aD0iOCIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIi8+CiAgPHRleHQgeD0iNTIiIHk9IjE3OCIgZm9udC1zaXplPSIyMiIgZm9udC1mYW1pbHk9InNlcmlmIiBmaWxsPSIjMjQxNTBmIj5TVkcgRklHVVJFPC90ZXh0PgogIDx0ZXh0IHg9IjE4MCIgeT0iMjIwIiBmb250LXNpemU9IjI0IiBmb250LWZhbWlseT0ibW9ub3NwYWNlIiBmaWxsPSIjMTcyMDJhIj52ZWN0b3Igc2VudGluZWw6IFZFQy0wMDEgzrHOss6zIOKGkiBNYXJrZG93bjwvdGV4dD4KPC9zdmc+) after-icon.

SENTINEL-FIGURE-003 image wrapped in link:
[![linked vector image alt text](data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSI3MjAiIGhlaWdodD0iMjQwIiB2aWV3Qm94PSIwIDAgNzIwIDI0MCI+CiAgPHJlY3QgeD0iMSIgeT0iMSIgd2lkdGg9IjcxOCIgaGVpZ2h0PSIyMzgiIGZpbGw9IiNmZmY1ZGYiIHN0cm9rZT0iI2Q2M2EyYiIgc3Ryb2tlLXdpZHRoPSIzIi8+CiAgPGNpcmNsZSBjeD0iOTgiIGN5PSI5MiIgcj0iNDYiIGZpbGw9IiM4YTRmMzIiLz4KICA8cGF0aCBkPSJNMTgwIDU1IEw2NjAgNTUgTTE4MCA5NSBMNjIwIDk1IE0xODAgMTM1IEw2OTAgMTM1IE0xODAgMTc1IEw1NzAgMTc1IiBzdHJva2U9IiMyZTZhZTYiIHN0cm9rZS13aWR0aD0iOCIgc3Ryb2tlLWxpbmVjYXA9InJvdW5kIi8+CiAgPHRleHQgeD0iNTIiIHk9IjE3OCIgZm9udC1zaXplPSIyMiIgZm9udC1mYW1pbHk9InNlcmlmIiBmaWxsPSIjMjQxNTBmIj5TVkcgRklHVVJFPC90ZXh0PgogIDx0ZXh0IHg9IjE4MCIgeT0iMjIwIiBmb250LXNpemU9IjI0IiBmb250LWZhbWlseT0ibW9ub3NwYWNlIiBmaWxsPSIjMTcyMDJhIj52ZWN0b3Igc2VudGluZWw6IFZFQy0wMDEgzrHOss6zIOKGkiBNYXJrZG93bjwvdGV4dD4KPC9zdmc+)](https://example.com/linked-image)

---

## 14. OCR / Raster Hybrid Future Fixture

SENTINEL-OCR-001: The following image is intentionally raster text. V1/V2/V3 core without OCR should preserve it as an image or alt text, not hallucinate body text. V4 optional OCR may recover the raster text only when explicitly enabled and must report OCR provenance.

![OCR raster image sentinel OCR-001](data:image/png;base64,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)

Expected OCR-visible phrase if OCR is enabled: `OCR-ONLY SENTINEL: Raster text 12345`.

---

## 15. Footnotes and Endnotes

SENTINEL-FOOTNOTE-001: This sentence has a numeric footnote.[^1] This sentence has another note with Unicode.[^unicode-note]

SENTINEL-FOOTNOTE-002: Multiple references to one definition may occur.[^1]

[^1]: Footnote definition one. It includes a URL <https://example.com/footnote>, punctuation, and continuation text.
[^unicode-note]: Footnote with Greek αβγ, emoji ☕, and CJK 中文.

## Endnotes

[EN1] SENTINEL-ENDNOTE-001: Endnote-style paragraph that resembles a bibliography entry but is not a citation.

---

## 16. Table of Contents, Cross-References, and Bibliography

## Contents

- [Plain Text, Paragraphs, Spacing, and Line Joining](#1-plain-text-paragraphs-spacing-and-line-joining)
- [Tables](#11-tables--gfm-alignment-escapes-and-borderless-like-patterns)
- [Figures](#13-figures-images-captions-floats-and-svg)

SENTINEL-CROSSREF-001: See Figure 1, Figure 2, Table 1, Section 11, Appendix A, Equation (1), and reference [1].

## References

[1] Doe, Jane. “A Synthetic PDF Benchmark.” Journal of Deterministic Documents, 2026. DOI: 10.0000/cocoapdf.0001.

[2] Smith, John and Roe, Alex. *Tables Without Borders*. Example Press, 2025. <https://example.com/references/2>.

Doe, J. (2024). Author-year reference style with hanging indentation expectation. Example Journal, 12(3), 45–67.

---

## 17. Math and Formula Fallback

SENTINEL-MATH-001 inline math-like text: E = mc², a² + b² = c², ∑ᵢ xᵢ = 42.

SENTINEL-MATH-002 display formula source:

$$
\int_0^1 x^2\,dx = \frac{1}{3}
$$

SENTINEL-MATH-003 Unicode formula:
∀x ∈ ℝ, ∃y ∈ ℝ such that y² = x when x ≥ 0.

SENTINEL-MATH-004 matrix-like text:

| Formula | Meaning |
| --- | --- |
| `A = [[1, 2], [3, 4]]` | matrix literal |
| `det(A) = -2` | determinant |

---

## 18. Multi-Column, Sidebars, Callouts, and Floats via HTML/CSS

<div style="columns: 2; column-gap: 2rem; border-left: 4px solid #d63a2b; padding-left: 1rem;">
<p><strong>SENTINEL-COLUMNS-001 left/right flow:</strong> Column content alpha begins here. It should be read in column order when the PDF physically lays it out in columns.</p>
<p>Column content bravo contains Greek αβγ and numbers 12345.</p>
<p>Column content charlie contains a link-like visible string https://example.com/columns.</p>
<p>Column content delta ends the multi-column region.</p>
</div>

<aside style="border: 1px solid #9bb7d3; background: #eef6ff; padding: 0.75rem; margin: 1rem 0;">
<strong>SENTINEL-CALLOUT-001:</strong> This is an HTML callout/sidebar. Markdown cannot represent this exactly; CocoaPDF should use safe HTML fallback or report a callout region.
</aside>

---

## 19. Forms and Annotations Future Fixture

SENTINEL-FORM-001 visible form-like text:

<label>Name: <input type="text" value="Cocoa Tester" /></label>

<label><input type="checkbox" checked /> checked checkbox visible label</label>

<label><input type="checkbox" /> unchecked checkbox visible label</label>

<select>
  <option>alpha</option>
  <option selected>bravo selected</option>
</select>

SENTINEL-ANNOTATION-001: Highlight, underline, strikeout, sticky-note, stamp, and file-attachment annotations require PDF-level fixture construction. This Markdown source includes visible labels only; annotation dictionaries should be added by a PDF fixture generator for V2.12/V3 tests.

---

## 20. Active Content and Security Future Fixture

SENTINEL-SECURITY-001: Generated PDFs for this section may be post-processed to inject `/OpenAction`, `/AA`, `/JavaScript`, `/Launch`, `/RichMedia`, remote `/GoToR`, and embedded-file actions. CocoaPDF must detect and report these without executing, fetching, launching, or submitting anything.

SENTINEL-SECURITY-002: Visible unsafe strings: javascript:alert(1), file:///tmp/secret.txt, data:text/html;base64,SGVsbG8=.

---

## 21. Page Breaks, Furniture, Headers, Footers

SENTINEL-PAGE-001: The generator may insert page numbers and running headers/footers. CocoaPDF must remove repeated furniture only with repeated-page evidence and must never drop body text.

<div style="page-break-before: always;"></div>

SENTINEL-PAGE-002: New page marker after explicit page break. If a PDF generator honors the break, this starts a new page.

Running header text candidate: CocoaPDF Fixture Header
Running footer text candidate: Page N Footer

---

## 22. Damaged, Encrypted, and Hard-PDF V3 Matrix Labels

SENTINEL-V3-001: This source cannot itself create malformed xrefs, object streams, encrypted streams, corrupted filters, or damaged trailers. Create derivative PDFs from this generated PDF for V3 tests:
- V3-A: object streams and xref streams
- V3-B: incremental update overriding old objects
- V3-C: empty-password RC4/AES encrypted copy
- V3-D: damaged xref requiring recovery scan
- V3-E: missing or lying `/Length`
- V3-F: no-ToUnicode symbolic font
- V3-G: CJK/RTL/vertical-writing focused pages
- V3-H: decompression bomb guard fixture

---

## 23. Final Kitchen Sink Paragraph

SENTINEL-FINAL-001: CocoaPDF final mixed paragraph contains **bold αβγ**, *italic 中文*, `code_with_pipe|and_backtick`, a link to [HTTPS](https://example.com/final), superscript x<sup>2</sup>, subscript H<sub>2</sub>O, emoji ☕🚀, currency €₹₿, RTL עברית العربية, and escaped markdown characters \ ` * _ [ ] ( ) # + - . ! |.

---

## Appendix A — Expected Comparison Notes

- V1/V2 Markdown-first output may preserve complex sections as safe HTML fallback.
- V3 hard-PDF cases require derivative PDFs created after this source PDF is generated.
- V4 OCR is optional and must be disabled by default; when enabled, OCR text must carry provenance and confidence.
- Unsafe links/actions must never remain active in HTML output.
- Structural false positives are worse than plain-text fallback.
