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

## Security Checklist

- [ ] All secrets in environment variables — `.env` is not committed to git
- [ ] MCP server connections use `verify=True` (SSL)
- [ ] `AUTONOMOUS_ENABLED` is explicitly set (not accidentally left `true` in dev)
- [ ] `AUTO_FIX_CONFIDENCE` set to `0.90` or higher for production
- [ ] Log level set to `INFO` (not `DEBUG`) in production to avoid leaking payloads
- [ ] `.env.example` committed; `.env` in `.gitignore`

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
