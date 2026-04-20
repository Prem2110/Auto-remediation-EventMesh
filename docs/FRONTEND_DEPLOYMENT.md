# Frontend Deployment Guide — SAP BTP Cloud Foundry

> **Stack:** React 18 + Vite + SAP UI5 Web Components  
> **Target:** SAP BTP Cloud Foundry (nginx_buildpack)  
> **Backend app name:** `sap-cpi-self-healing-agent` (already deployed via root `manifest.yml`)

---

## Overview

The frontend is a Vite-built static SPA. It needs to be:
1. Built into a `dist/` folder
2. Served by nginx on CF (for SPA client-side routing)
3. Configured to call the deployed backend URL instead of `localhost`

---

## Prerequisites

| Requirement | Check |
|---|---|
| Node.js `>=18` | `node -v` |
| npm or npx | `npm -v` |
| CF CLI installed | `cf -v` |
| Logged in to BTP CF space | `cf target` |
| Python backend already deployed | `cf apps` → `sap-cpi-self-healing-agent` |

---

## Step 1 — Get the Backend URL

After the Python backend is deployed, get its route:

```bash
cf app sap-cpi-self-healing-agent
```

Note the **routes** value. It will look like:

```
sap-cpi-self-healing-agent.cfapps.<region>.hana.ondemand.com
```

You will use this in Step 3.

---

## Step 2 — Create `frontend/nginx.conf`

Create this file at `frontend/nginx.conf` (same folder as `package.json`):

```nginx
worker_processes auto;
daemon off;

error_log stderr;

events { worker_connections 1024; }

http {
  include mime.types;
  default_type application/octet-stream;
  sendfile on;

  server {
    listen {{port}};

    root /home/vcap/app;

    # Gzip compression
    gzip on;
    gzip_types text/plain text/css application/javascript application/json;

    # Long-term cache for hashed JS/CSS chunks (Vite adds content hashes)
    location ~* \.(js|css|woff2?|png|svg|ico)$ {
      expires 1y;
      add_header Cache-Control "public, immutable";
    }

    # SPA fallback — all routes go to index.html (react-router handles them)
    location / {
      try_files $uri $uri/ /index.html;
    }
  }
}
```

> **Why nginx and not staticfile_buildpack?**  
> The app uses `react-router-dom`. Without the `try_files` fallback, refreshing on any
> route like `/pipeline` returns a 404 from the file server.

---

## Step 3 — Create `frontend/manifest.yaml`

Create this file at `frontend/manifest.yaml`:

```yaml
applications:
  - name: orbit-integration-suite-ui
    memory: 64M
    disk_quota: 256M
    instances: 1
    buildpacks:
      - nginx_buildpack
    path: dist
    env:
      VITE_API_BASE_URL: https://sap-cpi-self-healing-agent.cfapps.<region>.hana.ondemand.com
```

Replace `<region>` with your actual BTP region (e.g. `eu10`, `us10`, `ap10`).

---

## Step 4 — Configure the API Base URL in the Frontend

### 4a. Update `vite.config.ts`

In [frontend/vite.config.ts](../frontend/vite.config.ts), the build already outputs to `dist/`.
No change needed for the build output — Vite automatically injects `import.meta.env.VITE_*`
variables at build time from the environment.

### 4b. Update `frontend/src/services/api.ts`

Find where the API base URL is set and ensure it reads from the env variable:

```typescript
// Change from:
const _BASE = "http://localhost:8080";

// Change to:
const _BASE = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8080";
```

This means:
- **Local dev** (`npm run dev`): falls back to `localhost:8080`  
- **Production CF build**: uses the injected backend URL from `manifest.yaml`

---

## Step 5 — Verify CORS on the Backend

The Python backend (`main.py`) currently uses:

```python
allow_origins=["*"]
```

This works for development. For production, it is best practice to restrict it to the
frontend's CF URL. Update in `main.py` when you are ready:

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",  # local dev
        "https://orbit-integration-suite-ui.cfapps.<region>.hana.ondemand.com",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

> For now, `allow_origins=["*"]` will work fine. Lock it down before going to production.

---

## Step 6 — Build the Frontend

```bash
cd frontend
npm install          # only needed first time or after dependency changes
npm run build        # runs: tsc --noEmit && vite build
```

Expected output:
```
dist/index.html
dist/assets/react-vendor-<hash>.js
dist/assets/ui5-core-<hash>.js
dist/assets/ui5-fiori-<hash>.js
dist/assets/recharts-<hash>.js
dist/assets/query-<hash>.js
dist/assets/index-<hash>.js
dist/assets/index-<hash>.css
```

No chunk should exceed **600 KB** (Vite will warn if it does).

---

## Step 7 — Deploy to Cloud Foundry

```bash
cd frontend          # must be inside the frontend folder
cf push              # reads manifest.yaml, pushes dist/
```

Watch the staging logs:

```bash
cf logs orbit-integration-suite-ui --recent
```

---

## Step 8 — Verify the Deployment

```bash
cf app orbit-integration-suite-ui
```

Check the **routes** line, then open the URL in a browser:

```
https://orbit-integration-suite-ui.cfapps.<region>.hana.ondemand.com
```

Test these scenarios:
- [ ] App loads without a blank screen
- [ ] Navigating to `/pipeline` and refreshing does **not** give a 404
- [ ] API calls reach the backend (check the browser Network tab)
- [ ] No CORS errors in the browser console

---

## Full File Structure After Setup

```
frontend/
├── src/
├── dist/               ← generated by npm run build (pushed to CF)
├── manifest.yaml       ← NEW: CF deployment config
├── nginx.conf          ← NEW: SPA routing config
├── vite.config.ts      ← unchanged (already has manualChunks)
├── package.json
└── tsconfig.json
```

---

## Local Development (unchanged)

Nothing changes for local dev. The Vite proxy in `vite.config.ts` still handles
`/api/*` → `localhost:8080`.

```bash
cd frontend
npm run dev            # starts on http://localhost:3000
```

---

## Redeployment (after code changes)

```bash
cd frontend
npm run build
cf push
```

That's it — CF replaces the running instance with the new `dist/` content.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Blank page after deploy | `nginx.conf` missing | Check `frontend/nginx.conf` exists |
| 404 on page refresh | Missing `try_files` fallback | Ensure nginx.conf has `try_files $uri $uri/ /index.html` |
| CORS errors in browser console | Backend CORS not allowing UI URL | Add frontend CF URL to `allow_origins` in `main.py` |
| API calls going to `localhost` | `VITE_API_BASE_URL` not set | Check `manifest.yaml` env section |
| Chunk size warning during build | Large dependency not split | Add to `manualChunks` in `vite.config.ts` |
| Icons not showing | Missing icon import | Add the specific `@ui5/webcomponents-icons/dist/<name>.js` to `main.tsx` |
