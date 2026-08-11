"""Safety gate for model-generated SQL.

The language model is untrusted input. Not because it is malicious, but because
its output is steered by data we do not control — a CSV column could be named
`"; DROP TABLE`, and a user question is free text. Prompt instructions are not
a security control; the engine's behaviour is.

Three independent layers, each verified against DuckDB 1.5 rather than assumed:

1. **`enable_external_access=false` on the connection** — the load-bearing one.
   Without it, DuckDB reads and writes the local filesystem happily:
   `read_csv('/etc/passwd')`, `COPY (...) TO '/tmp/out.csv'`, `ATTACH`,
   `INSTALL`, `glob`. Crucially this is a *one-way latch*: a generated
   `SET enable_external_access=true` fails with "Cannot enable external access
   while database is running", so the model cannot undo it.

   Note this is emphatically **not** covered by `read_only=True`. A read-only
   connection still permits every one of those file operations — read-only
   protects the database's contents, not the host.

2. **Statement validation via DuckDB's own parser** — `extract_statements`
   returns real statement types, so chaining (`SELECT 1; DROP TABLE t` parses
   as two statements) and DDL/DML are rejected structurally rather than by
   pattern-matching SQL text, which is notoriously easy to evade.

3. **A textual denylist** — defence in depth for the gap the parser cannot
   see: `SELECT * FROM read_csv(...)` is a perfectly ordinary `SELECT`
   statement. Layer 1 already blocks it at execution; this layer rejects it
   earlier, with an error message the agent can feed back to the model for a
   corrected attempt.

Cross-tenant access is structurally impossible here regardless: each workspace
has its own DuckDB file, so there is no other tenant's data inside the
database being queried. The table allowlist below is therefore a correctness
guard, not a security boundary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import duckdb

from decisionflow.core.errors import UnsafeQueryError

# Hard cap on rows returned to the caller, applied even when the model asks
# for more. A question like "list every order" should not stream a million
# rows through the API and into a prompt.
MAX_ROW_LIMIT = 1000
DEFAULT_ROW_LIMIT = 200

# Functions and keywords that reach outside the database. Every one of these is
# already blocked by layer 1; listing them here turns an opaque permission
# error into a specific, actionable message.
_FORBIDDEN_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bread_csv\w*\s*\(", re.I), "reading files is not permitted"),
    (re.compile(r"\bread_parquet\s*\(", re.I), "reading files is not permitted"),
    (re.compile(r"\bread_json\w*\s*\(", re.I), "reading files is not permitted"),
    (re.compile(r"\bread_text\s*\(", re.I), "reading files is not permitted"),
    (re.compile(r"\bread_blob\s*\(", re.I), "reading files is not permitted"),
    (re.compile(r"\bglob\s*\(", re.I), "listing files is not permitted"),
    (re.compile(r"\bcopy\b", re.I), "COPY is not permitted"),
    (re.compile(r"\battach\b", re.I), "ATTACH is not permitted"),
    (re.compile(r"\bdetach\b", re.I), "DETACH is not permitted"),
    (re.compile(r"\binstall\b", re.I), "installing extensions is not permitted"),
    (re.compile(r"\bload\s+\w+", re.I), "loading extensions is not permitted"),
    (re.compile(r"\bpragma\b", re.I), "PRAGMA is not permitted"),
    (re.compile(r"\bexport\b", re.I), "EXPORT is not permitted"),
    (re.compile(r"\bimport\b", re.I), "IMPORT is not permitted"),
    # Dollar-quoted strings can smuggle text past naive scanners; we have no
    # legitimate use for them in generated analytics SQL.
    (re.compile(r"\$\$"), "dollar-quoted strings are not permitted"),
)

_LIMIT_TAIL = re.compile(r"\blimit\s+(\d+)\s*$", re.I)


@dataclass(frozen=True, slots=True)
class SafeQuery:
    """A query that passed every check, with its enforced row limit."""

    sql: str
    limit: int


def _strip_trailing_semicolon(sql: str) -> str:
    return sql.strip().rstrip(";").strip()


def _unwrap_markdown(sql: str) -> str:
    """Remove ```sql fences the model adds despite being told not to.

    Cheap and worth doing: rejecting an otherwise-valid query over formatting
    burns a whole retry round trip for no benefit.
    """
    text = sql.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def validate(sql: str, *, allowed_tables: set[str] | None = None) -> SafeQuery:
    """Check a generated query, or raise `UnsafeQueryError`.

    The error message is deliberately specific: the agent feeds it back to the
    model as correction context, so "COPY is not permitted" produces a better
    retry than "unsafe query".
    """
    cleaned = _strip_trailing_semicolon(_unwrap_markdown(sql))
    if not cleaned:
        raise UnsafeQueryError("The generated query was empty.")

    # --- layer 3: textual denylist (runs first, for better messages) -------
    for pattern, reason in _FORBIDDEN_PATTERNS:
        if pattern.search(cleaned):
            raise UnsafeQueryError(f"Query rejected: {reason}.")

    # --- layer 2: parse with DuckDB's own parser --------------------------
    try:
        statements = duckdb.extract_statements(cleaned)
    except Exception as exc:
        raise UnsafeQueryError(f"The generated query is not valid SQL: {exc}") from exc

    if len(statements) != 1:
        raise UnsafeQueryError(
            f"Expected exactly one statement, got {len(statements)}. "
            "Statement chaining is not permitted."
        )

    statement_type = str(statements[0].type).rsplit(".", maxsplit=1)[-1].upper()
    if statement_type != "SELECT":
        raise UnsafeQueryError(
            f"Only SELECT statements are permitted, got {statement_type}."
        )

    if allowed_tables is not None:
        _check_tables(cleaned, allowed_tables)

    return SafeQuery(sql=_apply_limit(cleaned), limit=_effective_limit(cleaned))


def _check_tables(sql: str, allowed: set[str]) -> None:
    """Reject references to tables outside the allowlist.

    A correctness guard rather than a security one — the workspace's DuckDB
    file contains only this tenant's data — so it is intentionally forgiving:
    it looks at what follows FROM and JOIN, and ignores CTE names, which are
    defined within the query itself.
    """
    cte_names = {
        match.group(1).lower()
        for match in re.finditer(r"\b(\w+)\s+AS\s*\(", sql, re.I)
    }
    referenced = {
        match.group(1).lower()
        for match in re.finditer(r"\b(?:from|join)\s+([A-Za-z_][\w.]*)", sql, re.I)
    }

    permitted = {name.lower() for name in allowed} | cte_names
    unknown = {
        name
        for name in referenced
        if name not in permitted and name.split(".")[-1] not in permitted
    }
    if unknown:
        raise UnsafeQueryError(
            f"Query references unknown tables: {', '.join(sorted(unknown))}. "
            f"Available: {', '.join(sorted(allowed))}."
        )


def _effective_limit(sql: str) -> int:
    match = _LIMIT_TAIL.search(sql)
    if match:
        return min(int(match.group(1)), MAX_ROW_LIMIT)
    return DEFAULT_ROW_LIMIT


def _apply_limit(sql: str) -> str:
    """Ensure the query returns a bounded number of rows.

    An existing LIMIT above the cap is lowered by wrapping rather than by
    rewriting the text — regex surgery on SQL is how subtle bugs get shipped.
    """
    match = _LIMIT_TAIL.search(sql)
    if match is None:
        return f"{sql} LIMIT {DEFAULT_ROW_LIMIT}"

    if int(match.group(1)) <= MAX_ROW_LIMIT:
        return sql

    # `sql` reached here only after passing every check above, and the cap is a
    # module constant.
    return f"SELECT * FROM ({sql}) LIMIT {MAX_ROW_LIMIT}"  # noqa: S608
