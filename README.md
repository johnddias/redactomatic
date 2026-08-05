# Redactomatic

A small Flask web app for redacting PII from PDF statements. Upload a PDF and
a control file (list of names, account numbers, addresses, etc.), and it
returns a copy with every match blacked out and page metadata stripped. For
recognized brokerage/bank statement layouts, it also extracts the
holdings/transaction tables into a markdown sidecar file, since redaction
otherwise makes that data unreadable.

## Features

- **Term-based redaction** — every occurrence of each line in the control
  file gets covered with a solid black box (`app/redactor.py`), and SSNs are
  matched even when a form splits them across separate text boxes.
- **Account-number redaction** — `(Acct # NNNN...)`-style references are
  found and redacted automatically, without needing to be listed in the
  control file.
- **Metadata stripped** — document info and any XMP metadata stream are
  removed from the output PDF.
- **Table extraction** — for recognized statement vendors, every
  holdings/transaction row is pulled into a `<name>.tables.md` file
  alongside the redacted PDF:
  - **J.P. Morgan Securities (JPMS)** brokerage statements → holdings
    (description, quantity, price, market value, cost basis, gain/loss, ...)
  - **Chase** checking statements → transactions (date, merchant,
    description, amount, ...)

  Unrecognized documents are redacted only; no table file is produced.
- **Batch mode** — upload many PDFs against one control file and download a
  single zip (redacted PDFs + tables + a manifest) once the background job
  finishes.

## Running it

### Docker (recommended)

```bash
docker compose up -d --build
```

The app listens on `http://localhost:8080` (mapped from container port
`5000` — see `docker-compose.yml`). Uploaded and output files persist in the
`redact-data` volume, mounted at `/data`.

### Locally

```bash
pip install -r requirements.txt
python app/main.py
```

Runs the Flask dev server on `http://localhost:5000`. Set `UPLOAD_FOLDER` to
control where files are stored (defaults to `/data`).

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `UPLOAD_FOLDER` | `/data` | Where per-session uploads and outputs are written |
| `MAX_CONTENT_MB` | `100` | Max upload size in MB |
| `SECRET_KEY` | random | Flask session secret; set this to a fixed value to avoid invalidating sessions on restart |

## Control file format

One PII term per line; lines starting with `#` are treated as comments. See
[example-control.txt](example-control.txt):

```
# --- Names ---
John Smith
Jane Doe

# --- Account / ID numbers ---
123-45-6789
4111 1111 1111 1111
```

## API

- `POST /redact` — form fields `pdf_file` and `control_file`. Returns JSON
  with the redaction count and download tokens for the redacted PDF and (if
  produced) the tables markdown file.
- `POST /batch` — form fields `files` (multiple), `control_file`, and
  optional `build_tables` (`"false"` to skip table extraction). Returns a
  `job_id` for polling.
- `GET /batch/<job_id>/status` — poll job progress; includes a
  `zip_download_token` once complete.
- `POST /batch/<job_id>/cancel` — cancel a running batch job.
- `GET /download/<token>` — stream back a redacted PDF, tables file, or
  batch zip.

## Adding a new statement vendor

Table extraction is dispatched by `app/document_extractors/classifier.py`,
which matches marker strings on the document's first page against a vendor
key. To support a new vendor:

1. Add a marker entry to `_MARKERS` in `classifier.py`.
2. Add a `<vendor>.py` module under `app/document_extractors/` exposing an
   `extract_*(pdf_path) -> list[Holding | Transaction]` function.
3. Register it in both `_EXTRACTORS` and `_WRITERS` in
   `app/document_extractors/__init__.py`.

## Project layout

```
app/
  main.py                  Flask routes
  redactor.py               Redaction engine (term/SSN/account-number matching)
  batch.py                  Background batch job orchestration
  version.py                Build/version identifier shown in the UI footer
  document_extractors/
    classifier.py            Picks a vendor extractor from first-page text
    jpmc.py                  JPMS brokerage holdings extraction
    chase.py                 Chase checking-statement transaction extraction
    base.py                  Shared Holding/Transaction types + markdown rendering
  templates/index.html      Upload UI
```
