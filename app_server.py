from __future__ import annotations

from argparse import ArgumentParser
from collections.abc import Mapping
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Queue
from threading import Lock, Thread
from urllib.parse import unquote, urlparse
import json
import os
import shutil
import socket
import time
import uuid

from game_data_engine import run_pipeline
from game_data_engine.warehouse import fetch_run_snapshot


ROOT = Path(__file__).resolve().parent


def resolve_data_dir(env: Mapping[str, str] = os.environ) -> tuple[Path, str]:
    railway_volume = env.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    if railway_volume:
        return Path(railway_volume).resolve(), "railway_volume"

    configured = env.get("APP_DATA_DIR", "").strip()
    if configured:
        return Path(configured).resolve(), "APP_DATA_DIR"

    default = env.get("APP_DEFAULT_DATA_DIR", "").strip()
    if default:
        return Path(default).resolve(), "APP_DEFAULT_DATA_DIR"

    return (ROOT / "data").resolve(), "default"


def resolve_output_dir(
    data_dir: Path,
    env: Mapping[str, str] = os.environ,
) -> tuple[Path, str]:
    configured = env.get("APP_OUTPUT_DIR", "").strip()
    if configured:
        return Path(configured).resolve(), "APP_OUTPUT_DIR"
    return (data_dir / "output").resolve(), "data_dir"


DATA_DIR, DATA_DIR_SOURCE = resolve_data_dir()
UPLOAD_DIR = DATA_DIR / "uploads"
RUNS_DIR = DATA_DIR / "runs"
OUTPUT_DIR, OUTPUT_DIR_SOURCE = resolve_output_dir(DATA_DIR)
WAREHOUSE_DB = DATA_DIR / "warehouse" / "game.duckdb"
DEFAULT_DICTIONARY = ROOT / "examples" / "log_language.json"
DICTIONARY = Path(os.environ.get("APP_DICTIONARY_PATH", DATA_DIR / "config" / "log_language.json")).resolve()
LATEST_RUN = OUTPUT_DIR / "latest_run.json"
JOB_QUEUE: Queue[tuple[str, list[Path], dict[str, str]]] = Queue()
WORKER_LOCK = Lock()
JSON_LOCK = Lock()
WORKER_STARTED = False
DEFAULT_ALLOWED_ORIGINS = {"https://vivaca86.github.io"}
ALLOWED_EVENT_TYPES = {
    "event",
    "session_start",
    "content_enter",
    "content_success",
    "content_fail",
    "match_issue",
    "product_view",
    "purchase",
    "reward_claim",
    "exit",
}


def allowed_origins() -> set[str]:
    configured = os.environ.get("APP_ALLOWED_ORIGINS", "")
    if configured:
        return {origin.strip().rstrip("/") for origin in configured.split(",") if origin.strip()}
    return DEFAULT_ALLOWED_ORIGINS


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


def parse_header_parameters(value: str) -> tuple[str, dict[str, str]]:
    parts = [part.strip() for part in str(value or "").split(";")]
    main = parts[0].lower() if parts else ""
    params: dict[str, str] = {}
    for part in parts[1:]:
        if "=" not in part:
            continue
        key, raw_value = part.split("=", 1)
        raw_value = raw_value.strip()
        if len(raw_value) >= 2 and raw_value[0] == raw_value[-1] == '"':
            raw_value = raw_value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        params[key.strip().lower()] = raw_value
    return main, params


def decode_header_filename(value: str | None) -> str:
    if not value:
        return ""
    if "''" in value:
        charset, encoded = value.split("''", 1)
        try:
            return unquote(encoded, encoding=charset or "utf-8")
        except LookupError:
            return unquote(encoded)
    return value


def strip_part_line_break(value: bytes) -> bytes:
    if value.endswith(b"\r\n"):
        return value[:-2]
    if value.endswith(b"\n"):
        return value[:-1]
    return value


def status_path(run_id: str) -> Path:
    return RUNS_DIR / run_id / "status.json"


def valid_run_id(run_id: str) -> bool:
    return bool(run_id) and all(char.isalnum() or char in {"-", "_"} for char in run_id)


def display_path(path: Path) -> str:
    resolved = path.resolve()
    for root in (ROOT, DATA_DIR):
        try:
            return str(resolved.relative_to(root))
        except ValueError:
            pass
    return str(resolved)


def is_railway_runtime(env: Mapping[str, str] = os.environ) -> bool:
    return any(env.get(name) for name in ("RAILWAY_ENVIRONMENT", "RAILWAY_SERVICE_ID", "RAILWAY_PROJECT_ID"))


def storage_health(env: Mapping[str, str] = os.environ) -> dict[str, object]:
    railway_volume_mounted = bool(env.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip())
    railway_runtime = is_railway_runtime(env)
    if railway_volume_mounted:
        persistence = "railway_volume"
    elif railway_runtime:
        persistence = "railway_ephemeral"
    else:
        persistence = "local_disk"

    return {
        "data_dir": display_path(DATA_DIR),
        "data_dir_source": DATA_DIR_SOURCE,
        "output_dir": display_path(OUTPUT_DIR),
        "output_dir_source": OUTPUT_DIR_SOURCE,
        "warehouse_db": display_path(WAREHOUSE_DB),
        "dictionary": display_path(DICTIONARY),
        "railway_runtime": railway_runtime,
        "railway_volume_mounted": railway_volume_mounted,
        "persistence": persistence,
    }


def build_storage(run_id: str) -> dict[str, str]:
    run_dir = RUNS_DIR / run_id
    processed_dir = run_dir / "processed"
    return {
        "run_id": run_id,
        "run_dir": display_path(run_dir),
        "raw_dir": display_path(run_dir / "raw"),
        "processed_dir": display_path(processed_dir),
        "analysis_json": display_path(processed_dir / "analysis.json"),
        "normalized_events": display_path(processed_dir / "normalized_events.csv"),
        "warehouse_db": display_path(WAREHOUSE_DB),
        "dictionary": display_path(DICTIONARY),
        "latest_analysis_json": display_path(OUTPUT_DIR / "analysis.json"),
        "latest_normalized_events": display_path(OUTPUT_DIR / "normalized_events.csv"),
    }


def ensure_dictionary() -> Path:
    if DICTIONARY.exists():
        return DICTIONARY
    DICTIONARY.parent.mkdir(parents=True, exist_ok=True)
    if DEFAULT_DICTIONARY.exists():
        shutil.copyfile(DEFAULT_DICTIONARY, DICTIONARY)
    else:
        write_json(
            DICTIONARY,
            {
                "timezone": "Asia/Seoul",
                "session_gap_minutes": 30,
                "fields": {},
                "event_labels": {},
                "content_labels": {},
                "product_labels": {},
            },
        )
    return DICTIONARY


def clean_mapping_text(value: object, max_length: int) -> str:
    text = str(value or "").strip()
    return text[:max_length]


def normalize_language_mapping(mapping: Mapping[str, object]) -> tuple[str, dict[str, object]] | None:
    raw = clean_mapping_text(mapping.get("raw") or mapping.get("code"), 160)
    if not raw:
        return None
    label = clean_mapping_text(mapping.get("label") or mapping.get("suggested_label") or raw, 160) or raw
    event_type = clean_mapping_text(mapping.get("event_type") or "event", 60)
    if event_type not in ALLOWED_EVENT_TYPES:
        event_type = "event"
    group = clean_mapping_text(mapping.get("group"), 120)
    return raw, {
        "label": label,
        "event_type": event_type,
        "group": group or None,
        "confidence": 1.0,
    }


def update_language_dictionary(
    mappings: list[Mapping[str, object]],
    dictionary_path: Path | None = None,
) -> dict[str, object]:
    target = dictionary_path or ensure_dictionary()
    target.parent.mkdir(parents=True, exist_ok=True)
    with JSON_LOCK:
        data = read_json(target) if target.exists() else {}
        data.setdefault("timezone", "Asia/Seoul")
        data.setdefault("session_gap_minutes", 30)
        data.setdefault("fields", {})
        event_labels = data.setdefault("event_labels", {})
        changed: list[str] = []
        for mapping in mappings:
            normalized = normalize_language_mapping(mapping)
            if not normalized:
                continue
            raw, entry = normalized
            event_labels[raw] = entry
            changed.append(raw)
        write_json(target, data)
    return {
        "status": "ok",
        "updated": len(changed),
        "codes": changed,
        "dictionary": display_path(target),
        "event_labels": data.get("event_labels", {}),
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
        dictionary_path = ensure_dictionary()
        result = run_pipeline(
            inputs=saved,
            dictionary_path=dictionary_path,
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
        if clean.startswith("/output/"):
            target = (OUTPUT_DIR / clean.removeprefix("/output/")).resolve()
            if target == OUTPUT_DIR or target.is_relative_to(OUTPUT_DIR):
                return str(target)
            return str(ROOT / "index.html")
        target = (ROOT / clean.lstrip("/")).resolve()
        if not (target == ROOT or target.is_relative_to(ROOT)):
            return str(ROOT / "index.html")
        return str(target)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        origin = self.headers.get("Origin")
        origins = allowed_origins()
        if origin and ("*" in origins or origin.rstrip("/") in origins):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/") or parsed.path.startswith("/output/"):
            self.send_response(204)
            self.end_headers()
            return
        self.send_error(404, "Not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/run":
                payload = self._handle_run()
                self._send_json(payload, status=202)
                return
            if parsed.path == "/api/language":
                payload = self._handle_language_save()
                self._send_json(payload)
                return
            self.send_error(404, "Not found")
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=500)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if parsed.path == "/api/health":
            storage = storage_health()
            self._send_json(
                {
                    "status": "ok",
                    "data_dir": storage["data_dir"],
                    "warehouse_db": storage["warehouse_db"],
                    "storage": storage,
                }
            )
            return
        if parsed.path == "/api/language":
            path = ensure_dictionary()
            payload = read_json(path)
            payload["dictionary"] = display_path(path)
            payload["event_type_options"] = sorted(ALLOWED_EVENT_TYPES)
            self._send_json(payload)
            return
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

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("content-length", "0") or 0)
        if length <= 0:
            return {}
        if length > 1_000_000:
            raise ValueError("JSON body is too large.")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_language_save(self) -> dict:
        payload = self._read_json_body()
        mappings = payload.get("mappings")
        if isinstance(mappings, dict):
            mappings = [{"raw": raw, **entry} for raw, entry in mappings.items() if isinstance(entry, dict)]
        if not isinstance(mappings, list):
            raise ValueError("mappings must be a list.")
        return update_language_dictionary([item for item in mappings if isinstance(item, dict)])

    def _read_part_until_boundary(self, boundary: bytes, handle: object | None = None) -> bytes:
        previous: bytes | None = None
        while True:
            line = self.rfile.readline()
            if line == b"":
                raise ValueError("Unexpected end of multipart upload.")
            stripped = line.rstrip(b"\r\n")
            if stripped == boundary or stripped == boundary + b"--":
                if previous is not None and handle is not None:
                    handle.write(strip_part_line_break(previous))
                return stripped
            if previous is not None and handle is not None:
                handle.write(previous)
            previous = line

    def _read_multipart_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        while True:
            line = self.rfile.readline()
            if line == b"":
                raise ValueError("Unexpected end of multipart headers.")
            if line in {b"\r\n", b"\n"}:
                return headers
            decoded = line.decode("utf-8", errors="replace").rstrip("\r\n")
            if ":" not in decoded:
                continue
            name, value = decoded.split(":", 1)
            headers[name.strip().lower()] = value.strip()

    def _save_multipart_files(self, content_type: str, raw_dir: Path, run_id: str) -> list[Path]:
        media_type, params = parse_header_parameters(content_type)
        boundary_value = params.get("boundary", "")
        if media_type != "multipart/form-data" or not boundary_value:
            raise ValueError("Only multipart/form-data uploads are supported.")

        boundary = f"--{boundary_value}".encode("utf-8")
        first_line = self.rfile.readline().rstrip(b"\r\n")
        if first_line != boundary:
            raise ValueError("Malformed multipart upload.")

        saved: list[Path] = []
        done = False
        while not done:
            headers = self._read_multipart_headers()
            _, disposition_params = parse_header_parameters(headers.get("content-disposition", ""))
            field_name = disposition_params.get("name")
            filename = decode_header_filename(
                disposition_params.get("filename*") or disposition_params.get("filename")
            )
            if field_name == "files" and filename:
                safe_name = Path(filename).name
                target = raw_dir / safe_name
                with target.open("wb") as handle:
                    boundary_line = self._read_part_until_boundary(boundary, handle)
                saved.append(target)
                shutil.copyfile(target, UPLOAD_DIR / f"{run_id}-{safe_name}")
            else:
                boundary_line = self._read_part_until_boundary(boundary)
            done = boundary_line == boundary + b"--"

        return saved

    def _handle_run(self) -> dict:
        content_type = self.headers.get("content-type", "")

        run_id = make_run_id()
        run_dir = RUNS_DIR / run_id
        raw_dir = run_dir / "raw"
        processed_dir = run_dir / "processed"
        raw_dir.mkdir(parents=True, exist_ok=True)
        processed_dir.mkdir(parents=True, exist_ok=True)
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        saved = self._save_multipart_files(content_type, raw_dir, run_id)

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
    parser.add_argument("port", nargs="?", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
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
