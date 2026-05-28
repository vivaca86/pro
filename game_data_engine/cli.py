from __future__ import annotations

import argparse
import json
from pathlib import Path

from .pipeline import run_pipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Game raw data processing engine")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Process raw data and produce analysis JSON")
    run.add_argument("--input", action="append", required=True, help="Input file or directory. Repeatable.")
    run.add_argument("--dictionary", help="Log language dictionary JSON")
    run.add_argument("--out", required=True, help="Output analysis JSON path")
    run.add_argument("--normalized-out", help="Optional normalized event CSV path")
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


if __name__ == "__main__":
    main()
