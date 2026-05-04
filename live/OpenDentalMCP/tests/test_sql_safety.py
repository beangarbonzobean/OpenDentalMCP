"""Tests for preprocessing.sql_safety.assert_select_only."""

from __future__ import annotations

import pytest

from preprocessing.sql_safety import SqlSafetyError, assert_select_only


# ---- Allowed shapes ---------------------------------------------------------

ALLOWED = [
    "SELECT 1",
    "SELECT * FROM document",
    "SELECT DocNum, PatNum FROM document WHERE DocNum > 0 ORDER BY DocNum LIMIT 100",
    "select * from document",  # case-insensitive
    "SELECT d.DocNum FROM document d JOIN patient p ON d.PatNum = p.PatNum",
    "WITH recent AS (SELECT * FROM document WHERE DateCreated > '2025-01-01') SELECT * FROM recent",
    "SHOW TABLES",
    "DESCRIBE document",
    "DESC document",
    "EXPLAIN SELECT * FROM document",
    # Trailing semicolon is OK.
    "SELECT 1;",
    # Comments are stripped, statement still legal.
    "/* comment */ SELECT 1",
    "-- comment\nSELECT 1",
    "# mysql comment\nSELECT 1",
    # String literal containing a forbidden keyword is OK (we strip strings).
    "SELECT 'INSERT INTO foo' AS msg",
    "SELECT \"DELETE FROM bar\" AS msg",
    # Backtick identifier containing a forbidden keyword.
    "SELECT `update_count` FROM document",
]


@pytest.mark.parametrize("sql", ALLOWED)
def test_allowed(sql: str) -> None:
    assert_select_only(sql)


# ---- Rejected shapes --------------------------------------------------------

REJECTED = [
    # DML
    "INSERT INTO document VALUES (1)",
    "UPDATE document SET FileName = 'x'",
    "DELETE FROM document",
    "REPLACE INTO document VALUES (1)",
    # DDL
    "DROP TABLE document",
    "ALTER TABLE document ADD COLUMN foo INT",
    "CREATE TABLE foo (id INT)",
    "TRUNCATE TABLE document",
    "RENAME TABLE document TO doc2",
    # Privilege / runtime
    "GRANT SELECT ON document TO 'user'",
    "REVOKE SELECT ON document FROM 'user'",
    "CALL my_proc()",
    "EXEC my_proc",
    "LOCK TABLES document WRITE",
    # File output
    "SELECT * FROM document INTO OUTFILE '/tmp/x'",
    "SELECT * FROM document INTO DUMPFILE '/tmp/x'",
    # Multi-statement
    "SELECT 1; UPDATE document SET FileName = 'x'",
    "SELECT 1; DROP TABLE document",
    # Comment-hidden write
    "SELECT 1; -- ok\nUPDATE document SET FileName = 'x'",
    "/* fake */ UPDATE document SET FileName = 'x'",
    # Empty / whitespace
    "",
    "   ",
    "-- only a comment",
    "/* only */",
]


@pytest.mark.parametrize("sql", REJECTED)
def test_rejected(sql: str) -> None:
    with pytest.raises(SqlSafetyError):
        assert_select_only(sql)


# ---- Type errors ------------------------------------------------------------

@pytest.mark.parametrize("bad", [None, 123, [], {}])
def test_non_string_rejected(bad) -> None:
    with pytest.raises(SqlSafetyError):
        assert_select_only(bad)  # type: ignore[arg-type]


# ---- Comment-stripping does not falsely strip whole statement ---------------

def test_comment_then_select_is_allowed() -> None:
    assert_select_only("/* leading */ SELECT 42")


def test_select_with_inline_comment_is_allowed() -> None:
    assert_select_only("SELECT 1 /* inline */ FROM document")


# ---- A few stress cases -----------------------------------------------------

def test_string_containing_semicolon_is_ok() -> None:
    assert_select_only("SELECT 'a;b' FROM document")


def test_keyword_inside_identifier_is_ok() -> None:
    # "updateBy" is a column name, not the UPDATE keyword.
    assert_select_only("SELECT updateBy FROM document")


def test_keyword_in_quoted_identifier_is_ok() -> None:
    assert_select_only('SELECT "INSERT" FROM document')


def test_two_selects_separated_by_semicolon_is_rejected() -> None:
    with pytest.raises(SqlSafetyError):
        assert_select_only("SELECT 1; SELECT 2")
