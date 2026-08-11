"""File sniffing, column-name normalisation, and type inference.

The job here is to turn an arbitrary spreadsheet into something a machine can
reason about: identifier-safe column names, a small closed set of logical
types, and a few real sample values.

Type inference is delegated to Polars rather than hand-rolled. Its CSV reader
already handles quoting, embedded newlines, encodings, and mixed-type columns —
all the things a naive `split(",")` gets wrong on real customer data.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl

from decisionflow.core.errors import ValidationError
from decisionflow.db.models.ingestion import ColumnType

# How many rows Polars reads to decide a column's type. Too low and a column
# that is integer for 500 rows then "N/A" is mistyped; too high and a large
# upload spends noticeable time on inference alone.
TYPE_INFERENCE_ROWS = 10_000
SAMPLE_VALUE_COUNT = 5

CSV_SUFFIXES = {".csv", ".tsv", ".txt"}
EXCEL_SUFFIXES = {".xlsx", ".xls", ".xlsm"}
SUPPORTED_SUFFIXES = CSV_SUFFIXES | EXCEL_SUFFIXES

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_LEADING_DIGIT = re.compile(r"^[0-9]")

# Reserved in DuckDB/SQL; a column named `select` would produce DDL that fails
# to parse. Suffixed rather than rejected, since renaming is invisible to the
# user and rejecting their file is not.
_RESERVED = frozenset(
    {
        "select", "from", "where", "group", "order", "by", "join", "table",
        "column", "index", "primary", "key", "all", "and", "or", "not", "null",
        "true", "false", "case", "when", "then", "else", "end", "as", "on",
        "in", "is", "like", "limit", "offset", "union", "create", "drop",
        "insert", "update", "delete", "values", "into", "distinct", "having",
        "default", "with", "using", "cross", "inner", "outer", "left", "right",
    }
)


@dataclass(slots=True)
class DetectedColumn:
    position: int
    name: str
    normalized_name: str
    data_type: ColumnType
    source_type: str
    nullable: bool
    sample_values: list[Any] = field(default_factory=list)


@dataclass(slots=True)
class DetectedSchema:
    columns: list[DetectedColumn]
    row_count: int


def normalize_column_name(raw: str, *, position: int, taken: set[str]) -> str:
    """Turn an arbitrary header into a unique, identifier-safe name.

    "Total Revenue (USD)" becomes `total_revenue_usd`. The original is kept on
    the model — it carries meaning for humans and for the LLM that the
    normalised form loses.
    """
    ascii_only = (
        unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii")
    )
    candidate = _NON_ALNUM.sub("_", ascii_only.lower()).strip("_")

    if not candidate:
        candidate = f"column_{position + 1}"
    if _LEADING_DIGIT.match(candidate):
        candidate = f"col_{candidate}"
    if candidate in _RESERVED:
        candidate = f"{candidate}_col"

    candidate = candidate[:120]

    # Spreadsheets very often carry duplicate or blank headers.
    unique = candidate
    suffix = 2
    while unique in taken:
        unique = f"{candidate}_{suffix}"
        suffix += 1

    taken.add(unique)
    return unique


def _map_dtype(dtype: pl.DataType) -> ColumnType:
    """Collapse a Polars dtype into our small logical vocabulary."""
    if dtype in (pl.Boolean,):
        return ColumnType.BOOLEAN
    if dtype in (pl.Int8, pl.Int16, pl.Int32, pl.Int64, pl.UInt8, pl.UInt16, pl.UInt32, pl.UInt64):
        return ColumnType.INTEGER
    if dtype in (pl.Float32, pl.Float64, pl.Decimal):
        return ColumnType.DECIMAL
    if dtype == pl.Date:
        return ColumnType.DATE
    if dtype in (pl.Datetime, pl.Time):
        return ColumnType.TIMESTAMP
    if dtype in (pl.Utf8, pl.String, pl.Categorical):
        return ColumnType.STRING
    return ColumnType.UNKNOWN


def read_sample(path: Path, *, rows: int = TYPE_INFERENCE_ROWS) -> pl.DataFrame:
    """Read the head of a file with types inferred.

    Raises ValidationError rather than letting a parser error escape, so a
    malformed upload is reported as the user's problem, not a 500.
    """
    suffix = path.suffix.lower()

    try:
        if suffix in EXCEL_SUFFIXES:
            # Excel carries its own types, so no inference length applies.
            return pl.read_excel(path).head(rows)

        separator = "\t" if suffix == ".tsv" else ","
        return pl.read_csv(
            path,
            separator=separator,
            n_rows=rows,
            infer_schema_length=rows,
            try_parse_dates=True,
            ignore_errors=True,  # a single bad row must not sink the upload
            truncate_ragged_lines=True,
        )
    except Exception as exc:
        raise ValidationError(f"Could not read this file: {exc}") from exc


def read_full(path: Path) -> pl.DataFrame:
    """Read an entire file.

    Used by the worker, which needs every row rather than a type-inference
    sample. Bounded by the upload size limit, and Polars holds it in a
    columnar Arrow buffer that DuckDB can then consume without a copy — so
    "read it all" costs far less here than the phrase suggests.
    """
    suffix = path.suffix.lower()

    try:
        if suffix in EXCEL_SUFFIXES:
            return pl.read_excel(path)

        separator = "\t" if suffix == ".tsv" else ","
        return pl.read_csv(
            path,
            separator=separator,
            infer_schema_length=TYPE_INFERENCE_ROWS,
            try_parse_dates=True,
            ignore_errors=True,
            truncate_ragged_lines=True,
        )
    except Exception as exc:
        raise ValidationError(f"Could not read this file: {exc}") from exc


def detect_schema(frame: pl.DataFrame) -> DetectedSchema:
    """Describe a dataframe's columns."""
    if frame.width == 0:
        raise ValidationError("The file contains no columns.")

    taken: set[str] = set()
    columns: list[DetectedColumn] = []

    for position, (name, dtype) in enumerate(zip(frame.columns, frame.dtypes, strict=True)):
        series = frame[name]
        null_count = series.null_count()

        samples = series.drop_nulls().head(SAMPLE_VALUE_COUNT).to_list()

        columns.append(
            DetectedColumn(
                position=position,
                name=str(name)[:300],
                normalized_name=normalize_column_name(str(name), position=position, taken=taken),
                data_type=_map_dtype(dtype),
                source_type=str(dtype),
                nullable=null_count > 0,
                # JSONB cannot hold dates or Decimals; stringify anything that
                # is not a JSON primitive.
                sample_values=[_jsonable(value) for value in samples],
            )
        )

    return DetectedSchema(columns=columns, row_count=frame.height)


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return str(value)


def validate_upload_name(filename: str) -> str:
    """Check the extension is one we can actually parse."""
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        supported = ", ".join(sorted(SUPPORTED_SUFFIXES))
        raise ValidationError(
            f"Unsupported file type {suffix or '(none)'}. Supported types: {supported}."
        )
    return suffix
