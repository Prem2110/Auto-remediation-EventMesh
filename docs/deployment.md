# Deployment

## Platforms

The application is stateless (all state in HANA + in-memory caches) and can be deployed on:

- **SAP Cloud Foundry** (recommended for BTP)
- **Kubernetes** (e.g., Kyma on BTP)
- **Local / Docker** (development)

---

## SAP Cloud Foundry

### manifest.yml

```yaml
applications:
  - name: cpi-self-healing-agent
    memory: 1G
    disk_quota: 2G
    buildpacks:
      - python_buildpack
    command: uvicorn main_v2:app --host 0.0.0.0 --port $PORT
    health-check-type: http
    health-check-http-endpoint: /health
    env:
      PYTHONPATH: .
      # All secrets via CF environment variables, not .env
```

### Set Environment Variables

```bash
cf set-env cpi-self-healing-agent AICORE_CLIENT_ID "..."
cf set-env cpi-self-healing-agent AICORE_CLIENT_SECRET "..."
# ... repeat for all required variables
cf restage cpi-self-healing-agent
```

### Deploy

```bash
cf push
```

---

## Docker

### Dockerfile

```dockerfile
FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml .
RUN pip install uv && uv sync --no-dev

COPY . .
EXPOSE 8080

CMD ["uvicorn", "main_v2:app", "--host", "0.0.0.0", "--port", "8080"]
```

### Run

```bash
docker build -t cpi-self-healing-agent .
docker run -p 8080:8080 --env-file .env cpi-self-healing-agent
```

---

## Kubernetes (Kyma)

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cpi-self-healing-agent
spec:
  replicas: 1
  template:
    spec:
      containers:
        - name: app
          image: <registry>/cpi-self-healing-agent:latest
          ports:
            - containerPort: 8080
          envFrom:
            - secretRef:
                name: cpi-agent-secrets
          livenessProbe:
            httpGet:
              path: /health
              port: 8080
          readinessProbe:
            httpGet:
              path: /health
              port: 8080
```

---

## Scaling

The application is horizontally scalable with one caveat: the in-memory `FIX_PROGRESS` dict in `core/state.py` is not shared across instances. If you need `GET /fix-progress/{id}` to work across replicas, replace `FIX_PROGRESS` with a HANA table or Redis.

For most deployments, a single instance with sufficient memory is sufficient.

---

## Observability

### Structured Logging

All logs are emitted in structured JSON via `utils/logger_config.py`. Every log line includes:

```json
{
  "timestamp": "2026-04-09T10:00:00Z",
  "level": "INFO",
  "component": "fix_agent",
  "correlation_id": "uuid",
  "user_id": "user123",
  "message": "Fix applied successfully"
}
```

Log files rotate automatically. Set `LOG_LEVEL` and `ENABLE_CONSOLE_LOGS` in `.env`.

### LangSmith Tracing

Set these variables to enable full LLM call tracing:

```env
LANGSMITH_API_KEY=...
LANGSMITH_PROJECT=cpi-self-healing
LANGSMITH_TRACING=true
```

### Prometheus Metrics

A `/metrics` endpoint is available if `prometheus-client` is installed. Scrape it with your Prometheus instance.

---

## Runtime Settings

The application ships with a live settings system that lets operators tune all key constants without restarting or redeploying.

### What is auto-created on startup

On every startup the application calls `ensure_settings_schema()`, which creates the `EM_RUNTIME_SETTINGS` HANA table if it does not exist:

```sql
CREATE TABLE "EM_RUNTIME_SETTINGS" (
    SETTING_KEY   NVARCHAR(100) PRIMARY KEY,
    SETTING_VALUE NCLOB,
    DATA_TYPE     NVARCHAR(20),
    UPDATED_AT    NVARCHAR(64),
    UPDATED_BY    NVARCHAR(200)
)
```

No manual migration is needed. The table name can be overridden with `HANA_TABLE_EM_SETTINGS` environment variable.

### Configuring settings after deployment

Use the UI settings page (`/settings`) or the REST API directly:

```bash
# View all settings
curl https://<backend-url>/settings

# Update the circuit breaker threshold
curl -X PATCH https://<backend-url>/settings \
  -H "Content-Type: application/json" \
  -d '{"MAX_CONSECUTIVE_FAILURES": 3}'

# Turn off autonomous fixing immediately (takes effect at once, no restart)
curl -X PATCH https://<backend-url>/settings \
  -H "Content-Type: application/json" \
  -d '{"AUTO_FIX_ALL_CPI_ERRORS": false}'

# Reset a setting to its compiled default
curl -X DELETE https://<backend-url>/settings/MAX_CONSECUTIVE_FAILURES
```

### Settings that do NOT require environment variables

The following values were previously only configurable via `.env` restage. They can now be changed live:

| Setting key | Previous mechanism | Now |
|---|---|---|
| `AUTO_FIX_CONFIDENCE` | `.env` / CF env var | UI / API (live) |
| `SUGGEST_FIX_CONFIDENCE` | `.env` / CF env var | UI / API (live) |
| `AUTO_FIX_ALL_CPI_ERRORS` | `.env` / CF env var | UI / API (**immediate**) |
| `AUTO_DEPLOY_AFTER_FIX` | `.env` / CF env var | UI / API (live) |
| `MAX_CONSECUTIVE_FAILURES` | `.env` / CF env var | UI / API (live) |
| `BURST_DEDUP_WINDOW_SECONDS` | `.env` / CF env var | UI / API (live) |
| `PENDING_APPROVAL_TIMEOUT_HRS` | `.env` / CF env var | UI / API (live) |
| `REMEDIATION_POLICIES` | code change + redeploy | UI / API (live) |

Environment variables in `.env` or CF still work and serve as the compiled default. DB overrides take precedence at runtime.

---

## Security Checklist

- [ ] All secrets in environment variables — `.env` is not committed to git
- [ ] MCP server connections use `verify=True` (SSL)
- [ ] `AUTONOMOUS_ENABLED` is explicitly set (not accidentally left `true` in dev)
- [ ] `AUTO_FIX_CONFIDENCE` set to `0.90` or higher for production (via UI Settings or env var)
- [ ] Log level set to `INFO` (not `DEBUG`) in production to avoid leaking payloads
- [ ] `.env.example` committed; `.env` in `.gitignore`
- [ ] Access to `PATCH /settings` and `DELETE /settings/{key}` restricted to authorised operators (add auth middleware if the app is publicly reachable)

---

## Production Startup Command

```bash
uvicorn main_v2:app \
  --host 0.0.0.0 \
  --port 8080 \
  --workers 1 \
  --log-level info \
  --no-access-log
```

!!! note "Single Worker"
    Use `--workers 1` unless you replace the in-memory `FIX_PROGRESS` state with a shared store. Multiple workers will cause `/fix-progress` to return stale data for requests routed to the wrong worker.
