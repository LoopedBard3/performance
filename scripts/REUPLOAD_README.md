# Reupload Failed WorkItems - User Guide

This guide explains how to use the reupload scripts to transfer failed workitem files from source Azure Blob Storage to target storage.

## Overview

The reupload process consists of two scripts:

1. **`generate_workitem_csv.py`** - Queries Kusto to generate a CSV of failed WorkItems
2. **`reupload_failed_workitems.py`** - Processes the CSV, downloads files, and reuploads them

## Prerequisites

### Python Dependencies

Install required packages:

```bash
pip install azure-kusto-data azure-identity azure-storage-blob azure-storage-queue
```

### Authentication

Both scripts use **Azure DefaultAzureCredential** which supports:
- Azure CLI authentication (`az login`)
- Managed Identity (when running in Azure)
- Environment variables (service principal)

**Recommended**: Run `az login` before executing the scripts.

### Permissions Required

- **Kusto**: Read access to `engineeringdata` database
- **Source Storage**: Read/List permissions on blob containers
- **Target Storage**: Write permissions on `results` container and `resultsqueue` queue

---

## Step 1: Generate WorkItem CSV

Run the helper script to query Kusto and generate the CSV:

```bash
python generate_workitem_csv.py --output failed_workitems.csv
```

### Options

- `--output` - Output CSV file path (default: `failed_workitems.csv`)
- `--machines` - Space-separated list of machine names to filter (defaults to all PERFVIPER machines)

### Example with Custom Machines

```bash
python generate_workitem_csv.py \
    --output my_workitems.csv \
    --machines PERFVIPER088 PERFVIPER096
```

### Output Format

The CSV will contain two columns:
```csv
JobId,WorkItemId
abc123...,def456...
```

---

## Step 2: Run Reupload Script

Execute the main reupload script:

```bash
python reupload_failed_workitems.py --csv failed_workitems.csv
```

### Options

| Option | Description | Default |
|--------|-------------|---------|
| `--csv` | **Required** - Path to CSV file with WorkItemId and JobId | - |
| `--state-db` | Path to SQLite state database for resume capability | `reupload_state.db` |
| `--workitem-workers` | Number of parallel WorkItem workers | 20 |
| `--file-workers` | Number of parallel file workers per WorkItem | 10 |
| `--resume` | Resume from previous run (skip completed WorkItems) | False |
| `--no-queue` | Skip queuing messages after upload | False |

### Basic Usage

```bash
# First run
python reupload_failed_workitems.py --csv failed_workitems.csv

# Resume after failure/interruption
python reupload_failed_workitems.py --csv failed_workitems.csv --resume

# Lower parallelism for rate-limited scenarios
python reupload_failed_workitems.py --csv failed_workitems.csv \
    --workitem-workers 10 \
    --file-workers 5

# Skip queue messages (upload only)
python reupload_failed_workitems.py --csv failed_workitems.csv --no-queue
```

---

## How It Works

### Architecture

```
┌─────────────────────────────────────────────────────────────┐
│ 1. Load WorkItems from CSV → Add to State DB (SQLite)      │
└────────────────────────┬────────────────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────────────────┐
│ 2. Process WorkItems in Parallel (20 workers by default)   │
│    For each WorkItem:                                       │
│    ├─ Query Kusto for file list (Uri, FileName)           │
│    ├─ Update state to 'in_progress'                       │
│    └─ Process files in parallel (10 workers)              │
│       For each file:                                        │
│       ├─ Download from source blob storage                │
│       ├─ Upload to target storage (results container)     │
│       ├─ Queue message to resultsqueue                    │
│       └─ Update file status in state DB                   │
└────────────────────────┬────────────────────────────────────┘
                         ▼
┌─────────────────────────────────────────────────────────────┐
│ 3. Update WorkItem status to 'completed' or 'failed'       │
└─────────────────────────────────────────────────────────────┘
```

### State Tracking

The script maintains a SQLite database (`reupload_state.db` by default) with two tables:

**workitems** table:
- `workitem_id`, `job_id` - Identifiers
- `status` - `pending`, `in_progress`, `completed`, `failed`
- `files_total`, `files_processed` - Progress tracking
- `error_message` - Failure details
- `started_at`, `completed_at` - Timestamps

**files** table (detailed tracking):
- `workitem_id`, `job_id`, `filename` - Identifiers
- `source_uri` - Original blob URL
- `status` - `pending`, `completed`, `failed`
- `error_message` - Failure details
- `uploaded_at` - Upload timestamp

### Resume Capability

The `--resume` flag allows you to restart the script after interruption:
- Completed WorkItems are skipped
- Failed WorkItems are retried
- In-progress WorkItems are retried

### Error Handling

- **Kusto query failures**: WorkItem marked as failed, can be retried
- **Download failures**: File marked as failed, WorkItem continues
- **Upload failures**: Retried automatically (3 attempts), then marked failed
- **Blob exists**: Treated as success (idempotent)

---

## Monitoring Progress

### Real-time Logs

The script outputs detailed logs:

```
2026-01-12 22:00:00 [INFO] Processing WorkItem abc123... (Job def456...)
2026-01-12 22:00:01 [INFO] Found 350 files for WorkItem abc123...
2026-01-12 22:00:02 [INFO] Downloading report.json from source...
2026-01-12 22:00:03 [INFO] Uploading report.json to target...
2026-01-12 22:00:04 [INFO] Uploaded blob: abc123-report.json
2026-01-12 22:00:05 [INFO] Queued message for: abc123-report.json
2026-01-12 22:00:20 [INFO] Progress: 10/21000 (Completed: 8, Failed: 2)
```

### Summary Report

At completion, a summary is printed:

```
============================================================
REUPLOAD SUMMARY
============================================================
Total WorkItems:       21000
  Completed:           20800
  Failed:              200
  In Progress:         0
  Pending:             0

Total Files:           16800000
  Processed:           16640000
============================================================
```

### Querying State Database

You can inspect the state database using SQLite:

```bash
# View failed WorkItems
sqlite3 reupload_state.db "SELECT workitem_id, job_id, error_message FROM workitems WHERE status='failed'"

# Count files by status
sqlite3 reupload_state.db "SELECT status, COUNT(*) FROM files GROUP BY status"

# View failed files for a specific WorkItem
sqlite3 reupload_state.db "SELECT filename, error_message FROM files WHERE workitem_id='abc123' AND status='failed'"
```

---

## Performance Tuning

### Recommended Settings

For **21,000 WorkItems** with **~800 files each**:

```bash
# High-throughput configuration (assumes good network & storage IOPS)
python reupload_failed_workitems.py \
    --csv failed_workitems.csv \
    --workitem-workers 20 \
    --file-workers 10

# Conservative configuration (for rate-limited scenarios)
python reupload_failed_workitems.py \
    --csv failed_workitems.csv \
    --workitem-workers 10 \
    --file-workers 5
```

### Adjusting Parallelism

- **Increase `--workitem-workers`** if:
  - CPU/memory utilization is low
  - Network bandwidth is underutilized
  - Storage throttling is not occurring

- **Decrease `--workitem-workers`** if:
  - Hitting storage account throttling limits (429 errors)
  - High memory usage (128GB should be sufficient)
  - Kusto rate limits being hit

- **Adjust `--file-workers`** independently:
  - Higher = faster per-WorkItem processing
  - Lower = better for WorkItems with many large files

### Estimated Duration

Rough estimates (highly dependent on network & storage):
- **Per file**: 0.5-2 seconds (download + upload)
- **Per WorkItem** (800 files, 10 workers): 40-160 seconds
- **Total** (21k WorkItems, 20 workers): 12-72 hours

---

## Troubleshooting

### Authentication Errors

```
ClientAuthenticationError: Unable to authenticate
```

**Fix**: Run `az login` to authenticate with Azure CLI.

### Kusto Query Failures

```
KustoServiceError: Request is invalid and cannot be executed
```

**Fix**: Verify Kusto permissions and database/cluster names.

### Storage Throttling (429 Errors)

```
azure.core.exceptions.HttpResponseError: Status code 429
```

**Fix**: Reduce parallelism:
```bash
--workitem-workers 5 --file-workers 3
```

### Blob Not Found in Source

```
ResourceNotFoundError: The specified blob does not exist
```

**Cause**: Source URI is invalid or blob was deleted.
**Action**: File marked as failed; check Kusto data accuracy.

### Memory Issues

```
MemoryError
```

**Fix**: Reduce parallelism or increase swap space.

---

## Retrying Failed WorkItems

After a run completes with failures:

1. **Check the state database**:
   ```bash
   sqlite3 reupload_state.db "SELECT workitem_id, error_message FROM workitems WHERE status='failed'"
   ```

2. **Re-run with `--resume`**:
   ```bash
   python reupload_failed_workitems.py --csv failed_workitems.csv --resume
   ```

3. **Manually create a CSV of failures** (if needed):
   ```bash
   sqlite3 -header -csv reupload_state.db \
       "SELECT job_id AS JobId, workitem_id AS WorkItemId FROM workitems WHERE status='failed'" \
       > failed_only.csv
   
   python reupload_failed_workitems.py --csv failed_only.csv --state-db retry_state.db
   ```

---

## Advanced: Manual Intervention

### Skip Specific WorkItems

Edit the state database:

```bash
sqlite3 reupload_state.db "UPDATE workitems SET status='completed' WHERE workitem_id='skip_this_one'"
```

### Reset a Failed WorkItem

```bash
sqlite3 reupload_state.db "UPDATE workitems SET status='pending', error_message=NULL WHERE workitem_id='retry_this_one'"
```

### Export All File URIs

```bash
sqlite3 -header -csv reupload_state.db \
    "SELECT workitem_id, filename, source_uri FROM files" \
    > all_files.csv
```

---

## Safety & Idempotency

The script is designed to be **safe and idempotent**:

- ✅ **No data loss**: Downloads before upload; both source and target preserved
- ✅ **Duplicate safety**: Existing blobs are not overwritten (ResourceExistsError ignored)
- ✅ **Resume friendly**: State database tracks all progress
- ✅ **Parallel safe**: SQLite handles concurrent writes safely

**Note**: If a blob already exists in the target, the script treats it as success and moves on.

---

## Example Complete Workflow

```bash
# Step 1: Install dependencies
pip install azure-kusto-data azure-identity azure-storage-blob azure-storage-queue

# Step 2: Authenticate
az login

# Step 3: Generate CSV from Kusto
python generate_workitem_csv.py --output failed_workitems.csv

# Step 4: Run reupload (initial run)
python reupload_failed_workitems.py --csv failed_workitems.csv

# Step 5: If interrupted or failed, resume
python reupload_failed_workitems.py --csv failed_workitems.csv --resume

# Step 6: Check for failures
sqlite3 reupload_state.db "SELECT COUNT(*) FROM workitems WHERE status='failed'"

# Step 7: Retry failures only
sqlite3 -header -csv reupload_state.db \
    "SELECT job_id AS JobId, workitem_id AS WorkItemId FROM workitems WHERE status='failed'" \
    > retry.csv

python reupload_failed_workitems.py --csv retry.csv --state-db retry_state.db
```

---

## Support & Debugging

### Enable Verbose Logging

Edit the script and change logging level:

```python
basicConfig(
    level=DEBUG,  # Changed from INFO
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
```

### Export Logs to File

```bash
python reupload_failed_workitems.py --csv failed_workitems.csv 2>&1 | tee reupload.log
```

### State Database Schema

```sql
-- View schema
sqlite3 reupload_state.db ".schema"

-- Aggregate statistics
SELECT 
    status,
    COUNT(*) as count,
    SUM(files_total) as total_files,
    SUM(files_processed) as processed_files
FROM workitems
GROUP BY status;
```

---

## Notes

- The script uses the same blob naming convention as production: `{WorkItemId}-{FileName}`
- All uploads use `application/json` content type
- Retry logic uses exponential backoff (built into `retry_on_exception`)
- Queue messages are sent only after successful upload
- Source blobs are **not** deleted (read-only operation)

---

## Questions or Issues?

If you encounter issues:
1. Check the state database for error messages
2. Verify Azure authentication and permissions
3. Review logs for specific error patterns
4. Adjust parallelism settings based on resource constraints
