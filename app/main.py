"""
Redactomatic – Flask web application.

Environment variables (all optional):
    UPLOAD_FOLDER   – where uploaded and output files are stored (default: /data)
    MAX_CONTENT_MB  – max upload size in MB (default: 100)
    SECRET_KEY      – Flask secret key for flash messages
"""

import os
import pathlib
import uuid

from flask import (
    Flask,
    jsonify,
    render_template,
    request,
    send_file,
)
from werkzeug.utils import secure_filename

import batch
from redactor import redact_pdf
from version import VERSION

# ---------------------------------------------------------------------------
# App configuration
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24))

UPLOAD_FOLDER = pathlib.Path(os.environ.get("UPLOAD_FOLDER", "/data"))
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)

MAX_MB = int(os.environ.get("MAX_CONTENT_MB", 100))
app.config["MAX_CONTENT_LENGTH"] = MAX_MB * 1024 * 1024


def _ext(filename: str) -> str:
    return pathlib.Path(filename).suffix.lstrip(".").lower()


def _session_dir() -> pathlib.Path:
    """Create a unique sub-directory for each upload session."""
    d = UPLOAD_FOLDER / uuid.uuid4().hex
    d.mkdir(parents=True, exist_ok=True)
    return d


def _resolve_session_dir(job_id: str) -> pathlib.Path | None:
    """Resolve *job_id* to an existing session directory under UPLOAD_FOLDER."""
    safe = (UPLOAD_FOLDER / job_id).resolve()
    if not str(safe).startswith(str(UPLOAD_FOLDER.resolve())):
        return None
    if not safe.is_dir():
        return None
    return safe


def _unique_path(session_dir: pathlib.Path, filename: str) -> pathlib.Path:
    """Return a save path in *session_dir* that doesn't collide with an existing file."""
    candidate = session_dir / filename
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    n = 2
    while (session_dir / f"{stem}_{n}{suffix}").exists():
        n += 1
    return session_dir / f"{stem}_{n}{suffix}"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", version=VERSION)


@app.route("/redact", methods=["POST"])
def redact():
    """Accept a PDF and a control file, redact, and return download info."""
    if "pdf_file" not in request.files or "control_file" not in request.files:
        return jsonify({"error": "Both a PDF file and a control file are required."}), 400

    pdf_file = request.files["pdf_file"]
    ctrl_file = request.files["control_file"]

    if not pdf_file.filename or not ctrl_file.filename:
        return jsonify({"error": "No file selected."}), 400

    if _ext(pdf_file.filename) != "pdf":
        return jsonify({"error": "Uploaded document must be a PDF."}), 400

    session = _session_dir()

    pdf_name = secure_filename(pdf_file.filename)
    ctrl_name = secure_filename(ctrl_file.filename) or "control.txt"

    pdf_path = session / pdf_name
    ctrl_path = session / ctrl_name

    pdf_file.save(str(pdf_path))
    ctrl_file.save(str(ctrl_path))

    try:
        out_path, count, tables_path = redact_pdf(str(pdf_path), str(ctrl_path))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 422
    except Exception as exc:  # noqa: BLE001
        app.logger.exception("Redaction failed")
        return jsonify({"error": f"Redaction failed: {exc}"}), 500

    out_rel = str(pathlib.Path(out_path).relative_to(UPLOAD_FOLDER))

    response = {
        "success": True,
        "redactions": count,
        "output_file": pathlib.Path(out_path).name,
        "download_token": out_rel,
    }
    if tables_path is not None:
        response["tables_file"] = pathlib.Path(tables_path).name
        response["tables_download_token"] = str(pathlib.Path(tables_path).relative_to(UPLOAD_FOLDER))

    return jsonify(response)


@app.route("/batch", methods=["POST"])
def create_batch():
    """Accept multiple PDFs and a control file, and start a background batch job."""
    files = request.files.getlist("files")
    ctrl_file = request.files.get("control_file")

    files = [f for f in files if f.filename]
    if not files:
        return jsonify({"error": "At least one PDF file is required."}), 400
    if not ctrl_file or not ctrl_file.filename:
        return jsonify({"error": "A control file is required."}), 400

    bad = [f.filename for f in files if _ext(f.filename) != "pdf"]
    if bad:
        return jsonify({"error": f"Unsupported file type(s): {', '.join(bad)}. Only PDF is supported."}), 400

    build_tables = request.form.get("build_tables", "true").lower() != "false"

    session = _session_dir()

    ctrl_name = secure_filename(ctrl_file.filename) or "control.txt"
    ctrl_path = session / ctrl_name
    ctrl_file.save(str(ctrl_path))

    saved_files: list[tuple[str, str]] = []
    for f in files:
        save_path = _unique_path(session, secure_filename(f.filename) or "file.pdf")
        f.save(str(save_path))
        saved_files.append((str(save_path), f.filename))

    batch.start_batch(session, saved_files, str(ctrl_path), build_tables)

    return jsonify({"job_id": session.name, "total": len(saved_files)})


@app.route("/batch/<job_id>/status")
def batch_status(job_id: str):
    session = _resolve_session_dir(job_id)
    if session is None:
        return jsonify({"error": "Unknown job."}), 404

    status = batch.read_status(session)
    if status is None:
        return jsonify({"error": "Unknown job."}), 404

    if status.get("state") == "complete" and status.get("zip_filename"):
        status["zip_download_token"] = f"{job_id}/{status['zip_filename']}"

    return jsonify(status)


@app.route("/batch/<job_id>/cancel", methods=["POST"])
def batch_cancel(job_id: str):
    session = _resolve_session_dir(job_id)
    if session is None:
        return jsonify({"error": "Unknown job."}), 404

    ok = batch.request_cancel(session)
    return jsonify({"ok": ok})


@app.route("/download/<path:token>")
def download(token: str):
    """Stream the redacted PDF back to the browser."""
    safe = (UPLOAD_FOLDER / token).resolve()
    if not str(safe).startswith(str(UPLOAD_FOLDER.resolve())):
        return "Forbidden", 403
    if not safe.exists():
        return "Not found", 404

    if safe.suffix == ".md":
        mimetype = "text/markdown"
    elif safe.suffix == ".zip":
        mimetype = "application/zip"
    else:
        mimetype = "application/pdf"

    return send_file(
        safe,
        as_attachment=True,
        download_name=safe.name,
        mimetype=mimetype,
    )


# ---------------------------------------------------------------------------
# Entry point (dev only – gunicorn is used in the container)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
