# Bindery

Merge PDFs into one document with an auto-generated table of contents and
numbered pages — a web rewrite of a tkinter desktop tool, built to run on
[Render](https://render.com).

Flask backend + a single-page vanilla JS/HTML/CSS frontend. No build step,
no database, nothing stored on disk — every merge is handled in memory
within one request.

## What changed from the desktop version

- **UI**: tkinter's file dialogs and treeview became a browser page — drag
  to reorder, click a title to rename it, add PDFs from disk or by URL.
- **SSRF guard**: the desktop app could safely fetch any URL because it
  only ever ran on one person's machine. A web service that fetches
  user-supplied URLs server-side is a standard SSRF risk, so `app.py`
  resolves the hostname and rejects loopback/private/link-local targets,
  and restricts to `http`/`https`.
- **No double-downloading**: the original fetched a remote PDF once to
  read its title and again during merge. This version only downloads a
  remote PDF once, at merge time; adding a URL just does a cheap HEAD
  check first.
- **Streaming downloads with a real size cap** (50 MB/file) instead of
  trusting `Content-Length` or buffering an unbounded response.
- **Multi-page table of contents** — the original silently stopped adding
  entries once one page filled up; this one paginates.
- **No more `pdfnumbering`/`fpdf` dependency** — page-number stamping and
  title extraction are implemented directly on pypdf + reportlab, which
  the project needs anyway.
- Business logic lives in small functions that take input and return
  output, rather than a set of callbacks closing over global widget state.

## Run it locally

```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Open http://localhost:5000.

## Deploy to Render

1. Push this folder to a GitHub repo.
2. In Render: **New +** → **Web Service** → connect the repo.
   - `render.yaml` is picked up automatically if you use **New +** →
     **Blueprint** instead — that sets everything below for you.
3. If configuring by hand:
   - **Environment**: Python 3
   - **Build command**: `pip install -r requirements.txt`
   - **Start command**: `gunicorn app:app`
   - **Health check path**: `/healthz`
4. Deploy. Render gives you a `https://<your-service>.onrender.com` URL.

No environment variables or database are required. The free plan works;
it just spins down when idle, so the first request after a while takes a
few extra seconds.

## Project layout

```
app.py                        Flask backend — routes, merge logic, safety checks
templates/index.html          Page shell
static/css/style.css          Dark theme
static/js/app.js              Queue state, drag-reorder, dialogs, merge request
config/sales_offices.json     Editable office directory for the cover-page dropdown
requirements.txt
Procfile                      For platforms that read it directly
render.yaml                   Render Blueprint (optional one-click config)
```

## Cover page

Three modes, chosen per merge:

- **None** — the output starts with the table of contents, as before.
- **Upload a PDF** — bring your own cover (or multi-page front matter); all
  its pages are prepended and excluded from page numbering, same as the TOC.
- **Generate a cover sheet** — fill in a form (title, contracting team
  contact, sales office, "Prepared For" block, project name/location/date)
  and Bindery renders a single cover page. An optional logo image can be
  uploaded per-merge and is scaled to fit a header box.

**Before this is usable for real submittals, edit `config/sales_offices.json`**
with your actual office names, addresses, and phone numbers — it ships with
two placeholder rows so the dropdown has something to show. Selecting an
office just pre-fills the name/address/phone fields below it; they stay
editable, and "Custom / not listed" clears them for manual entry.

**On the logo:** Bindery doesn't source or embed your company logo itself —
that's your trademarked asset. Upload your own approved logo file (PNG or
JPG) each time you generate a cover, or bake a default in by editing
`build_cover_page()` in `app.py` if you'd rather not upload it every time.

The generated cover's accent color is a placeholder professional blue
(`COVER_ACCENT_RGB` near the top of `app.py`) — not a verified brand color.
Update that one constant to match your actual brand guidelines.

## Limits (adjust in `app.py` if needed)

- 50 MB per source PDF (`MAX_SOURCE_BYTES`)
- 60 items per merge (`MAX_ITEMS`)
- 200 MB per request overall (`MAX_CONTENT_LENGTH`)
- 15s timeout per URL fetch (`REQUEST_TIMEOUT`)
