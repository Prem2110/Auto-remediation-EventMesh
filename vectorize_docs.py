"""
vectorize_docs.py — Generate embeddings for SAP_HELP_DOCS and store in HANA
============================================================================
Reads every row in SAP_HELP_DOCS that has no VEC_VECTOR yet,
calls SAP AI Core text-embedding-3-large, and writes the 3072-dim vector back.

Usage:
    python vectorize_docs.py              # vectorize all un-embedded rows
    python vectorize_docs.py --batch 50   # override batch size (default 20)
    python vectorize_docs.py --dry-run    # show counts only, no writes

Safe to re-run — already-vectorized rows (VEC_VECTOR IS NOT NULL) are skipped.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from dotenv import load_dotenv
from hdbcli import dbapi
from yaspin import yaspin
from yaspin.spinners import Spinners
from gen_ai_hub.proxy.native.openai import OpenAI

load_dotenv()

# ── Config ─────────────────────────────────────────────────────────────────────
HANA_ADDRESS          = os.getenv("HANA_ADDRESS") or os.getenv("HANA_HOST")
HANA_PORT             = int(os.getenv("HANA_PORT", "443"))
HANA_USER             = os.getenv("HANA_USER")
HANA_PASSWORD         = os.getenv("HANA_PASSWORD")
HANA_SCHEMA           = os.getenv("HANA_SCHEMA")
HANA_TABLE            = os.getenv("HANA_TABLE_SAP_DOCS", "SAP_HELP_DOCS")

EMBEDDING_DEPLOYMENT  = os.getenv("EMBEDDING_DEPLOYMENT_ID", "d703b4441112f83c")
VECTOR_DIMENSION      = int(os.getenv("VECTOR_DIMENSION", "3072"))
DEFAULT_BATCH_SIZE    = 100  # rows per AI Core call (max ~2048 tokens each)


# ── HANA helpers ───────────────────────────────────────────────────────────────

def _connect() -> dbapi.Connection:
    conn = dbapi.connect(
        address=HANA_ADDRESS, port=HANA_PORT,
        user=HANA_USER, password=HANA_PASSWORD,
        encrypt=True, **{"sslValidateCertificate": False},
    )
    cur = conn.cursor()
    cur.execute(f'SET SCHEMA "{HANA_SCHEMA}"')
    cur.close()
    return conn


def _ensure_vector_column(conn: dbapi.Connection) -> None:
    """Add VEC_VECTOR column if it doesn't exist yet."""
    cur = conn.cursor()
    try:
        cur.execute(
            f'ALTER TABLE "{HANA_TABLE}" '
            f'ADD ("VEC_VECTOR" REAL_VECTOR({VECTOR_DIMENSION}))'
        )
        conn.commit()
        print(f"  ✔  Added VEC_VECTOR REAL_VECTOR({VECTOR_DIMENSION}) to {HANA_TABLE}")
    except Exception:
        conn.rollback()
        # Column already exists — that's fine


def _count_pending(conn: dbapi.Connection) -> int:
    cur = conn.cursor()
    try:
        cur.execute(f'SELECT COUNT(*) FROM "{HANA_TABLE}" WHERE "VEC_VECTOR" IS NULL')
        row = cur.fetchone()
        return int(row[0]) if row else 0  # type: ignore[index]
    except Exception:
        # Column doesn't exist yet — all rows need vectorizing
        return _count_total(conn)


def _count_total(conn: dbapi.Connection) -> int:
    cur = conn.cursor()
    cur.execute(f'SELECT COUNT(*) FROM "{HANA_TABLE}"')
    row = cur.fetchone()
    return int(row[0]) if row else 0  # type: ignore[index]


def _fetch_batch(conn: dbapi.Connection, batch_size: int) -> list[tuple[int, str]]:
    """Return up to batch_size (id, vec_text) rows that have no vector yet."""
    cur = conn.cursor()
    cur.execute(
        f'SELECT TOP {batch_size} "ID", "VEC_TEXT" '
        f'FROM "{HANA_TABLE}" '
        f'WHERE "VEC_VECTOR" IS NULL'
    )
    return cur.fetchall()


def _update_vectors(
    conn: dbapi.Connection,
    id_vector_pairs: list[tuple[int, list[float]]],
) -> None:
    cur = conn.cursor()
    for row_id, vector in id_vector_pairs:
        vec_str = "[" + ",".join(str(v) for v in vector) + "]"
        cur.execute(
            f'UPDATE "{HANA_TABLE}" '
            f'SET "VEC_VECTOR" = TO_REAL_VECTOR(\'{vec_str}\') '
            f'WHERE "ID" = {row_id}'
        )
    conn.commit()


# ── Embedding ──────────────────────────────────────────────────────────────────

def _embed_texts(client: OpenAI, texts: list[str]) -> list[list[float]]:
    """Call AI Core and return list of embedding vectors."""
    resp = client.embeddings.create(
        deployment_id=EMBEDDING_DEPLOYMENT,
        input=texts,
    )
    # resp.data is sorted by index
    return [item.embedding for item in sorted(resp.data, key=lambda x: x.index)]


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Vectorize SAP_HELP_DOCS rows")
    parser.add_argument("--batch",   type=int, default=DEFAULT_BATCH_SIZE,
                        help=f"Rows per embedding API call (default {DEFAULT_BATCH_SIZE})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show counts only, do not write vectors")
    args = parser.parse_args()

    start = time.time()

    # ── Connect ────────────────────────────────────────────────────────────────
    with yaspin(Spinners.dots, text=f"Connecting to HANA  {HANA_ADDRESS}…", color="yellow") as sp:
        try:
            conn = _connect()
            sp.ok(f"  ✔  Connected  schema={HANA_SCHEMA}")
        except Exception as exc:
            sp.fail(f"  ✘  HANA connection failed: {exc}")
            sys.exit(1)

    # ── Ensure VEC_VECTOR column exists ───────────────────────────────────────
    if not args.dry_run:
        _ensure_vector_column(conn)

    # ── Counts ─────────────────────────────────────────────────────────────────
    total   = _count_total(conn)
    pending = _count_pending(conn)
    done    = total - pending
    print(f"\n  {HANA_SCHEMA}.{HANA_TABLE}")
    print(f"  Total rows   : {total}")
    print(f"  Already done : {done}")
    print(f"  To vectorize : {pending}\n")

    if pending == 0:
        print("  Nothing to do — all rows already have vectors.\n")
        conn.close()
        return

    if args.dry_run:
        print("  --dry-run: skipping embedding writes.\n")
        conn.close()
        return

    # ── Vectorize in batches ───────────────────────────────────────────────────
    embed_client  = OpenAI()
    vectorized    = 0
    failed        = 0
    batch_num     = 0
    total_batches = (pending + args.batch - 1) // args.batch

    print(f"  Batch size: {args.batch}  |  ~{total_batches} batches  |  deployment: {EMBEDDING_DEPLOYMENT}\n")

    with yaspin(Spinners.dots, text="Vectorizing…", color="cyan") as sp:
        while True:
            rows = _fetch_batch(conn, args.batch)
            if not rows:
                break

            batch_num += 1
            ids   = [r[0] for r in rows]
            texts = [r[1] or "" for r in rows]

            sp.text = (
                f"Batch [{batch_num}/{total_batches}]  "
                f"{vectorized} vectorized  {failed} failed"
            )

            try:
                vectors = _embed_texts(embed_client, texts)
                _update_vectors(conn, list(zip(ids, vectors)))
                vectorized += len(ids)
            except Exception as exc:
                failed += len(ids)
                sp.write(f"  ✘  Batch {batch_num} failed: {exc}")
                # Mark failed rows with a zero vector to skip on retry
                # (remove this if you want to retry failed rows next run)
                continue

        elapsed = time.time() - start
        sp.ok(
            f"  ✔  {vectorized} rows vectorized  |  {failed} failed  |  {elapsed:.1f}s"
        )

    conn.close()
    print(f"\n  Done. {vectorized}/{pending} rows now have embeddings.\n")
    if failed:
        print(f"  Re-run to retry {failed} failed rows (they still have NULL vectors).\n")


if __name__ == "__main__":
    main()
