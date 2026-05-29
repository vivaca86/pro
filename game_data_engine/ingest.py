from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
import json

import pandas as pd


SUPPORTED_SUFFIXES = {".csv", ".tsv", ".txt", ".json", ".jsonl", ".xlsx", ".xls"}


@dataclass(frozen=True)
class RawTableInfo:
    source_file: str
    row_count: int
    columns: list[str]
    source_sheet: str | None = None


RawTable = pd.DataFrame | RawTableInfo


def describe_raw_table(table: RawTable) -> RawTableInfo:
    if isinstance(table, RawTableInfo):
        return table
    return RawTableInfo(
        source_file=str(table["_source_file"].iloc[0]) if "_source_file" in table and not table.empty else "unknown",
        source_sheet=str(table["_source_sheet"].iloc[0]) if "_source_sheet" in table and not table.empty else None,
        row_count=int(len(table)),
        columns=[str(column) for column in table.columns if not str(column).startswith("_source")],
    )


def discover_files(inputs: Iterable[str | Path]) -> list[Path]:
    files: list[Path] = []
    for raw in inputs:
        path = Path(raw)
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.is_file() and child.suffix.lower() in SUPPORTED_SUFFIXES:
                    files.append(child)
        elif path.is_file():
            if path.suffix.lower() not in SUPPORTED_SUFFIXES:
                raise ValueError(f"Unsupported file type: {path}")
            files.append(path)
        else:
            raise FileNotFoundError(path)
    if not files:
        raise ValueError("No supported data files found.")
    return files


def read_tables(path: Path) -> list[pd.DataFrame]:
    suffix = path.suffix.lower()
    if suffix in {".csv", ".tsv", ".txt"}:
        sep = "\t" if suffix == ".tsv" else None
        frame = pd.read_csv(path, sep=sep, engine="python")
        frame["_source_file"] = path.name
        return [frame]
    if suffix == ".jsonl":
        frame = pd.read_json(path, lines=True)
        frame["_source_file"] = path.name
        return [frame]
    if suffix == ".json":
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        frame = pd.json_normalize(data)
        frame["_source_file"] = path.name
        return [frame]
    if suffix in {".xlsx", ".xls"}:
        sheets = pd.read_excel(path, sheet_name=None)
        frames: list[pd.DataFrame] = []
        for sheet_name, frame in sheets.items():
            frame["_source_file"] = path.name
            frame["_source_sheet"] = sheet_name
            frames.append(frame)
        return frames
    raise ValueError(f"Unsupported file type: {path}")


def ingest(inputs: Iterable[str | Path]) -> list[pd.DataFrame]:
    frames: list[pd.DataFrame] = []
    for path in discover_files(inputs):
        frames.extend(read_tables(path))
    return frames
