"""
database/init_db.py

Initializes the SQLite database from schema.sql.
Converts PostgreSQL-specific syntax (BIGSERIAL, BOOLEAN, NOW()) to SQLite equivalents.
"""

import os
import re
import sqlite3
from pathlib import Path

from config.settings import DATABASE_URL


def get_db_path(url: str) -> str:
    """Extract file path from SQLAlchemy SQLite URL."""
    # sqlite:///absolute/path  or  sqlite:////abs/path
    if url.startswith("sqlite:///"):
        path = url[len("sqlite:///"):]
        # On Windows, sqlite:///C:/... gives C:/...
        return path
    raise ValueError(f"Not a SQLite URL: {url}")


def adapt_schema(sql: str) -> str:
    """Convert PostgreSQL schema syntax to SQLite-compatible SQL."""
    # BIGSERIAL PRIMARY KEY -> INTEGER PRIMARY KEY AUTOINCREMENT
    sql = re.sub(r"\bBIGSERIAL\s+PRIMARY\s+KEY\b", "INTEGER PRIMARY KEY AUTOINCREMENT", sql, flags=re.IGNORECASE)
    # BIGSERIAL (non-PK, shouldn't appear but handle anyway)
    sql = re.sub(r"\bBIGSERIAL\b", "INTEGER", sql, flags=re.IGNORECASE)
    # BOOLEAN -> INTEGER (SQLite stores 0/1)
    sql = re.sub(r"\bBOOLEAN\b", "INTEGER", sql, flags=re.IGNORECASE)
    # JSONB -> TEXT
    sql = re.sub(r"\bJSONB\b", "TEXT", sql, flags=re.IGNORECASE)
    # NOW() -> CURRENT_TIMESTAMP
    sql = re.sub(r"\bNOW\(\)", "CURRENT_TIMESTAMP", sql, flags=re.IGNORECASE)
    # DECIMAL(x,y) is fine in SQLite — leave it
    # TIMESTAMP is fine in SQLite — leave it
    # Remove foreign key references (SQLite ignores them without PRAGMA but they cause parse errors with some syntax)
    # Actually SQLite accepts REFERENCES syntax, just doesn't enforce — leave them
    return sql


def init_db():
    db_path = get_db_path(DATABASE_URL)
    schema_path = Path(__file__).parent / "schema.sql"

    print(f"Database path : {db_path}")
    print(f"Schema file   : {schema_path}")

    if not schema_path.exists():
        raise FileNotFoundError(f"schema.sql not found at {schema_path}")

    raw_sql = schema_path.read_text(encoding="utf-8")

    # Strip line comments BEFORE splitting on ";" to avoid semicolons inside comments
    comment_free_lines = []
    for line in raw_sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            continue  # drop entire comment line
        # Remove inline comment (everything after  --)
        if "--" in line:
            line = line[:line.index("--")]
        comment_free_lines.append(line)
    comment_free_sql = "\n".join(comment_free_lines)

    adapted_sql = adapt_schema(comment_free_sql)

    # Split into individual statements
    statements = []
    for stmt in adapted_sql.split(";"):
        clean = stmt.strip()
        if clean:
            statements.append(clean)

    # Create parent directory if needed
    os.makedirs(os.path.dirname(db_path), exist_ok=True) if os.path.dirname(db_path) else None

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")

    tables_created = 0
    indexes_created = 0

    try:
        for stmt in statements:
            upper = stmt.upper().lstrip()
            if not upper:
                continue
            conn.execute(stmt)
            if upper.startswith("CREATE TABLE"):
                tables_created += 1
            elif upper.startswith("CREATE INDEX"):
                indexes_created += 1
        conn.commit()
    except Exception as e:
        conn.rollback()
        conn.close()
        raise RuntimeError(f"Schema execution failed: {e}\n\nStatement was:\n{stmt}") from e

    conn.close()

    print(f"Database initialized: {tables_created} tables, {indexes_created} indexes created")
    print(f"File exists: {os.path.exists(db_path)}")
    return db_path


if __name__ == "__main__":
    init_db()
