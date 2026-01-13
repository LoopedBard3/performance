# Reupload Failed WorkItems - File Index

## 📁 Quick Navigation

### 🚀 Start Here
- **[REUPLOAD_QUICKSTART.md](REUPLOAD_QUICKSTART.md)** - 5-minute setup guide

### 📖 Documentation
- **[REUPLOAD_SUMMARY.md](REUPLOAD_SUMMARY.md)** - Complete solution overview
- **[REUPLOAD_README.md](REUPLOAD_README.md)** - Detailed user manual
- **[INDEX.md](INDEX.md)** - This file

### 🛠️ Scripts

#### Core Scripts
1. **generate_workitem_csv.py** - Query Kusto → generate CSV
2. **reupload_failed_workitems.py** - Main reupload orchestrator
3. **monitor_reupload.py** - Real-time progress dashboard
4. **validate_uploads.py** - Verify uploaded files

#### Supporting Files
- **upload.py** - Existing production upload infrastructure
- **performance/common.py** - Shared utilities (retry logic, etc.)
- **WorkitemAndFileKustoQueries.txt** - Reference Kusto queries

---

## 🎯 Typical Usage Flow

```
┌──────────────────────────────────────────────────────────────┐
│ 1. READ: REUPLOAD_QUICKSTART.md                             │
└────────────────────────┬─────────────────────────────────────┘
                         ▼
┌──────────────────────────────────────────────────────────────┐
│ 2. RUN: generate_workitem_csv.py                            │
│    → Generates: failed_workitems.csv                        │
└────────────────────────┬─────────────────────────────────────┘
                         ▼
┌──────────────────────────────────────────────────────────────┐
│ 3. RUN: reupload_failed_workitems.py                        │
│    → Creates: reupload_state.db                             │
└────────────────────────┬─────────────────────────────────────┘
                         ▼
┌──────────────────────────────────────────────────────────────┐
│ 4. RUN: monitor_reupload.py (parallel terminal)             │
│    → Shows: Real-time progress                              │
└────────────────────────┬─────────────────────────────────────┘
                         ▼
┌──────────────────────────────────────────────────────────────┐
│ 5. RUN: validate_uploads.py                                 │
│    → Verifies: Files in target storage                      │
└──────────────────────────────────────────────────────────────┘
```

---

## 📚 When to Read What

### First Time Setup
1. **REUPLOAD_QUICKSTART.md** - Get started in 5 minutes
2. **REUPLOAD_SUMMARY.md** - Understand the architecture

### Detailed Reference
- **REUPLOAD_README.md** - Comprehensive guide for all options

### Troubleshooting
1. **REUPLOAD_README.md** → Troubleshooting section
2. State database queries (examples in README)
3. Log analysis

---

## 🎓 Learning Path

### Beginner
- Start with **REUPLOAD_QUICKSTART.md**
- Run with default settings
- Monitor with **monitor_reupload.py**

### Intermediate
- Read **REUPLOAD_SUMMARY.md** for architecture
- Tune parallelism settings
- Use state database queries

### Advanced
- Read **REUPLOAD_README.md** completely
- Customize SQL queries for analysis
- Modify scripts for specific needs
- Use **validate_uploads.py** for auditing

---

## 🔍 Find Information By Topic

### Authentication
- REUPLOAD_README.md → Prerequisites → Authentication
- REUPLOAD_QUICKSTART.md → Step 2

### Parallelism Tuning
- REUPLOAD_README.md → Performance Tuning
- REUPLOAD_SUMMARY.md → Configuration Options

### Error Handling
- REUPLOAD_README.md → Troubleshooting
- REUPLOAD_SUMMARY.md → Safety Features

### State Tracking
- REUPLOAD_README.md → Monitoring Progress → State Database
- REUPLOAD_SUMMARY.md → Monitoring & Debugging

### Resume/Retry
- REUPLOAD_README.md → Retrying Failed WorkItems
- REUPLOAD_QUICKSTART.md → If Interrupted or Failed

### Validation
- validate_uploads.py script
- REUPLOAD_SUMMARY.md → Phase 3: Validation

---

## 📊 File Size Reference

| File | Lines | Purpose |
|------|-------|---------|
| reupload_failed_workitems.py | ~650 | Main orchestrator |
| generate_workitem_csv.py | ~100 | Kusto query helper |
| monitor_reupload.py | ~300 | Progress dashboard |
| validate_uploads.py | ~250 | Upload verification |
| REUPLOAD_README.md | ~700 | Detailed manual |
| REUPLOAD_SUMMARY.md | ~500 | Architecture overview |
| REUPLOAD_QUICKSTART.md | ~150 | Quick start guide |

---

## 🎬 Example Commands

All examples assume you're in `C:\Users\parkerbibus\repos\performance\scripts`

### Generate CSV
```bash
python generate_workitem_csv.py --output failed_workitems.csv
```

### Run Reupload (Default)
```bash
python reupload_failed_workitems.py --csv failed_workitems.csv
```

### Run Reupload (Conservative)
```bash
python reupload_failed_workitems.py --csv failed_workitems.csv --workitem-workers 10 --file-workers 5
```

### Monitor Progress
```bash
python monitor_reupload.py --refresh 10
```

### Resume After Interruption
```bash
python reupload_failed_workitems.py --csv failed_workitems.csv --resume
```

### Validate Uploads
```bash
python validate_uploads.py --sample 100
```

### Query State Database
```bash
sqlite3 reupload_state.db "SELECT status, COUNT(*) FROM workitems GROUP BY status"
```

---

## 🏆 Success Indicators

You're done when:
- ✅ All WorkItems show `completed` status
- ✅ Validation script shows >99% success rate
- ✅ No errors in state database
- ✅ Queue messages exist for all uploaded files

---

## 💡 Pro Tips

1. **Always read QUICKSTART first** - saves time
2. **Keep all terminals open** - monitor while running
3. **Don't delete state DB** - valuable for audit
4. **Use validation script** - verify success
5. **Bookmark this INDEX** - quick reference

---

## 🆘 Need Help?

1. Check relevant documentation file (see "Find Information By Topic" above)
2. Query state database for errors
3. Review script output logs
4. Check Azure portal for storage/queue status

---

## 📝 Version Info

- **Created**: 2026-01-12
- **Author**: Automated tooling
- **Purpose**: Reupload failed workitem files from source to target storage
- **Scale**: 21,000 WorkItems, ~16.8M files

---

**Ready to start? → [REUPLOAD_QUICKSTART.md](REUPLOAD_QUICKSTART.md)** 🚀
