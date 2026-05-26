# Dashboard API

**Router prefix:** `/dashboard`  
**File:** `smart_monitoring_dashboard.py`

Aggregated data endpoints used by the frontend dashboard. All routes are read-only `GET` unless noted.

---

## Aggregate

### `GET /dashboard/all`

Single call that returns all dashboard data in one payload: KPIs, status breakdown, error distribution, top iFlows, failure timeline (last 72 hours), and Event Mesh status.

### `GET /dashboard/test`

Connectivity smoke-test. Returns `{ "status": "ok" }`.

---

## KPIs & Distributions

### `GET /dashboard/kpi-cards`

Key performance indicator cards: total failed messages, total incidents, in-progress, fix-failed, auto-fixed, pending approval, auto-fix rate, avg resolution time, RCA coverage.

### `GET /dashboard/error-distribution`

Breakdown of incidents grouped by `error_type`, sorted by count descending.

### `GET /dashboard/status-distribution`

Breakdown of incidents grouped by `status`, sorted by count descending.

### `GET /dashboard/status-breakdown`

Same as `status-distribution` with additional percentage fields.

---

## Charts

### `GET /dashboard/failures-over-time`

Time-series failure counts bucketed by interval over the last 72 hours.

**Query params:**

| Param | Values | Default |
|---|---|---|
| `interval` | `hour`, `day`, `week`, `month` | `hour` |
| `time_range` | `1h`, `24h`, `7d`, `30d` | `24h` |

**Response:**
```json
{ "interval": "hour", "time_range": "24h", "series": [{ "time": "2026-05-20T10:00", "count": 5 }], "total_failures": 42 }
```

### `GET /dashboard/top-failing-iflows`

Top iFlows ranked by failure count.

### `GET /dashboard/sender-receiver-stats`

Breakdown of failures by sender and receiver adapter type.

---

## Incident Tables

### `GET /dashboard/active-incidents-table`

Incidents currently in an active status (`DETECTED`, `RCA_IN_PROGRESS`, `RCA_COMPLETE`, `AWAITING_APPROVAL`, `AWAITING_HUMAN_REVIEW`, `FIX_IN_PROGRESS`, `FIX_DEPLOYED`).

### `GET /dashboard/incidents/paginated`

Paginated, filterable incident list.

**Query params:** `page`, `page_size`, `status`, `tab` (`active` | `resolved` | `failed` | `all`)

### `GET /dashboard/recent-failures-table`

Most recent failed CPI messages, ordered by `LogEnd` descending.

### `GET /dashboard/fix-progress-tracker`

List of incidents currently undergoing a fix, with their live progress percentage.

---

## Leaderboards

### `GET /dashboard/leaderboard/noisy-integrations`

iFlows ranked by total failure count (most problematic first).

### `GET /dashboard/leaderboard/recurring-incidents`

iFlows ranked by number of distinct recurring incident cycles.

### `GET /dashboard/leaderboard/longest-open`

Incidents ranked by age (longest unresolved first).

---

## Drill-Down

### `GET /dashboard/drill-down/message/{message_guid}`

All incidents and history associated with a specific CPI message GUID.

### `GET /dashboard/drill-down/incident/{incident_id}`

Full detail for a specific incident including timeline, fix steps, and related messages.

### `GET /dashboard/drill-down/iflow/{iflow_name}`

Incident history, failure trend, and fix success rate for a specific iFlow.

---

## Health & SLA

### `GET /dashboard/health-metrics`

System health indicators: fix success rate, RCA coverage, average time-to-fix, escalation rate.

### `GET /dashboard/sla-metrics`

SLA compliance metrics: incidents resolved within SLA window vs breached.

### `GET /dashboard/rca-coverage`

Percentage of incidents that have completed RCA, broken down by status and error type.
