# Quick Start Guide - Reupload Failed WorkItems

## 🚀 Fast Setup (5 minutes)

### 1. Install Dependencies

```bash
cd C:\Users\parkerbibus\repos\performance\scripts
pip install azure-kusto-data azure-identity azure-storage-blob azure-storage-queue
```

### 2. Authenticate with Azure

```bash
az login
```

### 3. Generate WorkItem CSV

```bash
python generate_workitem_csv.py --output failed_workitems.csv
```

### 4. Start Reupload (Open Terminal 1)

```bash
python reupload_failed_workitems.py --csv failed_workitems.csv
```

### 5. Monitor Progress (Open Terminal 2)

```bash
python monitor_reupload.py --refresh 10
```

---

## 📊 What to Expect

**Initial Query Phase (~1-2 minutes)**
- Loads 21,000 WorkItems into state database
- Each WorkItem queries Kusto for file list

**Download & Upload Phase (12-72 hours estimated)**
- 20 WorkItems processed in parallel
- 10 files per WorkItem in parallel
- Progress updates every 10 WorkItems
- Real-time monitoring available

**Completion**
- Summary report printed
- State database preserved for audit
- Failed items can be retried

---

## 🔄 If Interrupted or Failed

### Resume from where it left off:

```bash
python reupload_failed_workitems.py --csv failed_workitems.csv --resume
```

### Retry only failed WorkItems:

```bash
# Export failed items to new CSV
sqlite3 -header -csv reupload_state.db \
    "SELECT job_id AS JobId, workitem_id AS WorkItemId FROM workitems WHERE status='failed'" \
    > failed_only.csv

# Reprocess with new state DB
python reupload_failed_workitems.py --csv failed_only.csv --state-db retry_state.db
```

---

## ⚙️ Performance Tuning

### High-Speed Configuration

```bash
python reupload_failed_workitems.py \
    --csv failed_workitems.csv \
    --workitem-workers 30 \
    --file-workers 15
```

### Conservative (if hitting rate limits)

```bash
python reupload_failed_workitems.py \
    --csv failed_workitems.csv \
    --workitem-workers 10 \
    --file-workers 5
```

---

## 🔍 Check Status Anytime

### View Summary

```bash
python monitor_reupload.py --once
```

### Query State Database

```bash
# Count failures
sqlite3 reupload_state.db "SELECT COUNT(*) FROM workitems WHERE status='failed'"

# See error messages
sqlite3 reupload_state.db "SELECT workitem_id, error_message FROM workitems WHERE status='failed' LIMIT 10"

# Overall progress
sqlite3 reupload_state.db "SELECT status, COUNT(*) FROM workitems GROUP BY status"
```

---

## 📁 Files Created

| File | Purpose |
|------|---------|
| `failed_workitems.csv` | Input: WorkItemId and JobId list |
| `reupload_state.db` | State tracking (resume capability) |
| `reupload.log` | Optional: capture all output |

---

## ⚠️ Troubleshooting Quick Fixes

| Problem | Solution |
|---------|----------|
| Authentication error | Run `az login` |
| 429 throttling errors | Reduce `--workitem-workers` to 10 |
| Kusto query fails | Check VPN/network, verify permissions |
| Script crashes | Use `--resume` to continue |
| Out of memory | Reduce `--workitem-workers` to 5 |

---

## 💡 Pro Tips

1. **Run in `screen` or `tmux`** for long-running tasks
2. **Monitor in separate terminal** while script runs
3. **Check state DB periodically** to catch issues early
4. **Use `--resume` liberally** - it's safe and fast
5. **Keep logs**: `python reupload_failed_workitems.py ... 2>&1 | tee reupload.log`

---

## 📞 Need Help?

See full documentation: `REUPLOAD_README.md`

---

## ✅ Final Checklist

- [ ] Dependencies installed
- [ ] Authenticated with `az login`
- [ ] Access to the kusto, blob storage, and queue storage
- [ ] CSV generated from Kusto
- [ ] Reupload script running
- [ ] Monitor script shows progress
- [ ] Check for failures periodically
- [ ] Resume if interrupted

**You're all set! The script will handle the rest.** 🎉
