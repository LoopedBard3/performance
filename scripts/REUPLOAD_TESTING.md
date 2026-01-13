# Testing Strategy - Reupload Failed WorkItems

## 🧪 Recommended Test Approach

### Phase 1: Small Test (1 Machine, ~100 WorkItems)

This validates the entire pipeline with minimal risk and time investment.

---

## Step-by-Step Testing Guide

### 1️⃣ **Generate Small Test CSV** (1 machine only)

Modify the `generate_workitem_csv.py` to test with just one machine:

```bash
python generate_workitem_csv.py \
    --output test_workitems.csv \
    --machines PERFVIPER088
```

This will give you only the WorkItems from PERFVIPER088 (should be ~800-1000 WorkItems based on your 21k total).

---

### 2️⃣ **Verify CSV Content**

Check the CSV file before proceeding:

```bash
# Windows PowerShell
Get-Content test_workitems.csv | Select-Object -First 10
(Get-Content test_workitems.csv).Count - 1  # Subtract header row

# Linux/Mac
head test_workitems.csv
wc -l test_workitems.csv
```

Expected: Should see JobId and WorkItemId columns, ~800-1000 rows.

---

### 3️⃣ **Optional: Test with Even Smaller Sample**

If you want to test with just 10 WorkItems first:

```bash
# Windows PowerShell
Get-Content test_workitems.csv | Select-Object -First 11 | Set-Content tiny_test.csv

# Linux/Mac
head -n 11 test_workitems.csv > tiny_test.csv
```

This gives you 10 WorkItems (plus header) for a 5-10 minute test run.

---

### 4️⃣ **Run Test with Conservative Settings**

Start with lower parallelism for testing:

```bash
# Test run with 10 WorkItems (5-10 minutes)
python reupload_failed_workitems.py \
    --csv tiny_test.csv \
    --state-db test_state.db \
    --workitem-workers 5 \
    --file-workers 5

# OR test with one machine (~800 WorkItems, 30-60 minutes)
python reupload_failed_workitems.py \
    --csv test_workitems.csv \
    --state-db test_state.db \
    --workitem-workers 5 \
    --file-workers 5
```

---

### 5️⃣ **Monitor the Test Run**

In a separate terminal:

```bash
python monitor_reupload.py --state-db test_state.db --refresh 5
```

---

### 6️⃣ **What to Watch For**

During the test run, check for:

✅ **Authentication Success**
- No "ClientAuthenticationError" or "Unauthorized" errors
- Kusto queries succeed
- Blob downloads work
- Uploads complete

✅ **Correct File Processing**
- Files are found in Kusto
- Downloads complete without 404 errors
- Uploads succeed (watch for 429/throttling)
- Queue messages sent

✅ **State Tracking**
- WorkItems progress from `pending` → `in_progress` → `completed`
- Files tracked correctly

❌ **Watch for Problems**
- Authentication failures → Re-run `az login`
- 404/Not Found on source → Check Kusto query/URI format
- 429 throttling → Reduce parallelism
- Timeout errors → Check network/VPN

---

### 7️⃣ **Validate Test Results**

After test completes:

```bash
# Check summary
python monitor_reupload.py --state-db test_state.db --once

# Query state database
sqlite3 test_state.db "SELECT status, COUNT(*) FROM workitems GROUP BY status"

# Check for any failures
sqlite3 test_state.db "SELECT workitem_id, error_message FROM workitems WHERE status='failed'"

# Validate uploads (sample 10 files)
python validate_uploads.py --state-db test_state.db --sample 10
```

Expected results:
- All WorkItems should be `completed` (or small number `failed` with clear errors)
- Validation should show 100% files found in target
- No authentication errors

---

### 8️⃣ **Manual Spot Check** (Optional but recommended)

Pick one WorkItem and verify manually:

```bash
# Get a sample WorkItem and its files
sqlite3 test_state.db "SELECT workitem_id, job_id, files_total FROM workitems LIMIT 1"

# Get blob names for that WorkItem
sqlite3 test_state.db "SELECT filename FROM files WHERE workitem_id='<workitem_id>' AND status='completed' LIMIT 5"
```

Then check in Azure Portal:
1. Navigate to `pvscmdupload` storage account
2. Open `results` container
3. Search for blobs with pattern `<workitem_id>-*`
4. Verify files exist

Also check queue:
1. Navigate to `pvscmdupload` storage account
2. Open `resultsqueue` queue
3. Should see messages (may be processed already if you have consumers)

---

### 9️⃣ **Test Resume Capability**

Interrupt the script (Ctrl+C) mid-run, then resume:

```bash
# Stop the script mid-run (Ctrl+C)

# Resume from where it left off
python reupload_failed_workitems.py \
    --csv test_workitems.csv \
    --state-db test_state.db \
    --workitem-workers 5 \
    --file-workers 5 \
    --resume
```

Should skip completed WorkItems and continue with pending ones.

---

## 🎯 Decision Points

### ✅ If Test Succeeds (No errors, 100% validation)

Proceed to full run:

```bash
# Generate full CSV (all machines)
python generate_workitem_csv.py --output failed_workitems.csv

# Full production run
python reupload_failed_workitems.py \
    --csv failed_workitems.csv \
    --state-db production_state.db \
    --workitem-workers 20 \
    --file-workers 10
```

### ⚠️ If Test Has Minor Issues (< 5% failures)

Investigate failures:
```bash
sqlite3 test_state.db "SELECT workitem_id, error_message FROM workitems WHERE status='failed'"
```

Common fixable issues:
- Source blob missing → Expected if data was cleaned up
- Transient network errors → Will be retried in full run
- Rate limiting → Reduce parallelism

### ❌ If Test Has Major Issues (> 5% failures or systematic errors)

Stop and investigate:
1. Check error messages in state DB
2. Verify authentication (`az account show`)
3. Test Kusto access manually
4. Check source/target storage access
5. Review script logs for patterns

---

## 📋 Pre-Test Checklist

Before running ANY test:

- [ ] Authenticated with Azure: `az login`
- [ ] Verify account: `az account show`
- [ ] Check network/VPN connection
- [ ] Storage account accessible (test in portal)
- [ ] Kusto database accessible
- [ ] Python dependencies installed
- [ ] Sufficient disk space for state DB
- [ ] Separate terminal ready for monitoring

---

## 🧮 Estimated Test Times

| Test Size | WorkItems | Files (est.) | Time (5/5 workers) | Time (20/10 workers) |
|-----------|-----------|--------------|-------------------|---------------------|
| Tiny | 10 | ~8,000 | 5-10 min | 2-5 min |
| Small (1 machine) | ~800 | ~640,000 | 30-90 min | 15-45 min |
| Medium (5 machines) | ~4,000 | ~3,200,000 | 3-8 hours | 1.5-4 hours |
| Full (24 machines) | 21,000 | ~16,800,000 | 12-72 hours | 12-72 hours |

*Times highly dependent on network speed and file sizes*

---

## 💡 Pro Tips for Testing

1. **Start tiny** - 10 WorkItems is enough to validate the entire pipeline
2. **Watch the first few** - Monitor closely for first 5-10 WorkItems
3. **Check both success and failure paths** - Intentionally interrupt to test resume
4. **Validate early** - Run validation script after test completes
5. **Save test state DB** - Keep it for comparison with production run
6. **Note the timing** - Helps estimate full run duration
7. **Test at different times** - Network/storage performance varies

---

## 🔍 Troubleshooting Test Failures

### Authentication Errors
```bash
# Re-authenticate
az login

# Verify correct subscription
az account show

# Test storage access
az storage container list --account-name pvscmdupload --auth-mode login
```

### Kusto Connection Issues
```bash
# Test Kusto access with Azure CLI
az kusto database show --cluster-name engsrvprod --database-name engineeringdata --resource-group <resource-group>
```

### Source Blob Not Found
- Check if source URI format is correct
- Verify blob still exists (may have been cleaned up)
- This might be expected for old data

### Target Upload Failures
- Check storage account firewall rules
- Verify write permissions on `results` container
- Check for storage account throttling in portal

---

## ✅ Test Success Criteria

Your test is successful when:

1. ✅ No authentication errors
2. ✅ All WorkItems reach `completed` or documented `failed` status
3. ✅ Validation shows >95% of files in target storage
4. ✅ Queue messages were sent (if not using `--no-queue`)
5. ✅ Resume works correctly after interruption
6. ✅ Error messages are clear and actionable
7. ✅ Performance is acceptable (files/min rate)

If all criteria met → Proceed to full run with confidence! 🚀

---

## 📞 Quick Reference

```bash
# Tiny test (10 WorkItems, ~5 minutes)
python generate_workitem_csv.py --machines PERFVIPER088 --output test.csv
head -n 11 test.csv > tiny.csv
python reupload_failed_workitems.py --csv tiny.csv --state-db tiny.db --workitem-workers 5 --file-workers 5

# Monitor
python monitor_reupload.py --state-db tiny.db --refresh 5

# Validate
python validate_uploads.py --state-db tiny.db --sample 10

# Check results
sqlite3 tiny.db "SELECT status, COUNT(*) FROM workitems GROUP BY status"
```

Good luck with your test! 🎯
