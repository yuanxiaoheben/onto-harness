"""Text/Markdown by default; PDF and DOCX require the documents extra."""

import hashlib
from pathlib import Path
from typing import Any, Iterable

from ..common import HarnessError
from ..domain import Document

SUPPORTED_SUFFIXES = {".txt", ".md", ".pdf", ".docx"}


def discover_documents(inputs: Iterable[Path]) -> list[Path]:
    files: set[Path] = set()
    for source in inputs:
        source = source.resolve()
        if not source.exists():
            raise FileNotFoundError(source)
        if source.is_dir():
            files.update(
                item.resolve()
                for item in source.rglob("*")
                if item.is_file() and item.suffix.lower() in SUPPORTED_SUFFIXES
            )
        elif source.suffix.lower() in SUPPORTED_SUFFIXES:
            files.add(source)
        else:
            raise ValueError(f"Unsupported document format: {source.suffix}")
    if not files:
        raise ValueError("No supported documents found; add .txt, .md, .pdf, or .docx files")
    return sorted(files, key=str)


def load_document(path: Path) -> Document:
    path = path.resolve()
    content = path.read_bytes()
    metadata: dict[str, Any] = {"format": path.suffix.lower(), "offset_basis": "extracted_text"}
    if path.suffix.lower() in {".txt", ".md"}:
        text = content.decode("utf-8-sig")
    elif path.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as error:
            raise HarnessError(
                "missing_dependency", 'Install PDF support: pip install -e ".[documents]"'
            ) from error
        pages = [page.extract_text() or "" for page in PdfReader(path).pages]
        # Offsets refer to the persisted text, including the two-newline page separators.
        metadata["page_count"] = len(pages)
        metadata["empty_text_pages"] = [i + 1 for i, text in enumerate(pages) if not text.strip()]
        text = "\n\n".join(pages)
    elif path.suffix.lower() == ".docx":
        try:
            from docx import Document as DocxDocument
        except ImportError as error:
            raise HarnessError(
                "missing_dependency", 'Install DOCX support: pip install -e ".[documents]"'
            ) from error
        document = DocxDocument(path)
        # XML body order preserves interleaved paragraphs and tables.
        from docx.oxml.ns import qn
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        blocks = []
        for element in document.element.body.iterchildren():
            if element.tag == qn("w:p"):
                blocks.append(Paragraph(element, document).text)
            elif element.tag == qn("w:tbl"):
                blocks.extend(
                    "\t".join(cell.text for cell in row.cells)
                    for row in Table(element, document).rows
                )
        text = "\n".join(blocks)
    else:
        raise ValueError(f"Unsupported document format: {path.suffix}")
    if not text.strip():
        raise ValueError(f"No readable text: {path}; scanned PDFs need an OCR adapter")
    digest = hashlib.sha256(content).hexdigest()
    # Including the resolved path distinguishes separate files with identical content.
    identity = hashlib.sha256(str(path).encode("utf-8") + b"\0" + content).hexdigest()
    return Document(f"doc_{identity[:20]}", str(path), text, digest, metadata)
