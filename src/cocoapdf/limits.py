"""Deterministic limits for malformed or hostile PDFs.

These are product-policy values, not PDF-spec constants.  Keep resource
ceilings centralized so the fuzz and corpus gates can lock their behavior.
"""

MAX_PAGES = 50_000
MAX_PAGE_TREE_DEPTH = 256
MAX_OBJECTS = 1_000_000
MAX_OBJECT_STREAM_OBJECTS = 250_000

MAX_DECODED_STREAM = 256 * 1024 * 1024
MAX_TOTAL_DECODED = 1024 * 1024 * 1024
MAX_CONTENT_BYTES_PER_PAGE = 64 * 1024 * 1024

MAX_CHARS_PER_PAGE = 250_000
MAX_SEGMENTS_PER_PAGE = 500_000
MAX_FILLS_PER_PAGE = 250_000
MAX_IMAGE_PIXELS = 100_000_000
MAX_IMAGE_DIMENSION = 100_000
MAX_FORM_DEPTH = 12

MAX_ENDSTREAM_SCAN = 64 * 1024 * 1024
