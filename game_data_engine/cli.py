from __future__ import annotations

import argparse
import json
from pathlib import Path

from .benchmark import print_benchmark, run_benchmark
from .pipeline import run_pipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Game raw data processing engine")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Process raw data and produce analysis JSON")
    run.add_argument("--input", action="append", required=True, help="Input file or directory. Repeatable.")
    run.add_argument("--dictionary", help="Log language dictionary JSON")
    run.add_argument("--out", required=True, help="Output analysis JSON path")
    run.add_argument("--normalized-out", help="Optional normalized event CSV path")
    run.add_argument("--artifacts-dir", help="Optional directory for split dashboard artifacts")
    run.add_argument("--warehouse", help="Optional DuckDB warehouse path")
    run.add_argument("--run-id", help="Optional run id for warehouse writes")
    run.add_argument("--sample-limit", type=int, default=20, help="Max journey/session samples in JSON output")

    benchmark = subparsers.add_parser("benchmark", help="Generate synthetic events and measure a pipeline run")
    benchmark.add_argument("--rows", type=int, default=10_000, help="Synthetic row count")
    benchmark.add_argument("--users", type=int, help="Synthetic UID count")
    benchmark.add_argument("--out-dir", default="output/benchmark", help="Directory for generated files and reports")
    benchmark.add_argument("--dictionary", default="examples/log_language.json", help="Log language dictionary JSON")
    benchmark.add_argument("--warehouse", help="Optional DuckDB warehouse path")
    benchmark.add_argument("--sample-limit", type=int, default=5, help="Max journey/session samples in JSON output")
    benchmark.add_argument("--discard-input", action="store_true", help="Delete generated CSV after the run")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.command == "run":
        payload = run_pipeline(
            inputs=[Path(value) for value in args.input],
            dictionary_path=args.dictionary,
            out=args.out,
            normalized_out=args.normalized_out,
            artifacts_dir=args.artifacts_dir,
            warehouse_path=args.warehouse,
            run_id=args.run_id,
            sample_limit=args.sample_limit,
        )
        print(
            json.dumps(
                {
                    "out": args.out,
                    "active_users": payload["summary"]["active_users"],
                    "revenue": payload["summary"]["revenue"],
                    "alerts": payload["alerts"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.command == "benchmark":
        result = run_benchmark(
            rows=args.rows,
            users=args.users,
            output_dir=args.out_dir,
            dictionary_path=args.dictionary,
            warehouse_path=args.warehouse,
            sample_limit=args.sample_limit,
            keep_input=not args.discard_input,
        )
        print_benchmark(result)


if __name__ == "__main__":
    main()
