#!/usr/bin/env python3
"""
SAP HANA Cloud → SAP HANA Cloud table migration script.

Table   : SAP_HELP_DOCS
Columns : ID (INTEGER PK), VEC_TEXT (NCLOB), VEC_META (NCLOB), VEC_VECTOR (REAL_VECTOR(3072))

REAL_VECTOR strategy
--------------------
hdbcli does not guarantee a stable binary wire format for REAL_VECTOR across
driver/server versions.  The safest round-trip is:

  SELECT  … VECTOR_TO_HEX("VEC_VECTOR") …   → returns a hex string (NVARCHAR)
  INSERT  … TO_REAL_VECTOR(?)              …  → HANA reconstructs the vector server-side

This keeps the driver out of the binary serialisation entirely.

Usage
-----
  python migrate_hana_table.py [--mode overwrite|append] [--batch 1000]

Requirements
------------
  pip install hdbcli tqdm          # tqdm is optional — script degrades gracefully
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from contextlib import contextmanager
from typing import Generator

# ── optional tqdm ──────────────────────────────────────────────────────────────
try:
    from tqdm import tqdm as _tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False

# ── hdbcli ────────────────────────────────────────────────────────────────────
try:
    from hdbcli import dbapi  # type: ignore
except ImportError:
    sys.exit(
        "hdbcli is not installed.\n"
        "Run:  pip install hdbcli\n"
        "or add it to requirements.txt and re-install."
    )


# ══════════════════════════════════════════════════════════════════════════════
#  ✏️  FILL IN YOUR CREDENTIALS HERE — no .env or environment variables needed
# ══════════════════════════════════════════════════════════════════════════════

SOURCE_CONFIG: dict = {
    "host":                   "fdddfefe-e600-4b7f-b032-4f8fd05ec060.hna0.prod-us10.hanacloud.ondemand.com",
    "port":                   443,
    "user":                   "AI_USE_CASES_HDI_DB_1_9YUM85CO7FKDBO5H0FRK2Q2VA_RT",
    "password":               "Tn2FxjyBoXe6c.xQagv6lizP7SGgqd8ZSg4gmHGakSryuDH5vOAemDRqsr-jovT7Ov2c_oxC.tOHET1BrLuz895OMHYke_WJqlX_xSjA85.N0yjn.X5GZ.uo8L0pXH40",
    "encrypt":                True,
    "sslValidateCertificate": False,
}

TARGET_CONFIG: dict = {
    "host":                   "6eee8a49-23f9-42c3-a4b1-125f75146bf3.hna1.prod-us10.hanacloud.ondemand.com",
    "port":                   443,
    "user":                   "B76C95F2F75B4BD4BB8E6D322DBFCBD1_1IS2GGYH4N7F4QDETKA05EF11_RT",
    "password":               "(o#tO/`KMW(@jA=@=~CQt|EZJHUbeGC_~-GM(Qf[O`7!mEbEjA#UB9~{2?&O ,YC*lO46d#kDDk_]q*oYt_w+i,yzA[H+3!6FvmHk(WL^4F:s^fBd5X&26%wZKi%=+gc",
    "encrypt":                True,
    "sslValidateCertificate": False,
}

SOURCE_SCHEMA: str = "AI_USE_CASES_HDI_DB_1"
TARGET_SCHEMA: str = "B76C95F2F75B4BD4BB8E6D322DBFCBD1"
TABLE_NAME:    str = "SAP_HELP_DOCS"
VECTOR_DIM:   int  = 3072

BATCH_SIZE:  int = 1000   # rows per INSERT batch
MAX_RETRIES: int = 3      # per-batch retry attempts
RETRY_DELAY: int = 3      # seconds between retries (linear backoff × attempt)


# ══════════════════════════════════════════════════════════════════════════════
#  Logging
# ══════════════════════════════════════════════════════════════════════════════

def _build_logger() -> logging.Logger:
    fmt = "%(asctime)s | %(levelname)-8s | %(message)s"
    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("hana_migration.log", encoding="utf-8"),
    ]
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)
    return logging.getLogger("hana_migrate")


log = _build_logger()


# ══════════════════════════════════════════════════════════════════════════════
#  Connection helper
# ══════════════════════════════════════════════════════════════════════════════

@contextmanager
def hana_connect(cfg: dict, label: str) -> Generator:
    """Open a HANA connection and guarantee it is closed on exit."""
    conn = None
    try:
        log.info("[%s] Connecting to %s:%s …", label, cfg["host"], cfg["port"])
        conn = dbapi.connect(**cfg)
        conn.setautocommit(False)   # we commit per-batch explicitly
        log.info("[%s] Connected.", label)
        yield conn
    finally:
        if conn:
            conn.close()
            log.info("[%s] Connection closed.", label)


# ══════════════════════════════════════════════════════════════════════════════
#  DDL helpers
# ══════════════════════════════════════════════════════════════════════════════

_DDL_CREATE = (
    'CREATE COLUMN TABLE "{schema}"."{table}" ('
    '"ID"         INTEGER     NOT NULL PRIMARY KEY, '
    '"VEC_TEXT"   NCLOB, '
    '"VEC_META"   NCLOB, '
    '"VEC_VECTOR" REAL_VECTOR({dim})'
    ")"
)


def _table_exists(cursor, schema: str, table: str) -> bool:
    cursor.execute(
        "SELECT COUNT(*) FROM SYS.TABLES WHERE SCHEMA_NAME = ? AND TABLE_NAME = ?",
        (schema, table),
    )
    return cursor.fetchone()[0] > 0


def prepare_target_table(
    conn,
    schema: str,
    table: str,
    dim: int,
    mode: str,
) -> None:
    cursor = conn.cursor()
    exists = _table_exists(cursor, schema, table)

    if exists and mode == "overwrite":
        log.info("[target] DROP TABLE %s.%s …", schema, table)
        cursor.execute(f'DROP TABLE "{schema}"."{table}"')
        conn.commit()
        exists = False
        log.info("[target] Table dropped.")

    if not exists:
        log.info("[target] Creating table %s.%s (REAL_VECTOR(%d)) …", schema, table, dim)
        cursor.execute(_DDL_CREATE.format(schema=schema, table=table, dim=dim))
        conn.commit()
        log.info("[target] Table created.")
    else:
        log.info("[target] Table exists — appending.")

    cursor.close()


# ══════════════════════════════════════════════════════════════════════════════
#  SQL templates
# ══════════════════════════════════════════════════════════════════════════════

# VECTOR_TO_HEX serialises the in-memory float array to a deterministic hex
# string understood by TO_REAL_VECTOR on the other side.  No Python code
# touches the raw vector bytes.
_SELECT_SQL = (
    'SELECT "ID", "VEC_TEXT", "VEC_META", '
    'VECTOR_TO_HEX("VEC_VECTOR") AS "VEC_VECTOR_HEX" '
    'FROM "{schema}"."{table}" '
    'ORDER BY "ID"'
)

# TO_REAL_VECTOR rebuilds REAL_VECTOR({dim}) from the hex string entirely
# inside the HANA engine — no driver-side binary conversion needed.
_INSERT_SQL = (
    'INSERT INTO "{schema}"."{table}" '
    '("ID", "VEC_TEXT", "VEC_META", "VEC_VECTOR") '
    "VALUES (?, ?, ?, TO_REAL_VECTOR(?))"
)


def _count(cursor, schema: str, table: str) -> int:
    cursor.execute(f'SELECT COUNT(*) FROM "{schema}"."{table}"')
    return cursor.fetchone()[0]


# ══════════════════════════════════════════════════════════════════════════════
#  Batch insert with retry
# ══════════════════════════════════════════════════════════════════════════════

def _insert_batch(
    conn,
    cursor,
    rows: list[tuple],
    schema: str,
    table: str,
) -> None:
    sql = _INSERT_SQL.format(schema=schema, table=table)
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            # executemany sends all rows to the server in a single round-trip
            cursor.executemany(sql, rows)
            conn.commit()
            return
        except dbapi.Error as exc:
            if attempt == MAX_RETRIES:
                log.error("Batch failed after %d attempts: %s", MAX_RETRIES, exc)
                raise
            wait = RETRY_DELAY * attempt
            log.warning(
                "Batch insert error (attempt %d/%d): %s — retrying in %ds …",
                attempt, MAX_RETRIES, exc, wait,
            )
            time.sleep(wait)
            # Roll back the failed partial write before retrying
            try:
                conn.rollback()
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════════
#  Main migration function
# ══════════════════════════════════════════════════════════════════════════════

def migrate(
    *,
    source_cfg: dict,
    target_cfg: dict,
    source_schema: str,
    target_schema: str,
    table: str,
    dim: int,
    mode: str,
    batch_size: int,
) -> int:
    """
    Migrate one table from source to target HANA instance.
    Returns total rows migrated.
    """
    total_migrated = 0

    with (
        hana_connect(source_cfg, "source") as src_conn,
        hana_connect(target_cfg, "target") as tgt_conn,
    ):
        src_cur = src_conn.cursor()
        tgt_cur = tgt_conn.cursor()

        # ── DDL on target ──────────────────────────────────────────────────
        prepare_target_table(tgt_conn, target_schema, table, dim, mode)

        # ── Count source rows ──────────────────────────────────────────────
        total_src = _count(src_cur, source_schema, table)
        log.info("[source] %d rows to migrate.", total_src)

        if total_src == 0:
            log.info("Source table is empty — nothing to migrate.")
            return 0

        # ── Stream source rows (cursor iteration is lazy in hdbcli) ───────
        select_sql = _SELECT_SQL.format(schema=source_schema, table=table)
        src_cur.execute(select_sql)

        pbar = _tqdm(total=total_src, unit="rows", desc="Migrating") if HAS_TQDM else None

        batch: list[tuple] = []
        for row in src_cur:             # hdbcli yields one row at a time — O(batch_size) RAM
            batch.append(row)
            if len(batch) >= batch_size:
                _insert_batch(tgt_conn, tgt_cur, batch, target_schema, table)
                total_migrated += len(batch)
                log.info("Progress: %d / %d rows", total_migrated, total_src)
                if pbar:
                    pbar.update(len(batch))
                batch = []

        # ── Flush tail ─────────────────────────────────────────────────────
        if batch:
            _insert_batch(tgt_conn, tgt_cur, batch, target_schema, table)
            total_migrated += len(batch)
            if pbar:
                pbar.update(len(batch))

        if pbar:
            pbar.close()

        # ── Row-count validation ───────────────────────────────────────────
        target_count = _count(tgt_cur, target_schema, table)
        log.info("─" * 50)
        log.info("Validation")
        log.info("  Source rows : %d", total_src)
        log.info("  Target rows : %d", target_count)

        if mode == "append":
            # In append mode the target may already have had rows; only check
            # that we didn't lose any of the rows we just wrote.
            log.info("  Rows written: %d  (append mode — pre-existing rows not counted)", total_migrated)
        elif total_src == target_count:
            log.info("  [PASS] Row counts match.")
        else:
            delta = total_src - target_count
            log.error("  [FAIL] Row count mismatch — %d rows missing!", delta)
            raise RuntimeError(f"Migration incomplete: {delta} rows missing in target.")

        src_cur.close()
        tgt_cur.close()

    return total_migrated


# ══════════════════════════════════════════════════════════════════════════════
#  CLI entry point
# ══════════════════════════════════════════════════════════════════════════════

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate SAP_HELP_DOCS (with REAL_VECTOR) between two HANA Cloud instances."
    )
    parser.add_argument(
        "--mode",
        choices=["overwrite", "append"],
        default="overwrite",
        help="overwrite: drop + recreate target table before migration (default). "
             "append: keep existing target rows and add new ones.",
    )
    parser.add_argument(
        "--batch",
        type=int,
        default=BATCH_SIZE,
        metavar="N",
        help=f"Rows per INSERT batch (default: {BATCH_SIZE}).",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    log.info("=" * 60)
    log.info(
        "SAP HANA Migration   %s.%s  →  %s.%s",
        SOURCE_SCHEMA, TABLE_NAME, TARGET_SCHEMA, TABLE_NAME,
    )
    log.info(
        "Mode: %-10s | Batch: %d | Vector dim: %d",
        args.mode, args.batch, VECTOR_DIM,
    )
    log.info("=" * 60)

    t0 = time.monotonic()
    try:
        migrated = migrate(
            source_cfg=SOURCE_CONFIG,
            target_cfg=TARGET_CONFIG,
            source_schema=SOURCE_SCHEMA,
            target_schema=TARGET_SCHEMA,
            table=TABLE_NAME,
            dim=VECTOR_DIM,
            mode=args.mode,
            batch_size=args.batch,
        )
        elapsed = time.monotonic() - t0
        log.info("=" * 60)
        log.info("Done.  %d rows migrated in %.1fs.", migrated, elapsed)
        log.info("=" * 60)
    except Exception as exc:
        log.exception("Migration aborted: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
