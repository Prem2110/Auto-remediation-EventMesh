# Installation

## 1. Clone the Repository

```bash
git clone <repository-url>
cd auto-remediation
```

---

## 2. Install Dependencies

Using `uv` (recommended):

```bash
uv sync
```

Using `pip`:

```bash
pip install -r requirements.txt
```

---

## 3. Configure Environment

Copy the example env file and fill in your values:

```bash
cp .env.example .env
```

See [Configuration](configuration.md) for a full description of every variable.

---

## 4. (Optional) Scrape SAP Notes

If you want the vector store populated with SAP documentation for better RCA:

```bash
# Install Playwright browsers (first time only)
playwright install chromium

# Scrape SAP Notes (requires SAP_USERNAME / SAP_PASSWORD in .env)
python scrape_sap_docs.py

# Vectorize into HANA
python vectorize_docs.py
```

---

## 5. Start the Server

```bash
uvicorn main_v2:app --host 0.0.0.0 --port 8080 --reload
```

The API will be available at `http://localhost:8080`.

Interactive docs: `http://localhost:8080/docs`

---

## 6. Verify the Installation

```bash
# Health check
curl http://localhost:8080/health

# Run a test query
curl -X POST http://localhost:8080/query \
  -H "Content-Type: application/json" \
  -d '{"query": "List available MCP tools", "user_id": "test"}'
```

---

## Autonomous Mode

To enable background polling and auto-remediation, set in `.env`:

```env
AUTONOMOUS_ENABLED=true
POLL_INTERVAL_SECONDS=120
AUTO_FIX_CONFIDENCE=0.90
```

The autonomous loop starts automatically on server startup when `AUTONOMOUS_ENABLED=true`.

You can also toggle it at runtime without restarting:

```bash
# Enable
curl -X POST http://localhost:8080/autonomous/enable

# Disable
curl -X POST http://localhost:8080/autonomous/disable

# Check status
curl http://localhost:8080/autonomous/status
```

---

## Local Development (SQLite)

For local development without a HANA instance, the database layer automatically falls back to SQLite when `HANA_HOST` is not set. No extra configuration is needed.

!!! warning
    SQLite mode does not support vector search. RCA will still work but will skip the SAP Notes semantic search step.
