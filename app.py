"""
Bindery — merge PDFs into a single bound document with an auto-generated
table of contents and page numbering.

This is a from-scratch web rewrite of a desktop tkinter tool. Notable
changes from the original, beyond the UI move:

- SSRF guard: the original tool ran on one person's machine, so letting it
  fetch arbitrary URLs was harmless. As a public web service, an endpoint
  that fetches a user-supplied URL server-side is a classic SSRF vector
  (it could be pointed at internal/cloud-metadata addresses). `is_safe_url`
  restricts to http/https and blocks loopback/private/link-local targets.
- Remote PDFs are downloaded once, not twice. The original fetched each
  URL to read its title, then fetched it again during merge. This version
  only ever GETs a remote PDF at merge time; the title-check step uses a
  cheap HEAD request.
- Streaming downloads with a hard byte cap, instead of trusting
  Content-Length (which a server can lie about) or buffering an unbounded
  response.
- The table of contents now paginates instead of silently truncating once
  a page fills up.
- Page-number stamping and title extraction no longer depend on the
  `pdfnumbering`/`fpdf` packages (small, low-maintenance libraries) —
  it's implemented directly on top of pypdf + reportlab, which this
  project already needs.
- No global mutable UI state to thread through callbacks; each request is
  handled by pure functions that take input and return output.
"""

from __future__ import annotations

import io
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

import requests
from flask import Flask, jsonify, render_template, request, send_file
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename

app = Flask(__name__)

# A merge request itself is capped much higher; this guards a single
# multipart parse from ballooning memory before our own per-file checks run.
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB

MAX_SOURCE_BYTES = 50 * 1024 * 1024  # per PDF, local or remote
MAX_ITEMS = 60
DOWNLOAD_CHUNK = 64 * 1024
REQUEST_TIMEOUT = 15

TOC_TITLE_MAX_WIDTH = letter[0] - 2.5 * inch
LINE_HEIGHT = 0.3 * inch
FIRST_PAGE_TOP = letter[1] - 1.5 * inch
CONT_PAGE_TOP = letter[1] - 1.15 * inch
BOTTOM_MARGIN = inch


class BinderyError(ValueError):
    """A problem with the request that should be shown to the user as-is."""


# --------------------------------------------------------------------------
# Safety
# --------------------------------------------------------------------------

def is_safe_url(url: str) -> tuple[bool, str]:
    """Block anything that isn't a plain public http(s) PDF URL."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False, "That URL couldn't be parsed."

    if parsed.scheme not in ("http", "https"):
        return False, "Only http:// and https:// URLs are supported."
    if not parsed.hostname:
        return False, "That URL is missing a host."

    hostname = parsed.hostname.lower()
    if hostname in ("localhost", "metadata.google.internal"):
        return False, "That host isn't allowed."

    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False, "That host couldn't be resolved."

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False, "That URL points to a non-public address."

    return True, ""


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def download_pdf(url: str) -> bytes:
    safe, reason = is_safe_url(url)
    if not safe:
        raise BinderyError(reason)

    try:
        resp = requests.get(url, stream=True, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
        raise BinderyError(f"Couldn't download that URL ({exc}).") from exc

    content_type = resp.headers.get("content-type", "").lower()
    if "application/pdf" not in content_type and not url.lower().endswith(".pdf"):
        raise BinderyError("That URL doesn't point to a PDF.")

    chunks = []
    total = 0
    for chunk in resp.iter_content(DOWNLOAD_CHUNK):
        total += len(chunk)
        if total > MAX_SOURCE_BYTES:
            raise BinderyError("That PDF is larger than the 50 MB limit.")
        chunks.append(chunk)
    return b"".join(chunks)


def read_pdf(data: bytes, label: str) -> PdfReader:
    try:
        reader = PdfReader(io.BytesIO(data))
    except PdfReadError as exc:
        raise BinderyError(f'"{label}" doesn\'t look like a valid PDF.') from exc

    if reader.is_encrypted:
        try:
            # Only handles PDFs encrypted with an empty user password.
            if reader.decrypt("") == 0:
                raise BinderyError(f'"{label}" is password-protected and can\'t be merged.')
        except NotImplementedError as exc:
            raise BinderyError(f'"{label}" uses unsupported encryption.') from exc

    if len(reader.pages) == 0:
        raise BinderyError(f'"{label}" has no pages.')

    return reader


def guess_title(reader: PdfReader, fallback: str) -> str:
    meta_title = reader.metadata.title if reader.metadata else None
    if meta_title and meta_title.strip():
        return meta_title.strip()
    return fallback


def filename_from_url(url: str) -> str:
    path = urlparse(url).path
    name = path.rsplit("/", 1)[-1] or url
    return name or "Untitled"


# --------------------------------------------------------------------------
# Table of contents
# --------------------------------------------------------------------------

def _truncate_to_width(text: str, font: str, size: float, max_width: float) -> str:
    if stringWidth(text, font, size) <= max_width:
        return text
    truncated = text
    while truncated and stringWidth(truncated + "...", font, size) > max_width:
        truncated = truncated[:-1]
    return (truncated + "...") if truncated else "..."


def build_toc_pages(entries: list[tuple[str, int]]) -> list:
    """Render the TOC as one or more pages, returned as pypdf page objects."""
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter)
    width, _height = letter

    entries_per_first_page = int((FIRST_PAGE_TOP - BOTTOM_MARGIN) / LINE_HEIGHT)
    entries_per_cont_page = int((CONT_PAGE_TOP - BOTTOM_MARGIN) / LINE_HEIGHT)

    remaining = list(entries) or [("(No entries)", 1)]
    first_page = True

    while remaining:
        limit = entries_per_first_page if first_page else entries_per_cont_page
        page_entries, remaining = remaining[:limit], remaining[limit:]

        pdf.setFont("Helvetica-Bold", 18 if first_page else 12)
        title = "Table of Contents" if first_page else "Table of Contents (continued)"
        pdf.drawCentredString(width / 2.0, letter[1] - inch, title)

        y = FIRST_PAGE_TOP if first_page else CONT_PAGE_TOP
        pdf.setFont("Helvetica", 12)
        for entry_title, page_num in page_entries:
            display = _truncate_to_width(entry_title, "Helvetica", 12, TOC_TITLE_MAX_WIDTH)
            pdf.drawString(inch, y, display)
            pdf.drawRightString(width - inch, y, str(page_num))
            y -= LINE_HEIGHT

        pdf.showPage()
        first_page = False

    pdf.save()
    buffer.seek(0)
    return list(PdfReader(buffer).pages)


# --------------------------------------------------------------------------
# Page numbering
# --------------------------------------------------------------------------

def stamp_page_numbers(writer: PdfWriter, start_index: int, total: int) -> None:
    for offset in range(total):
        page = writer.pages[start_index + offset]
        page_w = float(page.mediabox.width)
        page_h = float(page.mediabox.height)

        overlay_buffer = io.BytesIO()
        pdf = canvas.Canvas(overlay_buffer, pagesize=(page_w, page_h))
        pdf.setFont("Helvetica", 10)
        pdf.setFillColorRGB(0x18 / 255, 0x18 / 255, 0x18 / 255)
        pdf.drawCentredString(page_w / 2.0, 10, f"{offset + 1} of {total}")
        pdf.save()
        overlay_buffer.seek(0)

        overlay_page = PdfReader(overlay_buffer).pages[0]
        page.merge_page(overlay_page)


# --------------------------------------------------------------------------
# Item model + merge
# --------------------------------------------------------------------------

@dataclass
class MergeItem:
    item_id: str
    kind: str  # "file" | "url"
    title: str
    url: str | None = None


def parse_meta(raw_meta: list) -> list[MergeItem]:
    if not isinstance(raw_meta, list):
        raise BinderyError("Malformed request.")
    if not raw_meta:
        raise BinderyError("Add at least one PDF first.")
    if len(raw_meta) > MAX_ITEMS:
        raise BinderyError(f"Please merge {MAX_ITEMS} files or fewer at a time.")

    items = []
    for raw in raw_meta:
        if not isinstance(raw, dict):
            raise BinderyError("Malformed request.")
        item_id = str(raw.get("id", "")).strip()
        kind = raw.get("kind")
        title = str(raw.get("title", "")).strip()
        url = raw.get("url")
        if not item_id or kind not in ("file", "url"):
            raise BinderyError("Malformed request.")
        if kind == "url" and not url:
            raise BinderyError("Malformed request.")
        items.append(MergeItem(item_id=item_id, kind=kind, title=title, url=url))
    return items


def perform_merge(items: list[MergeItem], files_by_id: dict) -> tuple[bytes, list[str]]:
    warnings: list[str] = []
    readers: list[PdfReader] = []
    titles: list[str] = []

    for item in items:
        if item.kind == "file":
            storage = files_by_id.get(item.item_id)
            if storage is None:
                raise BinderyError("A file went missing from the upload — please try again.")
            data = storage.read()
            if len(data) > MAX_SOURCE_BYTES:
                raise BinderyError(f'"{storage.filename}" is larger than the 50 MB limit.')
            label = item.title or secure_filename(storage.filename or "Untitled")
            reader = read_pdf(data, label)
            title = item.title or guess_title(reader, label)
        else:
            label = item.title or filename_from_url(item.url)
            data = download_pdf(item.url)
            reader = read_pdf(data, label)
            title = item.title or guess_title(reader, label)

        readers.append(reader)
        titles.append(title)

    # Pass 1: figure out where each document will land, and how long the
    # TOC itself will be, before we actually assemble anything.
    entries_per_first_page = int((FIRST_PAGE_TOP - BOTTOM_MARGIN) / LINE_HEIGHT)
    entries_per_cont_page = int((CONT_PAGE_TOP - BOTTOM_MARGIN) / LINE_HEIGHT)
    n = len(readers)
    if n <= entries_per_first_page:
        toc_page_count = 1
    else:
        toc_page_count = 1 + -(-(n - entries_per_first_page) // entries_per_cont_page)

    toc_entries = []
    running_page = toc_page_count + 1
    for reader, title in zip(readers, titles):
        toc_entries.append((title, running_page))
        running_page += len(reader.pages)
    total_content_pages = running_page - (toc_page_count + 1)

    # Pass 2: assemble.
    writer = PdfWriter()
    for toc_page in build_toc_pages(toc_entries):
        writer.add_page(toc_page)
    for reader in readers:
        for page in reader.pages:
            writer.add_page(page)

    stamp_page_numbers(writer, start_index=toc_page_count, total=total_content_pages)

    out = io.BytesIO()
    writer.write(out)
    out.seek(0)
    return out.read(), warnings


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.get("/")
def index():
    return render_template("index.html")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/api/inspect")
def api_inspect():
    """Local file upload: extract a title + page count immediately."""
    storage = request.files.get("file")
    if storage is None:
        return jsonify(error="No file provided."), 400

    data = storage.read()
    if len(data) > MAX_SOURCE_BYTES:
        return jsonify(error="That PDF is larger than the 50 MB limit."), 400

    try:
        reader = read_pdf(data, storage.filename or "Untitled")
    except BinderyError as exc:
        return jsonify(error=str(exc)), 400

    title = guess_title(reader, storage.filename or "Untitled")
    return jsonify(title=title, pages=len(reader.pages))


@app.post("/api/check-url")
def api_check_url():
    """Cheap validation for a pasted URL: HEAD only, no download."""
    payload = request.get_json(silent=True) or {}
    url = str(payload.get("url", "")).strip()
    if not url:
        return jsonify(error="No URL provided."), 400

    safe, reason = is_safe_url(url)
    if not safe:
        return jsonify(error=reason), 400

    try:
        resp = requests.head(url, allow_redirects=True, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
        return jsonify(error=f"Couldn't reach that URL ({exc})."), 400

    content_type = resp.headers.get("content-type", "").lower()
    if "application/pdf" not in content_type and not url.lower().endswith(".pdf"):
        return jsonify(error="That URL doesn't look like a PDF."), 400

    return jsonify(title=filename_from_url(url))


@app.post("/api/merge")
def api_merge():
    import json

    raw_meta = request.form.get("meta", "")
    try:
        meta = json.loads(raw_meta)
        items = parse_meta(meta)
    except (json.JSONDecodeError, BinderyError) as exc:
        return jsonify(error=str(exc)), 400

    files_by_id = {
        key[len("file_"):]: storage
        for key, storage in request.files.items()
        if key.startswith("file_")
    }

    filename = secure_filename(request.form.get("filename", "") or "") or "merged.pdf"
    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"

    try:
        pdf_bytes, warnings = perform_merge(items, files_by_id)
    except BinderyError as exc:
        return jsonify(error=str(exc)), 400
    except Exception as exc:  # noqa: BLE001 - surface a clean message, not a trace
        return jsonify(error=f"Something went wrong while merging: {exc}"), 500

    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
    )


@app.errorhandler(413)
def too_large(_exc):
    return jsonify(error="That upload is too large."), 413


@app.errorhandler(HTTPException)
def handle_http_exception(exc):
    return jsonify(error=exc.description), exc.code


if __name__ == "__main__":
    app.run(debug=True, port=5000)
