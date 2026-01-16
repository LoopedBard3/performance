#!/usr/bin/env python3
"""
Generate a new CSV with completed workitems removed.

Usage:
    python filter_completed_csv.py --state-db reupload_state.db --csv failed_workitems.csv --output remaining_workitems.csv
"""

import sqlite3
import csv
import argparse


def get_completed_workitems(state_db_path: str) -> set:
    """Get set of completed WorkItemIds from state database."""
    conn = sqlite3.connect(state_db_path)
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT workitem_id FROM workitems WHERE status = 'completed'
    """)
    
    completed = {row[0] for row in cursor.fetchall()}
    conn.close()
    
    return completed


def filter_csv(input_csv: str, output_csv: str, completed_ids: set):
    """Create new CSV with completed workitems removed."""
    total = 0
    filtered = 0
    remaining = 0
    
    with open(input_csv, 'r', encoding='utf-8') as infile, \
         open(output_csv, 'w', encoding='utf-8', newline='') as outfile:
        
        reader = csv.DictReader(infile)
        writer = csv.DictWriter(outfile, fieldnames=reader.fieldnames)
        writer.writeheader()
        
        for row in reader:
            total += 1
            workitem_id = row.get('WorkItemId') or row.get('workitem_id')
            
            if workitem_id in completed_ids:
                filtered += 1
            else:
                writer.writerow(row)
                remaining += 1
    
    return total, filtered, remaining


def main():
    parser = argparse.ArgumentParser(
        description='Generate new CSV with completed workitems removed'
    )
    parser.add_argument(
        '--state-db',
        required=True,
        help='Path to state database with completed workitems'
    )
    parser.add_argument(
        '--csv',
        required=True,
        help='Path to original CSV file'
    )
    parser.add_argument(
        '--output',
        required=True,
        help='Path to output CSV file (remaining workitems)'
    )
    
    args = parser.parse_args()
    
    print("=" * 80)
    print("GENERATING FILTERED CSV")
    print("=" * 80)
    print()
    
    # Get completed workitems
    print(f"Reading completed workitems from: {args.state_db}")
    completed = get_completed_workitems(args.state_db)
    print(f"  Found {len(completed):,} completed workitems")
    print()
    
    # Filter CSV
    print(f"Filtering CSV: {args.csv}")
    total, filtered, remaining = filter_csv(args.csv, args.output, completed)
    
    print()
    print("=" * 80)
    print("RESULTS")
    print("=" * 80)
    print(f"Total WorkItems in CSV:     {total:,}")
    print(f"  Already Completed:        {filtered:,} ({filtered/total*100:.1f}%)")
    print(f"  Remaining to Process:     {remaining:,} ({remaining/total*100:.1f}%)")
    print()
    print(f"Output written to: {args.output}")
    print("=" * 80)
    print()
    
    # Show sample commands
    print("NEXT STEPS:")
    print("-" * 80)
    print()
    print("# Start fresh with 2 machines using filtered CSV:")
    print()
    print("# Machine 1")
    print(f"python reupload_failed_workitems.py \\")
    print(f"  --csv {args.output} \\")
    print(f"  --state-db machine1.db \\")
    print(f"  --partition 0 \\")
    print(f"  --total-partitions 2 \\")
    print(f"  --workitem-workers 20 \\")
    print(f"  --file-workers 10")
    print()
    print("# Machine 2")
    print(f"python reupload_failed_workitems.py \\")
    print(f"  --csv {args.output} \\")
    print(f"  --state-db machine2.db \\")
    print(f"  --partition 1 \\")
    print(f"  --total-partitions 2 \\")
    print(f"  --workitem-workers 20 \\")
    print(f"  --file-workers 10")
    print()


if __name__ == '__main__':
    main()
