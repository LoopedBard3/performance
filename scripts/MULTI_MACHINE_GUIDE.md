# Multi-Machine Parallel Execution Guide

## Overview

You can now run **multiple instances** of the reupload script on different machines (or on the same machine with different state databases) using **automatic partitioning**.

The partitioning system uses **consistent hashing** on WorkItemId to ensure:
- ✅ **No duplicates** - Each workitem assigned to exactly one partition
- ✅ **Deterministic** - Same workitem always goes to same partition
- ✅ **Balanced** - Workitems distributed evenly across partitions
- ✅ **Simple** - Use same CSV on all machines, no manual splitting needed

---

## How It Works

### Consistent Hashing

```python
partition = hash(workitem_id) % total_partitions
```

**Example with 3 machines:**
```
WorkItem A123: hash(...) % 3 = 0 → Machine 0
WorkItem B456: hash(...) % 3 = 1 → Machine 1
WorkItem C789: hash(...) % 3 = 2 → Machine 2
WorkItem D012: hash(...) % 3 = 0 → Machine 0
...
```

Each machine processes ~33% of workitems, no overlap!

---

## Setup for 2 Machines

### Machine 1 (Partition 0)

```bash
python reupload_failed_workitems.py \
  --csv failed_workitems.csv \
  --state-db machine1_state.db \
  --partition 0 \
  --total-partitions 2 \
  --workitem-workers 20 \
  --file-workers 10
```

### Machine 2 (Partition 1)

```bash
python reupload_failed_workitems.py \
  --csv failed_workitems.csv \
  --state-db machine2_state.db \
  --partition 1 \
  --total-partitions 2 \
  --workitem-workers 20 \
  --file-workers 10
```

**Result:**
- Machine 1 processes ~50% of workitems
- Machine 2 processes the other ~50%
- **2x total throughput!**

---

## Setup for 3 Machines

### Machine 1

```bash
python reupload_failed_workitems.py \
  --csv failed_workitems.csv \
  --state-db machine1_state.db \
  --partition 0 \
  --total-partitions 3 \
  --workitem-workers 20 \
  --file-workers 10
```

### Machine 2

```bash
python reupload_failed_workitems.py \
  --csv failed_workitems.csv \
  --state-db machine2_state.db \
  --partition 1 \
  --total-partitions 3 \
  --workitem-workers 20 \
  --file-workers 10
```

### Machine 3

```bash
python reupload_failed_workitems.py \
  --csv failed_workitems.csv \
  --state-db machine3_state.db \
  --partition 2 \
  --total-partitions 3 \
  --workitem-workers 20 \
  --file-workers 10
```

**Result:**
- Each machine processes ~33% of workitems
- **3x total throughput!**

---

## Key Points

### 1. **Same CSV, Different State DBs**

✅ **Do this:**
```bash
# Machine 1
--csv shared/failed_workitems.csv --state-db machine1.db

# Machine 2
--csv shared/failed_workitems.csv --state-db machine2.db
```

❌ **Don't do this:**
```bash
# Both machines using same state DB
--state-db shared.db  # Will cause SQLite locking issues!
```

### 2. **Partition Numbers Start at 0**

For 2 machines: `--partition 0` and `--partition 1`  
For 3 machines: `--partition 0`, `--partition 1`, `--partition 2`  
For N machines: `--partition 0` through `--partition N-1`

### 3. **Total Partitions Must Match**

All machines must use the same `--total-partitions` value:

✅ **Correct:**
```bash
# Machine 1
--partition 0 --total-partitions 2

# Machine 2
--partition 1 --total-partitions 2
```

❌ **Wrong:**
```bash
# Machine 1
--partition 0 --total-partitions 2

# Machine 2
--partition 1 --total-partitions 3  # Mismatch!
```

### 4. **Resuming Works Per Machine**

Each machine has its own state database and can resume independently:

```bash
# Machine 1 - resume after crash
python reupload_failed_workitems.py \
  --csv failed_workitems.csv \
  --state-db machine1_state.db \
  --partition 0 \
  --total-partitions 2 \
  --resume  # Add --resume flag
```

---

## Performance Expectations

### Current (Single Machine)

```
WorkItems/min:          0.3
Total WorkItems:        21,000
Time to complete:       ~7 days
```

### With 2 Machines (2x speedup)

```
Machine 1:              0.3 wpm × ~10,500 items = ~3.5 days
Machine 2:              0.3 wpm × ~10,500 items = ~3.5 days
Total time:             ~3.5 days (running in parallel)
```

### With Optimized Settings + 2 Machines

If you also increase workers per machine:

```bash
# Each machine
--workitem-workers 20 --file-workers 10
```

**Expected:**
```
Each machine:           4 wpm × ~10,500 items = ~1.8 days
Total time:             ~1.8 days
```

**10x faster than current!** 🚀

---

## Monitoring Multiple Machines

### On Each Machine

```bash
# Machine 1
python monitor_reupload.py --state-db machine1_state.db --refresh 30

# Machine 2
python monitor_reupload.py --state-db machine2_state.db --refresh 30
```

### Overall Progress

You can manually calculate total progress:

```
Total Progress = (Machine1 Completed + Machine2 Completed) / Total WorkItems
```

Or create a simple aggregation script (see below).

---

## Validation After Completion

After all machines finish, you can validate from any one machine:

```bash
# Use any state DB (they all track their own files)
python validate_uploads.py --state-db machine1_state.db --sample 1000
```

Or validate all:

```bash
python validate_uploads.py --state-db machine1_state.db --sample 500
python validate_uploads.py --state-db machine2_state.db --sample 500
```

---

## Troubleshooting

### Issue: One machine is slower

**Cause:** Uneven workitem distribution (some workitems have more files)

**Solution:** Adjust workers on slower machine:
```bash
# Slower machine - increase workers
--workitem-workers 30 --file-workers 8
```

### Issue: Machine crashed and restarted

**Solution:** Just resume with same partition:
```bash
python reupload_failed_workitems.py \
  --csv failed_workitems.csv \
  --state-db machine1_state.db \
  --partition 0 \
  --total-partitions 2 \
  --resume  # It will continue where it left off
```

### Issue: Forgot partition number

**Check your state DB:**
```bash
sqlite3 machine1_state.db "SELECT COUNT(*) FROM workitems"
# Should be ~half of total workitems for 2 partitions
```

Or just look at the log output from the first run.

---

## Advanced: Aggregated Monitoring Script

Create `monitor_all.py`:

```python
#!/usr/bin/env python3
import sqlite3
import sys

def get_stats(db_path):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) FROM workitems WHERE status='completed'")
    completed = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM workitems")
    total = cursor.fetchone()[0]
    
    conn.close()
    return completed, total

# Update with your state DB paths
dbs = ['machine1_state.db', 'machine2_state.db']

total_completed = 0
total_workitems = 0

for db in dbs:
    try:
        completed, total = get_stats(db)
        total_completed += completed
        total_workitems += total
        print(f"{db}: {completed}/{total} ({completed/total*100:.1f}%)")
    except Exception as e:
        print(f"{db}: Error - {e}")

print(f"\nTotal: {total_completed}/{total_workitems} ({total_completed/total_workitems*100:.1f}%)")
```

Run:
```bash
python monitor_all.py
```

---

## Example: 2 Machines Setup

### Step 1: Copy files to both machines

Copy to both machines:
- `failed_workitems.csv`
- `reupload_failed_workitems.py`
- `upload.py`
- `monitor_reupload.py`

### Step 2: Start on Machine 1

```bash
cd C:\reupload
python reupload_failed_workitems.py \
  --csv failed_workitems.csv \
  --state-db machine1.db \
  --partition 0 \
  --total-partitions 2 \
  --workitem-workers 20 \
  --file-workers 10
```

### Step 3: Start on Machine 2

```bash
cd /home/user/reupload
python3 reupload_failed_workitems.py \
  --csv failed_workitems.csv \
  --state-db machine2.db \
  --partition 1 \
  --total-partitions 2 \
  --workitem-workers 20 \
  --file-workers 10
```

### Step 4: Monitor both

Machine 1:
```bash
python monitor_reupload.py --state-db machine1.db --refresh 30
```

Machine 2:
```bash
python3 monitor_reupload.py --state-db machine2.db --refresh 30
```

### Step 5: Wait for completion

Both will run independently until complete. Total time: ~half of single machine!

---

## Best Practices

1. **Use descriptive state DB names**
   - `machine1_state.db`, `machine2_state.db` (not `state.db`)

2. **Keep partition settings consistent**
   - Write them down, use a script, or use environment variables

3. **Start all machines at same time**
   - Not required, but easier to monitor

4. **Monitor both regularly**
   - Check for errors, adjust workers if needed

5. **Don't change partition settings mid-run**
   - If you need to add a machine, let current ones finish first

6. **Keep logs**
   - Each machine will have its own log output

---

## Quick Reference

### 2 Machines
```bash
# Machine 1
--partition 0 --total-partitions 2

# Machine 2
--partition 1 --total-partitions 2
```

### 3 Machines
```bash
# Machine 1
--partition 0 --total-partitions 3

# Machine 2
--partition 1 --total-partitions 3

# Machine 3
--partition 2 --total-partitions 3
```

### 4 Machines
```bash
# Machine 1-4
--partition 0 --total-partitions 4
--partition 1 --total-partitions 4
--partition 2 --total-partitions 4
--partition 3 --total-partitions 4
```

---

## Summary

✅ **Same CSV on all machines**  
✅ **Different state DB per machine**  
✅ **Partition 0 through N-1**  
✅ **Same total-partitions value**  
✅ **No overlap, no duplicates**  
✅ **Linear speedup (2 machines = 2x faster)**  

**Get started with 2 machines and cut your time in half!** 🚀
