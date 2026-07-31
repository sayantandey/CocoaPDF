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

## 14. Raster Image Preservation Fixture

SENTINEL-RASTER-001: The following image contains pixel-only text and must remain an extracted raster asset. CocoaPDF preserves the image without parsing or inventing body text from its pixels. The semantic image node keeps page, PDF-object, dimensions, asset hash, and confidence provenance.

![Raster image preservation sentinel RASTER-001](data:image/png;base64,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H3/8oZrG8ePHqVUAT3QYjYyMfCo1/fv3J4wSRo1Wrlzp5+en31S0MPrJJ584bEWE0UfI2MwzZswouxARERGff/75hQsXUp0+S5YsBQsWrFix4muvvTZhwoSYmBjbXcq4ceMIo4RRAIRRwihh1A2qVKkihaxRo8aRI0eSkpK012NjYwMDA+Wt7t27u0gteIRhVC937tzr16+31C34+/t36tTpypUrhFHCKADCqNtGqbNnzz4JVUkYdZe4uDj10/ySJUsc3lqxYoXaqK5du0br9akwqjXzO3fuHDx4sE+fPhkzZpTXc+TIod9tME6fmJh46dKlXbt2jRkzpmzZsurdQoUKHT169EnrUgijAAijhFHCqE+QFKK2nN9//93hrZkzZ8rrWbNmpen6bBjVfPbZZ+qtYcOGmewWkpOTR44cqXZFwsLCbt68SRgljAIgjBJGCaPe9ueff6otR8ZUh7emTJkir+fMmZOm6/th9OrVq+qtf/7zn5a6Be204BEjRhBGCaMACKPeC6P6jvjAgQNt27YtVKhQ1qxZq1atumDBAm2yc+fO9erVKzQ0NDAwsHjx4v369bt161ZK8zx06FD37t3DwsKyZMkisypbtmzv3r1PnjxpMqK55ZzRxMTEdevWyXJlRQoXLuzv7x8cHFyzZs2RI0fGxcW5rgcZVFq3bi31kDlz5vDwcBmknX5EXLt2bfjw4RERETJzWYR8pGXLlhs2bPB0GJXyjBo1qm7duvny5QsICChSpEidOnXGjh178eJFG1+HypqWOKyI+XqwN71bqsL8Zum5OVvdLO01c3XHA6leS91CfHx83rx5ZRpZU7eHUTf2M2lp2tHR0a+//npISEjGjBlbtWrlOoyeP3++cuXKqkL27dunf0sKIFk/T548QUFBFSpUkCq6e/eu6zBqciOpUaOGzKFv374OrxcrVkzN/NixY/rXly9fLi9mypTpxo0bxtWRksj6SvmlPkuVKiW9UGxsLAMwAN8Noxs3bpQu0iFzjB8/XnWj0uk7vFWrVq179+4ZZ/jNN9+os9YcSLD78ccfvRZGp0+fnlKQkk7577//TqkeVq9eLaOFw0dKlChh/MimTZskpjhdhAwzJsup6vzll182/xXLN5XScl955RUbX0caw6ilerAxvVuqwtJm6bk5W90svXlkVEhaMiYe94bRtPcztpu2JEj9olu0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)

Pixel-only phrase inside the extracted image: `Raster SENTINEL: Raster text = 12345`.

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
- Raster-only content must remain an extracted image with page, object, and glyph-free provenance.
- Unsafe links/actions must never remain active in HTML output.
- Structural false positives are worse than plain-text fallback.
