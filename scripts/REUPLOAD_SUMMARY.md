# Reupload Failed WorkItems - Complete Solution

## 📦 What's Included

This solution provides a complete, production-ready system for reuploading failed workitem files from source Azure Blob Storage to target storage.

### Scripts

| Script | Purpose |
|--------|---------|
| **generate_workitem_csv.py** | Query Kusto to generate CSV of failed WorkItems |
| **reupload_failed_workitems.py** | Main reupload script (download & upload) |
| **monitor_reupload.py** | Real-time progress monitoring dashboard |
| **validate_uploads.py** | Verify uploaded files exist in target storage |

### Documentation

| Document | Purpose |
|----------|---------|
| **REUPLOAD_QUICKSTART.md** | 5-minute quick start guide |
| **REUPLOAD_README.md** | Comprehensive user manual |
| **REUPLOAD_SUMMARY.md** | This file - overview and design |

---

## 🎯 Design Goals Achieved

✅ **Parallel Processing**: 20 WorkItems × 10 files = 200 parallel operations  
✅ **State Tracking**: SQLite database with resume capability  
✅ **Error Handling**: Retry logic, graceful failures, detailed error tracking  
✅ **Scalability**: Handles 21k WorkItems × ~800 files = ~16.8M files  
✅ **Memory Efficient**: Streaming downloads, no temp files  
✅ **Idempotent**: Safe to re-run, existing blobs not overwritten  
✅ **Monitorable**: Real-time dashboard and state queries  
✅ **Resumable**: Interrupt and continue from last checkpoint  

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                      INPUT: CSV (WorkItemId, JobId)                 │
└──────────────────────────┬──────────────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    STATE TRACKER (SQLite)                           │
│  - Tracks WorkItem status (pending/in_progress/completed/failed)   │
│  - Tracks individual file status                                   │
│  - Enables resume capability                                       │
└──────────────────────────┬──────────────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│              WORKITEM PROCESSOR (20 parallel workers)               │
│                                                                     │
│  For each WorkItem:                                                │
│  1. Query Kusto for file list (Uri, FileName)                     │
│  2. Download files from source blob storage                       │
│  3. Upload to target (pvscmdupload) using existing upload.py      │
│  4. Queue message to resultsqueue                                 │
│  5. Update state database                                         │
│                                                                     │
│  ┌───────────────────────────────────────────────────────┐        │
│  │  FILE PROCESSOR (10 parallel workers per WorkItem)   │        │
│  │  - Streams files (no disk I/O)                       │        │
│  │  - Retries on transient failures (3 attempts)        │        │
│  │  - Tracks each file individually                     │        │
│  └───────────────────────────────────────────────────────┘        │
└──────────────────────────┬──────────────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    OUTPUT: Uploaded Files                           │
│  - Target: pvscmdupload (results container)                        │
│  - Queue: resultsqueue                                             │
│  - Naming: {WorkItemId}-{FileName}                                 │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 🔄 Workflow

### Phase 1: Preparation

```bash
# Install dependencies
pip install azure-kusto-data azure-identity azure-storage-blob azure-storage-queue

# Authenticate
az login

# Generate CSV from Kusto
python generate_workitem_csv.py --output failed_workitems.csv
```

### Phase 2: Execution

```bash
# Terminal 1: Start reupload
python reupload_failed_workitems.py --csv failed_workitems.csv

# Terminal 2: Monitor progress
python monitor_reupload.py --refresh 10
```

### Phase 3: Validation

```bash
# Quick sample check (100 random files)
python validate_uploads.py --sample 100

# Full validation (all files)
python validate_uploads.py

# Export missing files for retry
python validate_uploads.py --export-missing missing_files.csv
```

### Phase 4: Retry (if needed)

```bash
# Resume interrupted run
python reupload_failed_workitems.py --csv failed_workitems.csv --resume

# Retry only failed WorkItems
sqlite3 -header -csv reupload_state.db \
    "SELECT job_id AS JobId, workitem_id AS WorkItemId FROM workitems WHERE status='failed'" \
    > retry.csv
python reupload_failed_workitems.py --csv retry.csv --state-db retry_state.db
```

---

## 📊 Expected Performance

### Scale
- **WorkItems**: 21,000
- **Files**: ~16,800,000 (average 800 per WorkItem)
- **File Size**: Mostly < 5MB, many smaller

### Throughput (estimated)
- **Per File**: 0.5-2 seconds (network dependent)
- **Per WorkItem**: 40-160 seconds (800 files, 10 workers)
- **Total Runtime**: 12-72 hours (20 WorkItem workers)

### Parallelism
- **Level 1**: 20 WorkItems processed simultaneously
- **Level 2**: 10 files per WorkItem downloaded/uploaded simultaneously
- **Total Concurrency**: Up to 200 parallel file operations

### Resource Usage
- **Memory**: ~2-4GB typical, well within 128GB available
- **Network**: High bandwidth utilization (optimize for available)
- **Storage**: No local disk required (streaming)

---

## 🔒 Safety Features

### Data Protection
- **Read-Only Source**: Original files never modified or deleted
- **No Overwrites**: Existing target blobs are preserved (ResourceExistsError ignored)
- **Transactional**: Files not queued if upload fails

### Error Recovery
- **Automatic Retries**: 3 attempts with exponential backoff
- **State Persistence**: Every operation logged to SQLite
- **Resume Capability**: Continue from last checkpoint after interruption
- **Isolated Failures**: One WorkItem failure doesn't affect others

### Idempotency
- Safe to run multiple times
- Completed items automatically skipped with `--resume`
- Existing blobs treated as success

---

## 🔍 Monitoring & Debugging

### Real-Time Monitoring

```bash
# Live dashboard (refreshes every 10 seconds)
python monitor_reupload.py

# One-time snapshot
python monitor_reupload.py --once
```

Dashboard shows:
- WorkItem status (completed/failed/in_progress/pending)
- File processing progress
- Throughput (WorkItems/min, Files/min)
- Estimated time to completion
- Top error messages

### State Database Queries

```bash
# Overall progress
sqlite3 reupload_state.db "SELECT status, COUNT(*) FROM workitems GROUP BY status"

# Failed WorkItems with errors
sqlite3 reupload_state.db "SELECT workitem_id, error_message FROM workitems WHERE status='failed'"

# File-level details
sqlite3 reupload_state.db "SELECT filename, status, error_message FROM files WHERE workitem_id='abc123'"

# Total files processed
sqlite3 reupload_state.db "SELECT SUM(files_processed) FROM workitems"
```

### Logs

```bash
# Capture all output to file
python reupload_failed_workitems.py --csv failed_workitems.csv 2>&1 | tee reupload.log
```

---

## ⚙️ Configuration Options

### Tuning Parallelism

```bash
# High-throughput (good network, no rate limits)
python reupload_failed_workitems.py \
    --csv failed_workitems.csv \
    --workitem-workers 30 \
    --file-workers 15

# Conservative (rate-limited, shared resources)
python reupload_failed_workitems.py \
    --csv failed_workitems.csv \
    --workitem-workers 10 \
    --file-workers 5

# Default (balanced)
python reupload_failed_workitems.py \
    --csv failed_workitems.csv
    # Uses 20 WorkItem workers, 10 file workers
```

### Other Options

```bash
# Skip queue messages (upload only, no queue)
--no-queue

# Custom state database location
--state-db /path/to/custom_state.db

# Resume from previous run
--resume
```

---

## 🛠️ Troubleshooting

### Common Issues

| Issue | Cause | Solution |
|-------|-------|----------|
| Authentication error | Not logged into Azure | Run `az login` |
| 429 throttling | Too much parallelism | Reduce `--workitem-workers` |
| Kusto timeout | Query too large | Process in batches |
| Memory error | Too many workers | Reduce parallelism |
| Blob not found | Source deleted | File marked as failed, check Kusto |

### Recovery Strategies

1. **Interrupted Run**: Use `--resume` flag
2. **Persistent Failures**: Export failed items, retry with new state DB
3. **Rate Limiting**: Reduce parallelism and retry
4. **Partial Completion**: Validate with `validate_uploads.py`, retry missing

---

## 📈 Success Metrics

### Completion Criteria
- ✅ All WorkItems status = `completed` or `failed`
- ✅ Failed WorkItems have documented error messages
- ✅ Validation shows >99% files present in target
- ✅ Queue contains messages for all uploaded files

### Audit Trail
- State database preserves complete history
- Timestamps for start/completion of each WorkItem
- Error messages for all failures
- File-level tracking for detailed investigation

---

## 🎓 Best Practices

1. **Always use `--resume`** when restarting - it's fast and safe
2. **Monitor in separate terminal** for real-time feedback
3. **Validate before celebrating** - check uploads actually succeeded
4. **Keep state database** - valuable for audit and troubleshooting
5. **Start conservative** - increase parallelism after confirming stability
6. **Use screen/tmux** for long-running tasks on remote machines
7. **Check logs periodically** - catch issues before they accumulate

---

## 📚 Additional Resources

- **Kusto Query Reference**: `WorkitemAndFileKustoQueries.txt`
- **Upload Infrastructure**: `upload.py` (existing production code)
- **Common Utilities**: `performance/common.py` (retry logic, etc.)

---

## ✅ Pre-Flight Checklist

Before starting the reupload:

- [ ] Dependencies installed (`pip install ...`)
- [ ] Authenticated with Azure (`az login`)
- [ ] CSV generated from Kusto
- [ ] Sufficient time allocated (12-72 hours estimate)
- [ ] Monitoring terminal ready
- [ ] State database location confirmed
- [ ] Backup plan if using production system

---

## 🎯 Success Path

```
1. Install dependencies ✓
2. Authenticate with Azure ✓
3. Generate CSV ✓
4. Start reupload script ✓
5. Start monitor script ✓
6. Wait (monitor periodically) ✓
7. Validate uploads ✓
8. Retry failures if any ✓
9. Final validation ✓
10. DONE! 🎉
```

---

## 💬 Support

If you encounter issues:

1. Check the relevant documentation (REUPLOAD_README.md)
2. Query the state database for error details
3. Review logs for specific error patterns
4. Adjust parallelism settings
5. Use validation script to identify missing files
6. Retry failed WorkItems with `--resume`

Good luck with your reupload! 🚀
