from __future__ import annotations

from argparse import ArgumentParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Queue
from threading import Lock, Thread
from urllib.parse import unquote, urlparse
import cgi
import json
import shutil
import socket
import time
import uuid

from game_data_engine import run_pipeline
from game_data_engine.warehouse import fetch_run_snapshot


ROOT = Path(__file__).resolve().parent
UPLOAD_DIR = ROOT / "data" / "uploads"
RUNS_DIR = ROOT / "data" / "runs"
OUTPUT_DIR = ROOT / "output"
WAREHOUSE_DB = ROOT / "data" / "warehouse" / "game.duckdb"
DICTIONARY = ROOT / "examples" / "log_language.json"
LATEST_RUN = OUTPUT_DIR / "latest_run.json"
JOB_QUEUE: Queue[tuple[str, list[Path], dict[str, str]]] = Queue()
WORKER_LOCK = Lock()
JSON_LOCK = Lock()
WORKER_STARTED = False


def now_stamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def make_run_id() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    temp.replace(path)


def read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def status_path(run_id: str) -> Path:
    return RUNS_DIR / run_id / "status.json"


def valid_run_id(run_id: str) -> bool:
    return bool(run_id) and all(char.isalnum() or char in {"-", "_"} for char in run_id)


def build_storage(run_id: str) -> dict[str, str]:
    run_dir = RUNS_DIR / run_id
    processed_dir = run_dir / "processed"
    return {
        "run_id": run_id,
        "run_dir": str(run_dir.relative_to(ROOT)),
        "raw_dir": str((run_dir / "raw").relative_to(ROOT)),
        "processed_dir": str(processed_dir.relative_to(ROOT)),
        "analysis_json": str((processed_dir / "analysis.json").relative_to(ROOT)),
        "normalized_events": str((processed_dir / "normalized_events.csv").relative_to(ROOT)),
        "warehouse_db": str(WAREHOUSE_DB.relative_to(ROOT)),
        "latest_analysis_json": str((OUTPUT_DIR / "analysis.json").relative_to(ROOT)),
        "latest_normalized_events": str((OUTPUT_DIR / "normalized_events.csv").relative_to(ROOT)),
    }


def update_status(run_id: str, **updates: object) -> dict:
    with JSON_LOCK:
        path = status_path(run_id)
        payload = read_json(path) if path.exists() else {"run_id": run_id, "created_at": now_stamp()}
        payload.update(updates)
        payload["updated_at"] = now_stamp()
        write_json(path, payload)
        return payload


def write_latest_run(payload: dict) -> None:
    with JSON_LOCK:
        write_json(LATEST_RUN, payload)


def queue_position() -> int:
    return JOB_QUEUE.qsize() + 1


def ensure_worker() -> None:
    global WORKER_STARTED
    with WORKER_LOCK:
        if WORKER_STARTED:
            return
        Thread(target=worker_loop, daemon=True).start()
        WORKER_STARTED = True


def worker_loop() -> None:
    while True:
        run_id, saved, storage = JOB_QUEUE.get()
        try:
            run_analysis_job(run_id, saved, storage)
        finally:
            JOB_QUEUE.task_done()


def run_analysis_job(run_id: str, saved: list[Path], storage: dict[str, str]) -> None:
    processed_dir = RUNS_DIR / run_id / "processed"
    processed_analysis = processed_dir / "analysis.json"
    processed_normalized = processed_dir / "normalized_events.csv"
    latest_analysis = OUTPUT_DIR / "analysis.json"
    latest_normalized = OUTPUT_DIR / "normalized_events.csv"

    def report_progress(progress: float, stage: str, message: str) -> None:
        update_status(
            run_id,
            status="running",
            stage=stage,
            progress=round(progress, 3),
            message=message,
        )

    try:
        update_status(
            run_id,
            status="running",
            stage="starting",
            progress=0.06,
            message="분석 준비 중",
        )
        result = run_pipeline(
            inputs=saved,
            dictionary_path=DICTIONARY if DICTIONARY.exists() else None,
            normalized_out=processed_normalized,
            artifacts_dir=processed_dir,
            warehouse_path=WAREHOUSE_DB,
            run_id=run_id,
            progress_callback=report_progress,
        )
        result["uploaded_files"] = [path.name for path in saved]
        result["storage"] = storage

        write_json(processed_analysis, result)
        shutil.copyfile(processed_analysis, latest_analysis)
        if processed_normalized.exists():
            shutil.copyfile(processed_normalized, latest_normalized)

        status = update_status(
            run_id,
            status="done",
            stage="done",
            progress=1,
            message="적용 완료",
            summary=result.get("summary", {}),
            analysis_json=storage["analysis_json"],
        )
        write_latest_run(status)
    except Exception as exc:
        error_payload = {
            "error": str(exc),
            "run_id": run_id,
            "failed_at": now_stamp(),
        }
        write_json(processed_dir / "error.json", error_payload)
        status = update_status(
            run_id,
            status="failed",
            stage="failed",
            progress=1,
            message="실패",
            error=str(exc),
        )
        write_latest_run(status)


class AppHandler(SimpleHTTPRequestHandler):
    server_version = "GameDataApp/0.1"

    def translate_path(self, path: str) -> str:
        parsed = urlparse(path)
        clean = unquote(parsed.path)
        if clean == "/":
            clean = "/index.html"
        target = (ROOT / clean.lstrip("/")).resolve()
        if not str(target).startswith(str(ROOT)):
            return str(ROOT / "index.html")
        return str(target)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/run":
            self.send_error(404, "Not found")
            return
        try:
            payload = self._handle_run()
            self._send_json(payload, status=202)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=500)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if parsed.path == "/api/runs/latest":
            if not LATEST_RUN.exists():
                self._send_json({"status": "empty", "message": "실행 이력이 없습니다."}, status=404)
                return
            self._send_json(read_json(LATEST_RUN))
            return
        if len(parts) == 4 and parts[:2] == ["api", "runs"]:
            run_id = parts[2]
            resource = parts[3]
            if not valid_run_id(run_id):
                self._send_json({"error": "Invalid run id"}, status=400)
                return
            if resource == "status":
                path = status_path(run_id)
                if not path.exists():
                    self._send_json({"error": "Run not found", "run_id": run_id}, status=404)
                    return
                self._send_json(read_json(path))
                return
            if resource == "analysis":
                path = RUNS_DIR / run_id / "processed" / "analysis.json"
                if not path.exists():
                    self._send_json({"error": "Analysis not ready", "run_id": run_id}, status=404)
                    return
                self._send_json(read_json(path))
                return
            if resource == "warehouse":
                self._send_json(fetch_run_snapshot(WAREHOUSE_DB, run_id))
                return
        super().do_GET()

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_run(self) -> dict:
        content_type = self.headers.get("content-type", "")
        if not content_type.startswith("multipart/form-data"):
            raise ValueError("Only multipart/form-data uploads are supported.")

        run_id = make_run_id()
        run_dir = RUNS_DIR / run_id
        raw_dir = run_dir / "raw"
        processed_dir = run_dir / "processed"
        raw_dir.mkdir(parents=True, exist_ok=True)
        processed_dir.mkdir(parents=True, exist_ok=True)
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": content_type,
                "CONTENT_LENGTH": self.headers.get("content-length", "0"),
            },
        )

        fields = form["files"] if "files" in form else []
        if not isinstance(fields, list):
            fields = [fields]

        saved: list[Path] = []
        for field in fields:
            if not getattr(field, "filename", None):
                continue
            filename = Path(field.filename).name
            target = raw_dir / filename
            with target.open("wb") as handle:
                shutil.copyfileobj(field.file, handle)
            saved.append(target)
            shutil.copyfile(target, UPLOAD_DIR / f"{run_id}-{filename}")

        if not saved:
            raise ValueError("No files were uploaded.")

        storage = build_storage(run_id)
        status = {
            "run_id": run_id,
            "status": "queued",
            "stage": "queued",
            "progress": 0.05,
            "message": "접수됨",
            "queue_position": queue_position(),
            "created_at": now_stamp(),
            "updated_at": now_stamp(),
            "uploaded_files": [path.name for path in saved],
            "input_bytes": sum(path.stat().st_size for path in saved),
            "storage": storage,
        }
        write_json(status_path(run_id), status)
        write_latest_run(status)
        JOB_QUEUE.put((run_id, saved, storage))
        ensure_worker()
        return status


def local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            return probe.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())


def parse_args() -> tuple[str, int]:
    parser = ArgumentParser(description="Serve the Game Data dashboard")
    parser.add_argument("port", nargs="?", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--allow-lan", action="store_true", help="Bind to 0.0.0.0 for LAN testing.")
    args = parser.parse_args()
    host = "0.0.0.0" if args.allow_lan else args.host
    return host, args.port


def main() -> None:
    host, port = parse_args()
    server = ThreadingHTTPServer((host, port), AppHandler)
    display_host = "127.0.0.1" if host in {"", "0.0.0.0"} else host
    print(f"Serving Game Data app at http://{display_host}:{port}/index.html")
    if host == "0.0.0.0":
        print(f"LAN access enabled: http://{local_ip()}:{port}/index.html")
    server.serve_forever()


if __name__ == "__main__":
    main()
