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

from redactor import redact_pdf

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


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


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
        out_path, count = redact_pdf(str(pdf_path), str(ctrl_path))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 422
    except Exception as exc:  # noqa: BLE001
        app.logger.exception("Redaction failed")
        return jsonify({"error": f"Redaction failed: {exc}"}), 500

    out_rel = str(pathlib.Path(out_path).relative_to(UPLOAD_FOLDER))

    return jsonify(
        {
            "success": True,
            "redactions": count,
            "output_file": pathlib.Path(out_path).name,
            "download_token": out_rel,
        }
    )


@app.route("/download/<path:token>")
def download(token: str):
    """Stream the redacted PDF back to the browser."""
    safe = (UPLOAD_FOLDER / token).resolve()
    if not str(safe).startswith(str(UPLOAD_FOLDER.resolve())):
        return "Forbidden", 403
    if not safe.exists():
        return "Not found", 404

    return send_file(
        safe,
        as_attachment=True,
        download_name=safe.name,
        mimetype="application/pdf",
    )


# ---------------------------------------------------------------------------
# Entry point (dev only – gunicorn is used in the container)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
