"""Offline document extraction and citation-preserving, heading-aware chunking.

Optional dependencies are imported only by the parser that needs them. See
``docs/parsers.md`` for configuration, safety limits and extraction limitations.
"""
from __future__ import annotations

from kb.telemetry import traced

import csv
import hashlib
import importlib
import io
import mimetypes
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import warnings
import zipfile
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from typing import Any, Iterable
from xml.sax.handler import ContentHandler


SUPPORTED_EXTENSIONS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp",
    ".docx", ".doc", ".xml", ".eml", ".msg", ".xls", ".xlsx", ".xlsm",
    ".csv", ".txt", ".log", ".md", ".markdown", ".tsv", ".parquet",
}


@dataclass
class ParsedBlock:
    text: str
    section: str = "Content"
    page: int | None = None
    kind: str = "text"
    asset_path: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class ParsedDocument:
    blocks: list[ParsedBlock]
    assets: list[dict]
    parser: str
    layout_objects: list[dict] = field(default_factory=list)
    extraction_evidence: dict = field(default_factory=dict)


class ParseError(ValueError):
    """Extraction failed; the caller must not index a partial result."""


class UnsupportedFormat(ParseError):
    """The input format needs an explicit conversion or a different parser."""


class MissingDependency(ParseError):
    """A local dependency or executable must be installed by an operator."""


class ParserLimitError(ParseError):
    """An operator-configured resource limit was exceeded (never truncation)."""


def _limit(name: str, default: int, *, allow_zero: bool = False) -> int:
    value = int(os.environ.get(name, default))
    if value < 0 or (value == 0 and not allow_zero):
        raise ParseError(f"{name} must be nonnegative" if allow_zero else f"{name} must be positive")
    return value


def _enabled(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).lower() in {"1", "true", "yes", "on"}


def _dependency(name: str, package: str | None = None) -> Any:
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise MissingDependency(f"Install {package or name} locally to parse this format") from exc


def _archive_guard(path: Path) -> None:
    """Reject oversized Office containers before third-party decompression."""
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > _limit("PARSER_MAX_ARCHIVE_ENTRIES", 20000):
            raise ParserLimitError("Office archive has too many members")
        if sum(entry.file_size for entry in entries) > _limit("PARSER_MAX_ARCHIVE_BYTES", 268435456):
            raise ParserLimitError("Office archive expanded size exceeds PARSER_MAX_ARCHIVE_BYTES")
        if any(entry.flag_bits & 1 for entry in entries):
            raise ParseError("Encrypted Office archives must be decrypted by an authorized operator")


class _Builder:
    def __init__(self, asset_dir: Path, parser: str):
        self.asset_dir = asset_dir.resolve()
        self.asset_dir.mkdir(parents=True, exist_ok=True)
        self.parser = parser
        self.blocks: list[ParsedBlock] = []
        self.assets: list[dict] = []
        self.chars = 0
        self.max_chars = _limit("PARSER_MAX_TEXT_CHARS", 20000000)
        self.max_blocks = _limit("PARSER_MAX_BLOCKS", 200000)
        self.max_asset_bytes = _limit("PARSER_MAX_ASSET_BYTES", 20971520)
        self.max_total_asset_bytes = _limit("PARSER_MAX_TOTAL_ASSET_BYTES", 268435456)
        self.asset_bytes = 0
        self.max_assets = _limit("PARSER_MAX_ASSETS", 10000)

    def add(self, text: Any, section: str = "Content", page: int | None = None,
            kind: str = "text", asset_path: str | None = None, metadata: dict | None = None) -> None:
        text = str(text) if text is not None else ""
        if not text.strip():
            return
        self.chars += len(text)
        if self.chars > self.max_chars or len(self.blocks) >= self.max_blocks:
            raise ParserLimitError("Extracted content exceeds PARSER_MAX_TEXT_CHARS/PARSER_MAX_BLOCKS")
        self.blocks.append(ParsedBlock(text, section, page, kind, asset_path, metadata or {}))

    def asset(self, data: bytes, suffix: str, section: str = "Content",
              page: int | None = None, **metadata: Any) -> str:
        if not data:
            raise ParseError("An embedded asset contains no bytes")
        if len(data) > self.max_asset_bytes or len(self.assets) >= self.max_assets:
            raise ParserLimitError("Embedded assets exceed PARSER_MAX_ASSET_BYTES/PARSER_MAX_ASSETS")
        self.asset_bytes += len(data)
        if self.asset_bytes > self.max_total_asset_bytes:
            raise ParserLimitError("Embedded assets exceed PARSER_MAX_TOTAL_ASSET_BYTES")
        suffix = suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
            suffix = ".bin"
        digest = hashlib.sha256(data).hexdigest()
        path = self.asset_dir / f"{digest}{suffix}"
        # Exclusive creation avoids overwriting another ingestion worker's asset.
        try:
            with path.open("xb") as output:
                output.write(data)
        except FileExistsError:
            if path.is_symlink() or path.stat().st_size != len(data) or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ParseError("Existing content-addressed asset failed integrity validation")
        record = {"path": str(path), "sha256": digest,
                  "mime_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                  "page": page, "section": section, **metadata}
        self.assets.append(record)
        return str(path)

    def finish(self) -> ParsedDocument:
        if not self.blocks:
            raise ParseError(f"{self.parser} extracted no indexable content; check encryption, OCR and format")
        return ParsedDocument(self.blocks, self.assets, self.parser, getattr(self, "layout_objects", []), getattr(self, "extraction_evidence", {}))


def _heading(text: str) -> str | None:
    text = text.strip()
    if not text or len(text) > 160 or "\n" in text:
        return None
    if re.match(r"^#{1,6}\s+\S", text):
        return re.sub(r"^#{1,6}\s+|\s+#+$", "", text)
    if re.match(r"^\d+(?:\.\d+)*[.)]?\s+[A-Z]", text) and len(text) < 100:
        return text
    if len(text) < 100 and any(c.isalpha() for c in text) and text.isupper():
        return text
    return None


def _add_text(builder: _Builder, text: str, section: str = "Content",
              page: int | None = None) -> str:
    """Prefer paragraphs; headings change the section, never remove their text."""
    for paragraph in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        # Line headings are common in PDF text and Markdown without blank lines.
        pending: list[str] = []
        for line in paragraph.splitlines(keepends=True):
            heading = _heading(line)
            if heading:
                if pending:
                    builder.add("".join(pending), section, page)
                    pending = []
                section = heading
                builder.add(line, section, page, "heading")
            else:
                pending.append(line)
        if pending:
            builder.add("".join(pending), section, page)
    return section


def _ocr(image: Any) -> str:
    if not _enabled("OCR_ENABLED"):
        raise MissingDependency("Image/scanned-page text requires OCR_ENABLED=true, Pillow, pytesseract and local Tesseract")
    pytesseract = _dependency("pytesseract")
    try:
        return pytesseract.image_to_string(
            image, lang=os.environ.get("OCR_LANGUAGE", "eng"),
            timeout=_limit("OCR_TIMEOUT_SECONDS", 60),
        ).strip()
    except Exception as exc:
        raise ParseError(f"Local OCR failed ({type(exc).__name__}); check Tesseract and installed language data") from exc


def _open_image(data: bytes) -> Any:
    image_module = _dependency("PIL.Image", "Pillow")
    image = image_module.open(io.BytesIO(data))
    if image.width * image.height > _limit("PARSER_MAX_IMAGE_PIXELS", 40000000):
        image.close()
        raise ParserLimitError("Image exceeds PARSER_MAX_IMAGE_PIXELS")
    return image


def _parse_image(path: Path, builder: _Builder) -> None:
    data = path.read_bytes()
    with _open_image(data) as image:
        frames = getattr(image, "n_frames", 1)
        if frames > _limit("PARSER_MAX_PAGES", 10000):
            raise ParserLimitError("Image frame count exceeds PARSER_MAX_PAGES")
        for index in range(frames):
            image.seek(index)
            if image.width * image.height > _limit("PARSER_MAX_IMAGE_PIXELS", 40000000):
                raise ParserLimitError("Image frame exceeds PARSER_MAX_IMAGE_PIXELS")
            text = _ocr(image)
            if not text:
                raise ParseError(f"OCR found no text in image frame {index + 1}; supply a reviewed description")
            asset = builder.asset(data, path.suffix, page=index + 1, frame=index + 1,
                                  extraction="original image; OCR is not chart interpretation")
            builder.add(text, page=index + 1, kind="ocr", asset_path=asset)


def _parse_pdf(path: Path, builder: _Builder) -> None:
    pypdf = _dependency("pypdf")
    with path.open("rb") as source:
        reader = pypdf.PdfReader(source)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ParseError("Password-protected PDF must be decrypted by an authorized operator")
        if len(reader.pages) > _limit("PARSER_MAX_PAGES", 10000):
            raise ParserLimitError("PDF exceeds PARSER_MAX_PAGES")
        section = "Content"
        for page_no, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ""
            section = _add_text(builder, text, section, page_no)
            image_text: list[str] = []
            image_count = 0
            try:
                images: Iterable = page.images
                for image in images:
                    image_count += 1
                    asset = builder.asset(image.data, Path(image.name).suffix, section, page_no,
                                          extraction="PDF image; no automatic chart description")
                    builder.add(f"[PDF image {image_count}; descriptive interpretation pending]",
                                section, page_no, "image", asset)
                    if not text.strip():
                        with _open_image(image.data) as opened:
                            recognized = _ocr(opened)
                            if recognized:
                                image_text.append(recognized)
                                builder.add(recognized, section, page_no, "ocr", asset)
            except (ParseError, ImportError):
                raise
            except Exception as exc:
                if not text.strip():
                    raise ParseError(f"Cannot extract scanned PDF page {page_no}; use a locally provisioned OCR pipeline") from exc
                warnings.warn(f"PDF page {page_no}: some images could not be extracted ({type(exc).__name__})", RuntimeWarning)
            if image_count and not text.strip() and not image_text:
                raise ParseError(f"No OCR text found on scanned PDF page {page_no}; supply reviewed content")
            # Some scans paint inline/vector content rather than image XObjects.
            if not text.strip() and not image_count and page.get_contents() is not None:
                raise ParseError(f"PDF page {page_no} has drawing content but no extractable text/images; use provisioned Docling OCR")


def _docling_document(path: Path, page_images: bool = False) -> Any:
    artifacts = os.environ.get("DOCLING_ARTIFACTS_PATH", "")
    if not artifacts or not Path(artifacts).is_dir():
        raise MissingDependency("Docling requires preprovisioned local DOCLING_ARTIFACTS_PATH; automatic model downloads are disabled")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    options_module = _dependency("docling.datamodel.pipeline_options", "docling")
    converter_module = _dependency("docling.document_converter", "docling")
    base = _dependency("docling.datamodel.base_models", "docling")
    options = options_module.PdfPipelineOptions(artifacts_path=Path(artifacts))
    options.do_ocr = _enabled("OCR_ENABLED")
    if options.do_ocr:
        options.ocr_options = options_module.TesseractCliOcrOptions(
            lang=os.environ.get("OCR_LANGUAGE", "eng").split("+"))
    options.do_table_structure = True
    options.generate_picture_images = True
    options.generate_page_images = page_images
    options.images_scale = 1.5
    options.do_picture_description = False
    options.do_picture_classification = False
    options.do_code_enrichment = False
    options.do_formula_enrichment = False
    options.enable_remote_services = False
    converter = converter_module.DocumentConverter(
        allowed_formats=[base.InputFormat.PDF],
        format_options={base.InputFormat.PDF: converter_module.PdfFormatOption(pipeline_options=options)},
    )
    result = converter.convert(path, raises_on_error=True,
                               max_num_pages=_limit("PARSER_MAX_PAGES", 10000),
                               max_file_size=_limit("PARSER_MAX_FILE_BYTES", 134217728))
    if str(getattr(result, "status", "")).lower().split(".")[-1] != "success":
        raise ParseError("Docling did not report complete conversion success; partial output is not indexable")
    return result.document


def _parse_docling(path: Path, builder: _Builder) -> None:
    document = _docling_document(path)
    section = "Content"
    for item, _level in document.iterate_items():
        provenance = getattr(item, "prov", []) or []
        pages = list(dict.fromkeys(p.page_no for p in provenance))
        page = pages[0] if pages else None
        label = str(getattr(item, "label", "")).lower()
        if "section_header" in label or "title" in label:
            section = getattr(item, "text", section)
        if "table" in label and hasattr(item, "export_to_markdown"):
            text = item.export_to_markdown(doc=document)
        else:
            text = getattr(item, "text", "")
        if len(pages) > 1:
            # Contract supports one page per block: duplicate the source block
            # for citation provenance, rather than silently discard later pages.
            for item_page in pages:
                builder.add(text, section, item_page, "table" if "table" in label else "text")
        else:
            builder.add(text, section, page, "table" if "table" in label else "text")
        if "picture" in label and hasattr(item, "get_image"):
            image = item.get_image(document)
            if image is None:
                raise ParseError("Docling picture has no image; check generate_picture_images support")
            output = io.BytesIO()
            image.save(output, format="PNG")
            for item_page in pages or [None]:
                asset = builder.asset(output.getvalue(), ".png", section, item_page,
                                      extraction="Docling picture; chart description unavailable, review/enrichment required")
                builder.add("[Picture captured; chart description unavailable, reviewed enrichment required]",
                            section, item_page, "image", asset)


def _parse_docx(path: Path, builder: _Builder) -> None:
    _archive_guard(path)
    docx = _dependency("docx", "python-docx")
    paragraph_type = _dependency("docx.text.paragraph", "python-docx").Paragraph
    table_type = _dependency("docx.table", "python-docx").Table
    document = docx.Document(path)
    section = "Content"

    def images(element: Any, part: Any, current: str) -> None:
        for node in element.iter():
            if node.tag.endswith("}blip"):
                rid = node.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed")
            elif node.tag.endswith("}imagedata"):
                rid = node.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
            else:
                continue
            if not rid:
                continue
            image_part = part.related_parts.get(rid)
            if image_part is None:
                raise ParseError("DOCX image relationship points to a missing embedded object")
            suffix = Path(str(image_part.partname)).suffix
            asset = builder.asset(image_part.blob, suffix, current,
                                  extraction="embedded DOCX image; pagination not available")
            builder.add("[Embedded image; reviewed description pending]", current, kind="image", asset_path=asset)

    def table_text(table: Any) -> str:
        rows = []
        for row in table.rows:
            cells = []
            for cell in row.cells:
                pieces = []
                for child in cell._tc:
                    if child.tag.endswith("}p"):
                        pieces.append(paragraph_type(child, cell).text)
                    elif child.tag.endswith("}tbl"):
                        pieces.append(table_text(table_type(child, cell)))
                cells.append("\n".join(pieces))
            rows.append(" | ".join(cells))
        return "\n".join(rows)

    def body(element: Any, parent: Any, part: Any, initial: str) -> None:
        nonlocal section
        section = initial
        for child in element:
            if child.tag.endswith("}p"):
                paragraph = paragraph_type(child, parent)
                style = paragraph.style.name if paragraph.style else ""
                heading = style.lower().startswith(("heading", "title"))
                if heading and paragraph.text.strip():
                    section = paragraph.text.strip()
                builder.add(paragraph.text, section, kind="heading" if heading else "text")
            elif child.tag.endswith("}tbl"):
                builder.add(table_text(table_type(child, parent)), section, kind="table")
            images(child, part, section)

    body(document.element.body, document, document.part, "Content")
    seen_parts: set[str] = set()
    for doc_section in document.sections:
        for name in ("header", "first_page_header", "even_page_header", "footer", "first_page_footer", "even_page_footer"):
            container = getattr(doc_section, name)
            # Avoid creating empty header/footer parts while merely reading.
            if container.is_linked_to_previous:
                continue
            key = str(container.part.partname)
            if key not in seen_parts:
                seen_parts.add(key)
                body(container._element, container, container.part, name.replace("_", " ").title())


def _parse_doc(path: Path, builder: _Builder) -> None:
    executable = shutil.which("libreoffice") or shutil.which("soffice")
    textutil = shutil.which("textutil")
    if not executable and not textutil:
        raise MissingDependency("Legacy .doc requires local LibreOffice (preferred) or macOS textutil; convert to .docx")
    with tempfile.TemporaryDirectory(prefix="kb-doc-") as temporary:
        work = Path(temporary)
        # A stable filename prevents an untrusted source name becoming an option.
        source = work / "source.doc"
        shutil.copyfile(path, source)
        output = work / "source.docx"
        if executable:
            profile = (work / "profile").as_uri()
            command = [executable, f"-env:UserInstallation={profile}", "--headless", "--convert-to", "docx", "--outdir", str(work), str(source)]
        else:
            command = [textutil, "-convert", "docx", "-output", str(output), str(source)]
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, cwd=work, check=False, shell=False,
                                    timeout=_limit("DOC_CONVERSION_TIMEOUT_SECONDS", 120))
        except subprocess.TimeoutExpired as exc:
            raise ParseError("Legacy Word conversion timed out; inspect the document in an isolated converter") from exc
        if result.returncode or not output.is_file():
            raise ParseError("Legacy Word conversion failed; convert manually to a validated .docx")
        if output.stat().st_size > _limit("PARSER_MAX_FILE_BYTES", 134217728):
            raise ParserLimitError("Converted Word file exceeds PARSER_MAX_FILE_BYTES")
        _parse_docx(output, builder)


def _parse_xml(path: Path, builder: _Builder) -> None:
    safe_sax = _dependency("defusedxml.sax", "defusedxml")

    class Handler(ContentHandler):
        def __init__(self) -> None:
            super().__init__()
            self.stack: list[str] = []
            self.pending: list[str] = []
            self.size = 0

        def flush(self) -> None:
            if self.pending:
                builder.add("".join(self.pending), "/".join(self.stack) or "XML")
                self.pending = []
                self.size = 0

        def startElement(self, name: str, attrs: Any) -> None:
            self.flush()
            self.stack.append(name)
            if len(self.stack) > _limit("PARSER_MAX_XML_DEPTH", 128):
                raise ParserLimitError("XML exceeds PARSER_MAX_XML_DEPTH")
            if attrs.getLength():
                builder.add(" ".join(f"{key}={attrs[key]}" for key in attrs.getNames()), "/".join(self.stack), kind="metadata")

        def endElement(self, name: str) -> None:
            self.flush()
            self.stack.pop()

        def characters(self, content: str) -> None:
            self.pending.append(content)
            self.size += len(content)
            if self.size >= 65536:
                self.flush()

    with path.open("rb") as source:
        safe_sax.parse(source, Handler(), forbid_dtd=True, forbid_entities=True, forbid_external=True)


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        if not self.hidden and tag in {"br", "p", "div", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1
        if not self.hidden and tag in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def _html_text(text: str) -> str:
    parser = _HTMLText()
    parser.feed(text)
    return "".join(parser.parts)


def _parse_eml(path: Path, builder: _Builder) -> None:
    with path.open("rb") as source:
        message = BytesParser(policy=policy.default).parse(source)
    for name in ("Subject", "From", "To", "Cc", "Date", "Message-ID"):
        for value in message.get_all(name, []):
            builder.add(f"{name}: {value}", "Email headers", kind="metadata")
    body_count = 0
    for part in message.walk():
        if part.is_multipart():
            continue
        mime = part.get_content_type()
        disposition = part.get_content_disposition()
        filename = part.get_filename()
        if mime.startswith("image/"):
            suffix = Path(filename or "").suffix or mimetypes.guess_extension(mime) or ".bin"
            asset = builder.asset(part.get_payload(decode=True) or b"", suffix, "Email attachments",
                                  filename=filename, content_id=str(part.get("Content-ID", "")))
            builder.add("[Email image attachment; reviewed description pending]", "Email attachments", kind="image", asset_path=asset)
        elif disposition == "attachment" or filename:
            # Attachments are retained as assets but never recursively executed/extracted.
            data = part.get_payload(decode=True) or b""
            if data:
                asset = builder.asset(data, Path(filename or "attachment.bin").suffix,
                                      "Email attachments", filename=filename)
                builder.add(f"[Attachment: {filename or mime}; contents not recursively indexed]",
                            "Email attachments", kind="attachment", asset_path=asset)
        elif mime in {"text/plain", "text/html"}:
            content = part.get_content()
            if isinstance(content, bytes):
                content = content.decode(part.get_content_charset() or "utf-8", errors="strict")
            if mime == "text/html":
                content = _html_text(content)
            if content.strip():
                body_count += 1
                _add_text(builder, content, "Email body")
    if not body_count and not builder.assets:
        raise ParseError("EML contains no readable body or attachments; headers alone are not indexed")


def _parse_msg(path: Path, builder: _Builder) -> None:
    extract_msg = _dependency("extract_msg", "extract-msg")
    with extract_msg.openMsg(str(path)) as message:
        for key in ("subject", "sender", "to", "cc", "date"):
            value = getattr(message, key, None)
            if value:
                builder.add(f"{key}: {value}", "Email headers", kind="metadata")
        body = getattr(message, "body", None)
        if not body:
            html = getattr(message, "htmlBody", b"") or b""
            body = _html_text(html.decode("utf-8", errors="strict") if isinstance(html, bytes) else html)
        _add_text(builder, body or "", "Email body")
        attachment_count = 0
        for attachment in message.attachments:
            data = getattr(attachment, "data", None)
            if not isinstance(data, bytes):
                raise UnsupportedFormat("Embedded MSG objects require explicit offline extraction; convert the message to EML")
            filename = getattr(attachment, "longFilename", None) or getattr(attachment, "shortFilename", None) or "attachment.bin"
            asset = builder.asset(data, Path(filename).suffix, "Email attachments", filename=filename)
            builder.add(f"[Attachment: {filename}; contents not recursively indexed]", "Email attachments",
                        kind="attachment", asset_path=asset)
            attachment_count += 1
        if not (body or "").strip() and not attachment_count:
            raise ParseError("MSG contains no readable body or attachments")


def _parse_excel(path: Path, builder: _Builder) -> None:
    maximum = _limit("PARSER_MAX_CELLS", 1000000)
    count = 0
    if path.suffix.lower() == ".xls":
        xlrd = _dependency("xlrd")
        workbook = xlrd.open_workbook(str(path), on_demand=True)
        try:
            for sheet in workbook.sheets():
                if count + sheet.nrows * sheet.ncols > maximum:
                    raise ParserLimitError("Spreadsheet exceeds PARSER_MAX_CELLS")
                for index in range(sheet.nrows):
                    count += sheet.ncols
                    values = sheet.row_values(index)
                    if any(value != "" for value in values):
                        builder.add(f"Row {index + 1}: " + " | ".join(map(str, values)), sheet.name, kind="table")
        finally:
            workbook.release_resources()
    else:
        _archive_guard(path)
        openpyxl = _dependency("openpyxl")
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=False, keep_links=False)
        try:
            for sheet in workbook.worksheets:
                # Ignore stale worksheet dimensions, but apply an explicit cell budget.
                sheet.reset_dimensions()
                for index, row in enumerate(sheet.iter_rows(values_only=True), 1):
                    count += len(row)
                    if count > maximum or index > maximum:
                        raise ParserLimitError("Spreadsheet exceeds PARSER_MAX_CELLS")
                    if any(value is not None for value in row):
                        builder.add(f"Row {index}: " + " | ".join("" if v is None else str(v) for v in row),
                                    sheet.title, kind="table")
        finally:
            workbook.close()


def _text_encoding(path: Path) -> str:
    with path.open("rb") as source:
        head = source.read(4096)
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    if b"\x00" in head:
        raise ParseError("Text contains NUL bytes; provide a known text encoding or correct file format")
    # Validate the entire bounded input incrementally; never silently replace bytes.
    import codecs
    decoder = codecs.getincrementaldecoder("utf-8-sig")()
    try:
        with path.open("rb") as source:
            while data := source.read(65536):
                decoder.decode(data)
            decoder.decode(b"", final=True)
        return "utf-8-sig"
    except UnicodeDecodeError:
        return "cp1252"


def _parse_text(path: Path, builder: _Builder) -> None:
    encoding = _text_encoding(path)
    with path.open(encoding=encoding, newline="") as source:
        if path.suffix.lower() == ".csv":
            sample = source.read(8192)
            source.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            except csv.Error:
                dialect = csv.excel
            # Keep the standard library's bounded field limit; errors are explicit.
            cells = 0
            for index, row in enumerate(csv.reader(source, dialect), 1):
                cells += len(row)
                if cells > _limit("PARSER_MAX_CELLS", 1000000):
                    raise ParserLimitError("CSV exceeds PARSER_MAX_CELLS")
                if any(value.strip() for value in row):
                    builder.add(f"Row {index}: " + " | ".join(row), "CSV", kind="table")
        else:
            section = "Content"
            pending: list[str] = []
            size = 0
            while line := source.readline(65536):
                heading = _heading(line)
                if heading or not line.strip() or size + len(line) > 65536:
                    if pending:
                        builder.add("".join(pending), section)
                        pending = []
                        size = 0
                if heading:
                    section = heading
                    builder.add(line, section, kind="heading")
                else:
                    pending.append(line)
                    size += len(line)
            if pending:
                builder.add("".join(pending), section)


def _parse_parquet(path: Path, builder: _Builder) -> None:
    parquet = _dependency("pyarrow.parquet", "pyarrow")
    # Footer/schema inspection only: no read_table, iter_batches, or row values.
    with parquet.ParquetFile(path) as source:
        builder.add("Parquet schema (metadata only; no row values)\n" + str(source.schema_arrow),
                    "Structured metadata", kind="metadata")
        builder.add(f"Row groups: {source.metadata.num_row_groups}; row count: {source.metadata.num_rows}",
                    "Structured metadata", kind="metadata")


from kb.pdf_render_lock import serialize_pdf_processing


@traced('ingestion.parse')
@serialize_pdf_processing
def parse_document(path: Path, asset_dir: Path) -> ParsedDocument:
    """Extract a local file or raise ParseError; never download models or run a shell.

    Assets are content-addressed local files. A failed parse can leave assets for
    the ingestion job's lifecycle manager to remove; no partial document returns.
    """
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ParseError("Input is not a readable regular file")
    suffix = path.suffix.lower()
    if suffix in {".bz2", ".gz", ".xz", ".zip", ".7z", ".tar"}:
        raise UnsupportedFormat("Compressed archives/wiki dumps are not automatically decompressed. Use an approved offline streaming decompression/splitting job, then onboard bounded .xml/.txt documents.")
    size = path.stat().st_size
    if not size:
        raise ParseError("Input file is empty")
    if size > _limit("PARSER_MAX_FILE_BYTES", 134217728):
        raise ParserLimitError("Input exceeds PARSER_MAX_FILE_BYTES; split it in an approved offline job")
    selected_pdf = os.environ.get("PDF_PARSER", "pypdf").lower()
    functions = {
        ".docx": ("python-docx", _parse_docx), ".doc": ("doc-conversion/python-docx", _parse_doc),
        ".xml": ("defusedxml", _parse_xml), ".eml": ("email", _parse_eml),
        ".msg": ("extract-msg", _parse_msg), ".xls": ("xlrd", _parse_excel),
        ".xlsx": ("openpyxl", _parse_excel), ".xlsm": ("openpyxl", _parse_excel),
        ".parquet": ("pyarrow-metadata", _parse_parquet),
    }
    if suffix == ".pdf":
        if selected_pdf not in {"pypdf", "docling", "docling_hybrid"}:
            raise ParseError("PDF_PARSER must be pypdf, docling or docling_hybrid; no implicit fallback")
        if selected_pdf == "docling_hybrid":
            from kb.pdf_hybrid import parse_hybrid_pdf
            parser, function = "docling+pypdf+pdfplumber", parse_hybrid_pdf
        else:
            parser, function = selected_pdf, _parse_docling if selected_pdf == "docling" else _parse_pdf
    elif suffix in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}:
        parser, function = "pytesseract", _parse_image
    elif suffix in {".csv", ".txt", ".log", ".md", ".markdown", ".tsv"}:
        parser, function = "csv" if suffix == ".csv" else "text", _parse_text
    elif suffix in functions:
        parser, function = functions[suffix]
    else:
        raise UnsupportedFormat(f"Unsupported extension {suffix or '(none)'}; convert explicitly to PDF, DOCX, EML, XML or UTF-8 text")
    try:
        builder = _Builder(Path(asset_dir), parser)
        function(path, builder)
        return builder.finish()
    except ParseError:
        raise
    except Exception as exc:
        # Do not include file contents, credentials, or third-party exception text.
        raise ParseError(f"{parser} extraction failed ({type(exc).__name__}); inspect the source format/dependencies and configured limits") from exc


def chunk_document(parsed: ParsedDocument, max_chars: int = 1800, overlap: int = 200) -> list[dict]:
    """Chunk by consecutive section, then paragraph/sentence/word boundaries.

    Every character of every nonempty block survives in at least one chunk,
    including arbitrarily long unbroken strings. Overlap never crosses sections.
    Page/asset metadata is computed from intersecting source spans, not guessed
    from the first page, so a semantic chunk can cite multiple source pages.
    """
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars <= 0:
        raise ValueError("max_chars must be a positive integer")
    if not isinstance(overlap, int) or isinstance(overlap, bool) or not 0 <= overlap < max_chars:
        raise ValueError("overlap must be an integer in [0, max_chars)")
    chunks: list[dict] = []
    max_chunks = _limit("PARSER_MAX_CHUNKS", 100000)
    asset_index: dict[str, list[dict]] = {}
    for asset in parsed.assets:
        if asset.get("path"):
            asset_index.setdefault(str(asset["path"]), []).append(asset)
    group: list[ParsedBlock] = []

    def emit(blocks: list[ParsedBlock]) -> None:
        pieces: list[str] = []
        spans: list[tuple[int, int, ParsedBlock]] = []
        position = 0
        for block in blocks:
            if not block.text:
                continue
            if pieces:
                pieces.append("\n\n")
                position += 2
            start = position
            pieces.append(block.text)
            position += len(block.text)
            spans.append((start, position, block))
        text = "".join(pieces)
        start = 0
        span_cursor = 0
        while start < len(text):
            end = min(start + max_chars, len(text))
            if end < len(text):
                minimum = start + max(overlap + 1, max_chars // 2)
                # Prefer semantic breaks, then whitespace, then a hard window.
                for pattern in (r"\n\s*\n", r"[.!?](?:[\"')\]]*)\s+", r"\s+"):
                    candidates = [match.end() for match in re.finditer(pattern, text[minimum:end])]
                    if candidates:
                        end = minimum + candidates[-1]
                        break
            while span_cursor < len(spans) and spans[span_cursor][1] <= start:
                span_cursor += 1
            pages: list[int] = []
            assets: list[dict] = []
            objects: list[dict] = []
            content_kinds: list[str] = []
            source_block_indices: list[int] = []
            seen_assets: set[tuple[str, Any]] = set()
            for span_index in range(span_cursor, len(spans)):
                block_start, block_end, block = spans[span_index]
                if block_start >= end:
                    break
                if block_end <= start:
                    continue
                if 'sequence' in block.metadata and block.metadata['sequence'] not in source_block_indices:
                    source_block_indices.append(block.metadata['sequence'])
                if block.kind not in content_kinds:
                    content_kinds.append(block.kind)
                if block.metadata.get('object_id') and block.metadata not in objects:
                    objects.append(dict(block.metadata))
                if block.page is not None and block.page not in pages:
                    pages.append(block.page)
                if block.asset_path:
                    records = asset_index.get(block.asset_path) or [{"path": block.asset_path, "page": block.page, "section": block.section}]
                    for asset in records:
                        # Reused images on other pages should not invent citations.
                        if asset.get("page") not in (None, block.page):
                            continue
                        if asset.get("section") not in (None, block.section):
                            continue
                        key = (str(asset.get("path")), asset.get("page"))
                        if key not in seen_assets:
                            seen_assets.add(key)
                            assets.append(dict(asset))
            if len(chunks) >= max_chunks:
                raise ParserLimitError("Chunk count exceeds PARSER_MAX_CHUNKS; increase max_chars or reduce overlap")
            chunk = {"text": text[start:end], "section": blocks[0].section,
                     "page": pages[0] if pages else None, "pages": pages, "assets": assets,
                     "objects": objects, "content_kinds": content_kinds,
                     "source_block_indices": source_block_indices, "sequence": len(chunks)}
            visual_assets = [asset for asset in assets if asset.get('object_id') and asset.get('mime_type', '').startswith('image/')]
            if len(visual_assets) > 1:
                raise ParseError('A visual chunk must reference exactly one object image')
            if visual_assets:
                chunk['image_asset'] = dict(visual_assets[0])
            chunks.append(chunk)
            if end == len(text):
                break
            start = end - overlap

    for block in parsed.blocks:
        if group and (block.section != group[0].section
                      or (block.metadata.get('object_id') if block.asset_path else None)
                         != (group[0].metadata.get('object_id') if group[0].asset_path else None)):
            emit(group)
            group = []
        group.append(block)
    if group:
        emit(group)
    if not chunks or not any(chunk["text"].strip() for chunk in chunks):
        raise ParseError("Cannot chunk a document with no readable blocks")
    return chunks
