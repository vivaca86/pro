from __future__ import annotations

from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse
import cgi
import json
import shutil
import sys
import time

from game_data_engine import run_pipeline


ROOT = Path(__file__).resolve().parent
UPLOAD_DIR = ROOT / "data" / "uploads"
RUNS_DIR = ROOT / "data" / "runs"
OUTPUT_DIR = ROOT / "output"
DICTIONARY = ROOT / "examples" / "log_language.json"


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
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            body = json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def _handle_run(self) -> dict:
        content_type = self.headers.get("content-type", "")
        if not content_type.startswith("multipart/form-data"):
            raise ValueError("Only multipart/form-data uploads are supported.")

        run_id = time.strftime("%Y%m%d-%H%M%S")
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

        processed_analysis = processed_dir / "analysis.json"
        processed_normalized = processed_dir / "normalized_events.csv"
        latest_analysis = OUTPUT_DIR / "analysis.json"
        latest_normalized = OUTPUT_DIR / "normalized_events.csv"

        result = run_pipeline(
            inputs=saved,
            dictionary_path=DICTIONARY if DICTIONARY.exists() else None,
            out=processed_analysis,
            normalized_out=processed_normalized,
        )
        result["uploaded_files"] = [path.name for path in saved]
        result["storage"] = {
            "run_id": run_id,
            "run_dir": str(run_dir.relative_to(ROOT)),
            "raw_dir": str(raw_dir.relative_to(ROOT)),
            "processed_dir": str(processed_dir.relative_to(ROOT)),
            "analysis_json": str(processed_analysis.relative_to(ROOT)),
            "normalized_events": str(processed_normalized.relative_to(ROOT)),
            "latest_analysis_json": str(latest_analysis.relative_to(ROOT)),
            "latest_normalized_events": str(latest_normalized.relative_to(ROOT)),
        }

        with processed_analysis.open("w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
        shutil.copyfile(processed_analysis, latest_analysis)
        shutil.copyfile(processed_normalized, latest_normalized)
        return result


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    server = ThreadingHTTPServer(("127.0.0.1", port), AppHandler)
    print(f"Serving Game Data app at http://127.0.0.1:{port}/index.html")
    server.serve_forever()


if __name__ == "__main__":
    main()
