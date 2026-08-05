"""
Batch redaction job orchestration.

Runs a batch of PDF redactions on a background thread and persists progress
to a ``status.json`` file inside the job's session directory rather than
holding it in memory. The app can run under multiple gunicorn worker
processes with no shared memory, so a client polling for status may land on
a different worker than the one running the job -- disk is the only thing
both can see.
"""

import json
import pathlib
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone

from redactor import redact_pdf

STATUS_FILENAME = "status.json"
CANCEL_FLAG_FILENAME = "cancel.flag"

# status.json has exactly one writer: the background batch thread. Cancellation
# is a separate, single-purpose "cancel.flag" file that the HTTP cancel route
# creates -- a plain file-create is inherently race-free, unlike a
# read-modify-write against the shared status document from a second writer.


def _status_path(session_dir: pathlib.Path) -> pathlib.Path:
    return session_dir / STATUS_FILENAME


def _write_status(session_dir: pathlib.Path, data: dict) -> None:
    """Write status.json atomically so pollers never see a partial write."""
    path = _status_path(session_dir)
    tmp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)


def read_status(session_dir: pathlib.Path) -> dict | None:
    path = _status_path(session_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _cancel_flag_path(session_dir: pathlib.Path) -> pathlib.Path:
    return session_dir / CANCEL_FLAG_FILENAME


def request_cancel(session_dir: pathlib.Path) -> bool:
    """Flag a running job for cancellation. Returns False if not running."""
    status = read_status(session_dir)
    if status is None or status.get("state") != "running":
        return False
    _cancel_flag_path(session_dir).touch()
    return True


def start_batch(
    session_dir: pathlib.Path,
    saved_files: list[tuple[str, str]],
    control_path: str,
    build_tables: bool,
) -> None:
    """Kick off a batch job in a background thread.

    *saved_files* is a list of (saved_path, original_filename) tuples for
    files already written to *session_dir*.
    """
    total = len(saved_files)
    _write_status(session_dir, {
        "state": "running",
        "total": total,
        "completed": 0,
        "current_index": 0,
        "current_file": None,
        "results": [],
        "started_at": datetime.now(timezone.utc).isoformat(),
        "cancel_requested": False,
        "eta_seconds": None,
        "zip_filename": None,
        "error": None,
    })

    thread = threading.Thread(
        target=_run_batch,
        args=(session_dir, saved_files, control_path, build_tables),
        daemon=True,
    )
    thread.start()


def _run_batch(
    session_dir: pathlib.Path,
    saved_files: list[tuple[str, str]],
    control_path: str,
    build_tables: bool,
) -> None:
    total = len(saved_files)
    results: list[dict] = []
    start_time = time.monotonic()

    try:
        for index, (saved_path, original_filename) in enumerate(saved_files, start=1):
            if _cancel_flag_path(session_dir).exists():
                status = read_status(session_dir) or {}
                status["state"] = "cancelled"
                status["cancel_requested"] = True
                status["results"] = results
                status["current_file"] = None
                _write_status(session_dir, status)
                _cleanup_session_dir(session_dir, keep={STATUS_FILENAME})
                return

            status = read_status(session_dir) or {}
            status["current_index"] = index
            status["current_file"] = original_filename
            status["results"] = results
            _write_status(session_dir, status)

            entry = {
                "original_filename": original_filename,
                "redacted_filename": None,
                "tables_filename": None,
                "holdings_json_filename": None,
                "redactions": 0,
                "status": "error",
                "error": None,
            }
            try:
                out_path, count, tables_path, holdings_json_path = redact_pdf(saved_path, control_path, build_tables)
                entry["redacted_filename"] = pathlib.Path(out_path).name
                entry["tables_filename"] = pathlib.Path(tables_path).name if tables_path else None
                entry["holdings_json_filename"] = pathlib.Path(holdings_json_path).name if holdings_json_path else None
                entry["redactions"] = count
                entry["status"] = "success"
            except Exception as exc:  # noqa: BLE001 - one bad file must not abort the batch
                entry["error"] = str(exc)

            results.append(entry)

            elapsed = time.monotonic() - start_time
            avg_per_file = elapsed / index
            remaining = total - index
            eta_seconds = round(avg_per_file * remaining, 1) if remaining > 0 else 0

            status = read_status(session_dir) or {}
            status["completed"] = index
            status["results"] = results
            status["eta_seconds"] = eta_seconds
            _write_status(session_dir, status)

        zip_path = _build_zip(session_dir, session_dir.name, results, build_tables)
        _cleanup_session_dir(session_dir, keep={STATUS_FILENAME, zip_path.name})

        status = read_status(session_dir) or {}
        status["state"] = "complete"
        status["zip_filename"] = zip_path.name
        status["current_file"] = None
        status["eta_seconds"] = 0
        _write_status(session_dir, status)

    except Exception as exc:  # noqa: BLE001 - never leave the client polling forever
        status = read_status(session_dir) or {"total": total, "results": results}
        status["state"] = "error"
        status["error"] = str(exc)
        _write_status(session_dir, status)


def _build_zip(
    session_dir: pathlib.Path,
    job_id: str,
    results: list[dict],
    build_tables: bool,
) -> pathlib.Path:
    manifest = {
        "job_id": job_id,
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "build_tables": build_tables,
        "files": [
            {
                "original_filename": r["original_filename"],
                "redacted_filename": r["redacted_filename"],
                "tables_filename": r["tables_filename"],
                "holdings_json_filename": r["holdings_json_filename"],
                "redactions": r["redactions"],
                "status": r["status"],
                "error": r["error"],
            }
            for r in results
        ],
    }

    zip_path = session_dir / f"redacted_batch_{job_id}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        for r in results:
            if r["status"] != "success":
                continue
            redacted_path = session_dir / r["redacted_filename"]
            if redacted_path.exists():
                zf.write(redacted_path, arcname=r["redacted_filename"])
            if r["tables_filename"]:
                tables_path = session_dir / r["tables_filename"]
                if tables_path.exists():
                    zf.write(tables_path, arcname=r["tables_filename"])
            if r["holdings_json_filename"]:
                holdings_json_path = session_dir / r["holdings_json_filename"]
                if holdings_json_path.exists():
                    zf.write(holdings_json_path, arcname=r["holdings_json_filename"])

    return zip_path


def _cleanup_session_dir(session_dir: pathlib.Path, keep: set[str]) -> None:
    """Remove every working file in *session_dir* except the ones in *keep*."""
    for path in session_dir.iterdir():
        if path.name in keep or path.name.endswith(".tmp"):
            continue
        if path.is_file():
            path.unlink(missing_ok=True)
