# Tickets and Approvals System

## Overview

The Auto-Remediation system now includes a comprehensive tickets and approvals workflow to track iFlow errors and manage human-in-the-loop approvals for automated fixes.

## Features

### 1. Tickets Tab

The Tickets tab displays all tickets created for iFlow errors. Each ticket includes:

- **Ticket ID**: Unique identifier for the ticket
- **iFlow Name**: The name of the affected iFlow
- **Error Message**: Detailed error information
- **Severity**: Priority level (high, medium, low)
- **Status**: Current state (open, in_progress, resolved)
- **Assigned To**: Team member responsible for the ticket
- **Resolution Notes**: Details about how the issue was resolved
- **Timestamps**: Creation and last update times

#### Ticket Severity Levels

- **High**: Critical issues requiring immediate attention
- **Medium**: Important issues that should be addressed soon
- **Low**: Minor issues that can be addressed during regular maintenance

#### Ticket Status

- **Open**: New ticket awaiting assignment
- **In Progress**: Ticket is being actively worked on
- **Resolved**: Issue has been fixed and verified

### 2. Approvals Tab

The Approvals tab manages human-in-the-loop approvals for automated fixes. Each approval request includes:

- **Approval ID**: Unique identifier
- **iFlow Name**: The affected iFlow
- **Fix Description**: Summary of the proposed fix
- **Fix Type**: Category of the fix (configuration, schema, security, code)
- **Status**: Current state (pending, approved, rejected)
- **Requested By**: Who/what requested the approval (usually the auto-remediation agent)
- **Approved/Rejected By**: Human approver
- **Approval Notes**: Comments from the approver
- **Fix Details**: Technical details of the proposed fix (JSON format)
- **Timestamps**: Creation and last update times

#### Fix Types

- **Configuration**: Changes to iFlow configuration parameters
- **Schema**: Updates to XML schemas or data structures
- **Security**: Security-related changes (credentials, certificates, etc.)
- **Code**: Changes to iFlow logic or transformations

#### Approval Actions

For pending approvals, users can:

- **Approve**: Grant permission to apply the fix
- **Reject**: Deny the fix request with optional notes

## API Endpoints

### Tickets

#### GET /api/tickets
Retrieve all tickets

**Response:**
```json
{
  "tickets": [
    {
      "id": 1,
      "iflow_id": 1,
      "iflow_name": "PaymentProcessing_IFlow",
      "error_message": "Connection timeout",
      "severity": "high",
      "status": "open",
      "created_at": "2026-04-14T10:30:00",
      "updated_at": "2026-04-14T10:30:00",
      "assigned_to": null,
      "resolution_notes": null
    }
  ]
}
```

### Approvals

#### GET /api/approvals
Retrieve all approval requests

**Response:**
```json
{
  "approvals": [
    {
      "id": 1,
      "iflow_id": 1,
      "iflow_name": "PaymentProcessing_IFlow",
      "fix_description": "Increase connection timeout",
      "fix_type": "configuration",
      "status": "pending",
      "created_at": "2026-04-14T10:30:00",
      "updated_at": "2026-04-14T10:30:00",
      "requested_by": "auto-remediation-agent",
      "approved_by": null,
      "approval_notes": null,
      "fix_details": "{\"timeout\": 60}"
    }
  ]
}
```

#### POST /api/approvals/{approval_id}/approve
Approve a pending fix

**Request Body:**
```json
{
  "approved_by": "user@company.com",
  "notes": "Approved after review"
}
```

**Response:**
```json
{
  "message": "Approval granted successfully"
}
```

#### POST /api/approvals/{approval_id}/reject
Reject a pending fix

**Request Body:**
```json
{
  "rejected_by": "user@company.com",
  "notes": "Needs more testing"
}
```

**Response:**
```json
{
  "message": "Approval rejected"
}
```

## Database Schema

### Tickets Table

```sql
CREATE TABLE tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    iflow_id INTEGER,
    iflow_name TEXT NOT NULL,
    error_message TEXT,
    severity TEXT DEFAULT 'medium',
    status TEXT DEFAULT 'open',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    assigned_to TEXT,
    resolution_notes TEXT,
    FOREIGN KEY (iflow_id) REFERENCES iflows(id)
);
```

### Approvals Table

```sql
CREATE TABLE approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    iflow_id INTEGER,
    iflow_name TEXT NOT NULL,
    fix_description TEXT,
    fix_type TEXT,
    status TEXT DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    requested_by TEXT,
    approved_by TEXT,
    approval_notes TEXT,
    fix_details TEXT,
    FOREIGN KEY (iflow_id) REFERENCES iflows(id)
);
```

## Usage Examples

### Creating a Ticket (from Python code)

```python
import sqlite3
from datetime import datetime

conn = sqlite3.connect('auto_remediation.db')
cursor = conn.cursor()

cursor.execute("""
    INSERT INTO tickets 
    (iflow_id, iflow_name, error_message, severity, status)
    VALUES (?, ?, ?, ?, ?)
""", (1, 'MyIFlow', 'Connection timeout', 'high', 'open'))

conn.commit()
conn.close()
```

### Creating an Approval Request (from Python code)

```python
import sqlite3
import json

conn = sqlite3.connect('auto_remediation.db')
cursor = conn.cursor()

fix_details = json.dumps({
    "timeout": 60,
    "retry_count": 3
})

cursor.execute("""
    INSERT INTO approvals 
    (iflow_id, iflow_name, fix_description, fix_type, status, requested_by, fix_details)
    VALUES (?, ?, ?, ?, ?, ?, ?)
""", (1, 'MyIFlow', 'Increase timeout', 'configuration', 'pending', 'auto-agent', fix_details))

conn.commit()
conn.close()
```

## Integration with Auto-Remediation Workflow

1. **Error Detection**: When an iFlow error is detected, a ticket is automatically created
2. **Fix Generation**: The RCA and Fix agents analyze the error and generate a fix
3. **Approval Request**: If the fix requires human approval, an approval request is created
4. **Human Review**: Users review the approval request in the UI
5. **Approval/Rejection**: Users approve or reject the fix
6. **Fix Application**: If approved, the fix is applied to the iFlow
7. **Ticket Resolution**: Once fixed, the ticket is updated to "resolved" status

## Best Practices

1. **Ticket Management**
   - Assign tickets promptly to appropriate team members
   - Update ticket status as work progresses
   - Add detailed resolution notes for future reference

2. **Approval Workflow**
   - Review fix details carefully before approving
   - Add meaningful notes when rejecting fixes
   - Test approved fixes in staging before production

3. **Monitoring**
   - Regularly check for pending approvals
   - Monitor ticket trends to identify recurring issues
   - Use severity levels to prioritize work

## Future Enhancements

- Email notifications for new tickets and approvals
- Ticket assignment automation based on iFlow ownership
- Approval workflow with multiple reviewers
- Integration with external ticketing systems (JIRA, ServiceNow)
- Analytics dashboard for ticket and approval metrics