"""
SQL safety guard for the preprocessing layer.

Enforces that every SQL string sent to Open Dental's database from preprocessing
modules is a single read-only SELECT (or WITH ... SELECT). Anything else raises
SqlSafetyError before the SQL leaves the process.

This is one of two enforcement layers — the other is a test that AST-walks
preprocessing/ and rejects callsites that bypass _query_database.
"""

from __future__ import annotations

import re


class SqlSafetyError(ValueError):
    """Raised when a SQL string violates the read-only contract."""


_FORBIDDEN_KEYWORDS = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "ALTER",
    "CREATE",
    "TRUNCATE",
    "REPLACE",
    "MERGE",
    "GRANT",
    "REVOKE",
    "CALL",
    "EXEC",
    "EXECUTE",
    "LOCK",
    "RENAME",
    "HANDLER",
    "LOAD",
    "INTO OUTFILE",
    "INTO DUMPFILE",
)


def _strip_comments(sql: str) -> str:
    """Remove SQL line and block comments so they can't hide DML keywords."""
    # /* ... */ block comments (non-greedy, multi-line)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    # -- line comments (to end of line)
    sql = re.sub(r"--[^\n]*", " ", sql)
    # # line comments (MySQL)
    sql = re.sub(r"(^|\s)#[^\n]*", " ", sql)
    return sql


def _strip_string_literals(sql: str) -> str:
    """Remove single-quoted and double-quoted string literals so the keywords
    they contain are not flagged. Backtick-quoted identifiers are preserved
    (only quotes are stripped) to keep them out of keyword scanning too."""
    # Single-quoted strings (handles '' as escaped quote)
    sql = re.sub(r"'(?:''|[^'])*'", "''", sql)
    # Double-quoted strings
    sql = re.sub(r'"(?:""|[^"])*"', '""', sql)
    # Backtick identifiers — replace contents with empty backticks
    sql = re.sub(r"`(?:``|[^`])*`", "``", sql)
    return sql


def assert_select_only(sql: str) -> None:
    """Raise SqlSafetyError unless `sql` is a single read-only SELECT/WITH+SELECT.

    Rules:
    - Must be a non-empty string.
    - After comment-stripping, must contain exactly one top-level statement
      (no statement-terminating semicolons except an optional trailing one).
    - The first keyword must be SELECT, WITH (CTE), SHOW, DESCRIBE, DESC, or EXPLAIN.
    - Must not contain any DML/DDL keyword as a standalone token.
    """
    if not isinstance(sql, str) or not sql.strip():
        raise SqlSafetyError("SQL must be a non-empty string")

    cleaned = _strip_comments(sql)
    cleaned = _strip_string_literals(cleaned)

    # Disallow multi-statement: any non-trailing ';' is a hard reject.
    stripped = cleaned.strip().rstrip(";").strip()
    if ";" in stripped:
        raise SqlSafetyError("Multi-statement SQL is not allowed")

    if not stripped:
        raise SqlSafetyError("SQL contains no statement after comment removal")

    # First keyword check.
    first_token_match = re.match(r"\s*([A-Za-z_]+)", stripped)
    if not first_token_match:
        raise SqlSafetyError("SQL has no leading keyword")
    first = first_token_match.group(1).upper()
    allowed_starts = {"SELECT", "WITH", "SHOW", "DESCRIBE", "DESC", "EXPLAIN"}
    if first not in allowed_starts:
        raise SqlSafetyError(
            f"Statement must start with one of {sorted(allowed_starts)}; got {first!r}"
        )

    # Standalone-token scan for forbidden keywords.
    upper = stripped.upper()
    for kw in _FORBIDDEN_KEYWORDS:
        # Word-boundary match. For multi-word keywords ("INTO OUTFILE") the
        # whitespace matching handles the space.
        pattern = r"(?<![A-Z0-9_])" + re.escape(kw) + r"(?![A-Z0-9_])"
        if re.search(pattern, upper):
            raise SqlSafetyError(f"Forbidden keyword in SQL: {kw}")
