from ._version import __version__
from .core import ConvertOptions, ConvertResult, convert, convert_file

__project__ = "CocoaPDF"
__description__ = "Deterministic PDF-to-Markdown/HTML conversion for structured text-layer PDFs. No OCR. No AI."

__all__ = [
	"ConvertOptions",
	"ConvertResult",
	"convert",
	"convert_file",
	"__description__",
	"__project__",
	"__version__",
]
