"""
Vector Store — SAP_HELP_DOCS (HANA Cloud)
==========================================
Table: SAP_HELP_DOCS  (schema: HANA_SCHEMA)
Columns:
    ID          INTEGER             — auto-increment primary key
    VEC_TEXT    NCLOB               — chunked SAP Note text
    VEC_META    NCLOB               — JSON metadata (title, url, source, note_id)
    VEC_VECTOR  REAL_VECTOR(3072)   — text-embedding-3-large embedding (SAP AI Core)

Search strategy (tried in order):
    1. COSINE_SIMILARITY on VEC_VECTOR  — semantic vector search (best relevance)
    2. CONTAINS fuzzy search on VEC_TEXT — full-text fallback
    3. LIKE keyword search on VEC_TEXT   — reliable fallback
    4. SELECT TOP N scan                 — last resort

Env vars:
    HANA_ADDRESS            — HANA Cloud hostname
    HANA_PORT               — default 443
    HANA_USER               — runtime DB user
    HANA_PASSWORD           — runtime DB password
    HANA_SCHEMA             — schema name  (e.g. AI_USE_CASES_HDI_DB_1)
    HANA_TABLE_VECTOR       — table name   (default: SAP_HELP_DOCS)
    EMBEDDING_DEPLOYMENT_ID — SAP AI Core embedding deployment ID
    VECTOR_DIMENSION        — embedding dimensions (default: 3072)
"""

import json
import os
from typing import Dict, List, Optional

from hdbcli import dbapi

from utils.logger_config import setup_logger

logger = setup_logger(__name__)


class VectorStoreRetriever:
    """
    Retrieves relevant SAP Notes from SAP_HELP_DOCS using cosine similarity
    on 3072-dim embeddings, with full-text fuzzy search as fallback.
    """

    def __init__(self):
        self.host       = os.getenv("HANA_ADDRESS") or os.getenv("HANA_HOST")
        self.port       = int(os.getenv("HANA_PORT", "443"))
        self.user       = os.getenv("HANA_USER")
        self.password   = os.getenv("HANA_PASSWORD")
        self.schema     = os.getenv("HANA_SCHEMA")
        self.table      = os.getenv("HANA_TABLE_VECTOR", "SAP_HELP_DOCS")
        self.embed_deployment = os.getenv("EMBEDDING_DEPLOYMENT_ID")
        self.vector_dim = int(os.getenv("VECTOR_DIMENSION", "3072"))
        self.enabled    = all([self.host, self.user, self.password, self.schema, self.table])
        self._conn: Optional[dbapi.Connection] = None  # persistent connection

        if not self.enabled:
            logger.warning(
                "[VectorStore] Disabled — set HANA_ADDRESS, HANA_USER, HANA_PASSWORD, "
                "HANA_SCHEMA, HANA_TABLE_VECTOR in .env"
            )

        if not self.embed_deployment:
            logger.warning(
                "[VectorStore] EMBEDDING_DEPLOYMENT_ID not set — "
                "vector search disabled, falling back to fuzzy text search"
            )

    # ── Connection ────────────────────────────────────────────────────────────

    def _is_connection_alive(self) -> bool:
        """Cheaply test if the persistent connection is still usable."""
        if self._conn is None:
            return False
        try:
            cur = self._conn.cursor()
            cur.execute("SELECT 1 FROM DUMMY")
            cur.close()
            return True
        except Exception:
            return False

    def _get_connection(self) -> Optional[dbapi.Connection]:
        """
        Return the persistent HANA connection, reconnecting if it is dead.
        Opening one TCP connection per VectorStoreRetriever instance avoids
        exhausting HANA connection limits under concurrent RCA load.
        """
        if not self.enabled:
            return None
        if self._is_connection_alive():
            return self._conn
        # Connection is absent or stale — (re)connect
        try:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
            self._conn = dbapi.connect(
                address=self.host,
                port=self.port,
                user=self.user,
                password=self.password,
                encrypt=True,
                **{"sslValidateCertificate": False},
            )
            cur = self._conn.cursor()
            cur.execute(f'SET SCHEMA "{self.schema}"')
            cur.close()
            return self._conn
        except Exception as exc:
            logger.error("[VectorStore] HANA connection failed | error=%s", exc)
            self._conn = None
            return None

    # ── Embedding ─────────────────────────────────────────────────────────────

    def _embed_query(self, text: str) -> Optional[List[float]]:
        """Embed a query string using SAP AI Core text-embedding-3-large."""
        if not self.embed_deployment:
            return None
        try:
            from gen_ai_hub.proxy.native.openai import OpenAI  # type: ignore[import]
            client = OpenAI()
            resp = client.embeddings.create(
                deployment_id=self.embed_deployment,
                input=[text],
            )
            return resp.data[0].embedding
        except Exception as exc:
            logger.warning(
                "[VectorStore] Embedding failed — falling back to text search | error=%s", exc
            )
            return None

    # ── Retrieval ─────────────────────────────────────────────────────────────

    def retrieve_relevant_notes(
        self,
        error_message: str,
        error_type: str,
        iflow_id: str,
        limit: int = 5,
    ) -> List[Dict]:
        """
        Search SAP_HELP_DOCS for notes relevant to the given error.

        Args:
            error_message: Raw error message text from SAP CPI.
            error_type:    Classified type (MAPPING_ERROR, AUTH_ERROR, …).
            iflow_id:      Integration flow name.
            limit:         Maximum rows to return.

        Returns:
            List of dicts — each has: note_id, note_title, note_content,
            error_category, solution_steps, source, meta, similarity.
        """
        if not self.enabled:
            logger.debug("[VectorStore] Retrieval skipped — vector store is disabled")
            return []

        conn = self._get_connection()
        if not conn:
            logger.warning("[VectorStore] Retrieval failed — could not connect to HANA")
            return []

        query_text = f"{error_type} {error_message[:300]} {iflow_id}".strip()
        table_ref  = f'"{self.table}"'
        results: List[Dict] = []

        logger.info(
            "[VectorStore] Retrieval started | error_type=%s | iflow=%s | limit=%d",
            error_type, iflow_id, limit,
        )

        try:
            cur = conn.cursor()

            # ── 1. Cosine similarity — vector search ──────────────────────────
            embedding = self._embed_query(query_text)
            if embedding:
                try:
                    vector_str = json.dumps(embedding)
                    cur.execute(
                        f"""
                        SELECT TOP {limit}
                            VEC_TEXT,
                            VEC_META,
                            COSINE_SIMILARITY(VEC_VECTOR, TO_REAL_VECTOR(?)) AS score
                        FROM {table_ref}
                        WHERE VEC_VECTOR IS NOT NULL
                        ORDER BY score DESC
                        """,
                        (vector_str,),
                    )
                    rows = cur.fetchall()
                    if rows:
                        results = self._rows_to_dicts(rows, include_score=True)
                        logger.info(
                            "[VectorStore] Vector search SUCCESS | returned=%d | "
                            "error_type=%s | top_score=%.4f | top_titles=%s",
                            len(results), error_type,
                            results[0].get("similarity", 0),
                            [r.get("note_title", "N/A") for r in results[:3]],
                        )
                except Exception as exc:
                    logger.warning(
                        "[VectorStore] Vector search failed — falling back | error=%s", exc
                    )

            # ── 2. CONTAINS fuzzy search ──────────────────────────────────────
            if not results:
                try:
                    cur.execute(
                        f"""
                        SELECT TOP {limit}
                            VEC_TEXT,
                            VEC_META
                        FROM {table_ref}
                        WHERE CONTAINS(VEC_TEXT, ?, FUZZY(0.6))
                        """,
                        (query_text,),
                    )
                    rows = cur.fetchall()
                    if rows:
                        results = self._rows_to_dicts(rows)
                        logger.info(
                            "[VectorStore] Fuzzy search SUCCESS | returned=%d | "
                            "error_type=%s | top_titles=%s",
                            len(results), error_type,
                            [r.get("note_title", "N/A") for r in results[:3]],
                        )
                except Exception as exc:
                    logger.debug("[VectorStore] Fuzzy search failed | error=%s", exc)

            # ── 3. LIKE keyword search ────────────────────────────────────────
            if not results:
                try:
                    first_keyword = (
                        error_message.split()[0] if error_message.split() else error_type
                    )
                    cur.execute(
                        f"""
                        SELECT TOP {limit}
                            VEC_TEXT,
                            VEC_META
                        FROM {table_ref}
                        WHERE UPPER(VEC_TEXT) LIKE UPPER(?)
                           OR UPPER(VEC_TEXT) LIKE UPPER(?)
                        """,
                        (f"%{error_type}%", f"%{first_keyword}%"),
                    )
                    rows = cur.fetchall()
                    if rows:
                        results = self._rows_to_dicts(rows)
                        logger.info(
                            "[VectorStore] LIKE search SUCCESS | returned=%d | "
                            "error_type=%s | top_titles=%s",
                            len(results), error_type,
                            [r.get("note_title", "N/A") for r in results[:3]],
                        )
                except Exception as exc:
                    logger.error("[VectorStore] LIKE search failed | error=%s", exc)

            # ── 4. Fallback scan — return first N rows ────────────────────────
            if not results:
                try:
                    cur.execute(
                        f"SELECT TOP {limit} VEC_TEXT, VEC_META FROM {table_ref}"
                    )
                    rows = cur.fetchall()
                    if rows:
                        results = self._rows_to_dicts(rows)
                        logger.info(
                            "[VectorStore] Fallback scan SUCCESS | returned=%d | top_titles=%s",
                            len(results),
                            [r.get("note_title", "N/A") for r in results[:3]],
                        )
                except Exception as exc:
                    logger.error("[VectorStore] Fallback scan failed | error=%s", exc)

            cur.close()

        except Exception as exc:
            logger.error("[VectorStore] Retrieval error | error=%s", exc)
            # Mark connection as dead so next call reconnects
            self._conn = None

        if results:
            logger.info(
                "[VectorStore] Retrieval COMPLETE | returned=%d | error_type=%s | iflow=%s",
                len(results), error_type, iflow_id,
            )
        else:
            logger.warning(
                "[VectorStore] Retrieval returned NO RESULTS | error_type=%s | iflow=%s | "
                "RCA context will be limited",
                error_type, iflow_id,
            )

        return results

    # ── Row → dict ────────────────────────────────────────────────────────────

    @staticmethod
    def _rows_to_dicts(rows, include_score: bool = False) -> List[Dict]:
        """
        Convert (VEC_TEXT, VEC_META[, score]) rows to structured dicts.

        VEC_META is a JSON string with keys: title, url, source, note_id.
        """
        results = []
        for i, row in enumerate(rows):
            vec_text  = row[0] or ""
            raw_meta  = row[1] or "{}"
            score     = float(row[2]) if include_score and len(row) > 2 else 1.0

            try:
                meta: Dict = json.loads(raw_meta) if isinstance(raw_meta, str) else (raw_meta or {})
            except (json.JSONDecodeError, TypeError):
                meta = {}

            title = (
                meta.get("title")
                or meta.get("note_title")
                or meta.get("name")
                or f"SAP Note {i + 1}"
            )
            category = (
                meta.get("error_category")
                or meta.get("category")
                or meta.get("type")
                or meta.get("error_type")
                or ""
            )
            solution = (
                meta.get("solution_steps")
                or meta.get("solution")
                or meta.get("fix")
                or meta.get("resolution")
                or ""
            )
            source = meta.get("url") or meta.get("source") or ""

            results.append({
                "note_id":        meta.get("note_id") or meta.get("id") or str(i + 1),
                "note_title":     title,
                "note_content":   vec_text,
                "error_category": category,
                "solution_steps": solution,
                "source":         source,
                "meta":           meta,
                "similarity":     score,
            })
        return results

    # ── Prompt formatter ──────────────────────────────────────────────────────

    def format_notes_for_prompt(self, notes: List[Dict]) -> str:
        """
        Format SAP Notes for injection into the LLM RCA/fix prompt.
        Trims each entry to ~600 chars to keep context size manageable.
        """
        if not notes:
            return ""

        lines = ["\n\n=== RELEVANT SAP NOTES FROM KNOWLEDGE BASE ==="]
        for note in notes:
            lines.append(f"\n--- {note.get('note_title', 'SAP Note')} ---")

            if note.get("source"):
                lines.append(f"Source     : {note['source']}")

            if note.get("similarity") and note["similarity"] < 1.0:
                lines.append(f"Similarity : {note['similarity']:.4f}")

            if note.get("error_category"):
                lines.append(f"Category   : {note['error_category']}")

            content = note.get("note_content", "")
            if content:
                trimmed = content[:600].strip()
                if len(content) > 600:
                    trimmed += "…"
                lines.append(f"Content    : {trimmed}")

            if note.get("solution_steps"):
                lines.append(f"Solution   : {note['solution_steps']}")

            lines.append("-" * 60)

        return "\n".join(lines)


# ── Global singleton ──────────────────────────────────────────────────────────
_vector_store: VectorStoreRetriever | None = None


def get_vector_store() -> VectorStoreRetriever:
    """Return (or create) the global VectorStoreRetriever instance."""
    global _vector_store
    if _vector_store is None:
        _vector_store = VectorStoreRetriever()
    return _vector_store
