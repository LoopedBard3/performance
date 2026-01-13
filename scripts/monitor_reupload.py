"""
Monitor reupload progress in real-time.

This script provides a dashboard view of the reupload state database,
showing progress, throughput, and estimated completion time.
"""

import sqlite3
import argparse
import time
import sys
from datetime import datetime, timedelta
from typing import Dict, Optional


def clear_screen():
    """Clear terminal screen."""
    print("\033[2J\033[H", end='')


def get_stats(db_path: str) -> Dict:
    """Get current statistics from state database."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    stats = {}
    
    # WorkItem stats
    cursor.execute("""
        SELECT status, COUNT(*) FROM workitems GROUP BY status
    """)
    status_counts = dict(cursor.fetchall())
    stats['total'] = sum(status_counts.values())
    stats['completed'] = status_counts.get('completed', 0)
    stats['failed'] = status_counts.get('failed', 0)
    stats['in_progress'] = status_counts.get('in_progress', 0)
    stats['pending'] = status_counts.get('pending', 0)
    
    # File stats - count from files table for accuracy
    cursor.execute("""
        SELECT COUNT(*) FROM files
    """)
    stats['total_files'] = cursor.fetchone()[0]
    
    cursor.execute("""
        SELECT COUNT(*) FROM files WHERE status = 'completed'
    """)
    stats['processed_files'] = cursor.fetchone()[0]
    
    # Recent completions (last 5 minutes)
    five_min_ago = (datetime.now(__import__('datetime').timezone.utc) - timedelta(minutes=5)).isoformat()
    cursor.execute("""
        SELECT COUNT(*) FROM workitems 
        WHERE status = 'completed' AND completed_at > ?
    """, (five_min_ago,))
    stats['recent_completions'] = cursor.fetchone()[0]
    
    # Average files per workitem
    cursor.execute("""
        SELECT AVG(files_total) FROM workitems WHERE files_total > 0
    """)
    avg_files = cursor.fetchone()[0]
    stats['avg_files_per_workitem'] = avg_files or 0
    
    # Earliest start time
    cursor.execute("""
        SELECT MIN(started_at) FROM workitems WHERE started_at IS NOT NULL
    """)
    earliest_start = cursor.fetchone()[0]
    stats['earliest_start'] = earliest_start
    
    # Latest completion time
    cursor.execute("""
        SELECT MAX(completed_at) FROM workitems WHERE completed_at IS NOT NULL
    """)
    latest_completion = cursor.fetchone()[0]
    stats['latest_completion'] = latest_completion
    
    # Top errors
    cursor.execute("""
        SELECT error_message, COUNT(*) as cnt 
        FROM workitems 
        WHERE status = 'failed' AND error_message IS NOT NULL
        GROUP BY error_message
        ORDER BY cnt DESC
        LIMIT 5
    """)
    stats['top_errors'] = cursor.fetchall()
    
    conn.close()
    return stats


def calculate_eta(stats: Dict) -> Optional[str]:
    """Calculate estimated time to completion."""
    if stats['total'] == 0 or stats['completed'] == 0:
        return None
    
    if not stats['earliest_start']:
        return None
    
    try:
        start_time = datetime.fromisoformat(stats['earliest_start'])
        elapsed = datetime.now(__import__('datetime').timezone.utc) - start_time
        
        if elapsed.total_seconds() < 60:  # Less than 1 minute
            return None
        
        completed = stats['completed']
        remaining = stats['total'] - completed - stats['failed']
        
        if remaining <= 0:
            return "Complete"
        
        rate = completed / elapsed.total_seconds()  # workitems per second
        if rate == 0:
            return None
        
        eta_seconds = remaining / rate
        eta = datetime.now(__import__('datetime').timezone.utc) + timedelta(seconds=eta_seconds)
        
        return eta.strftime('%Y-%m-%d %H:%M:%S')
    except:
        return None


def format_duration(seconds: float) -> str:
    """Format duration in human-readable format."""
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        minutes = int(seconds / 60)
        secs = int(seconds % 60)
        return f"{minutes}m {secs}s"
    else:
        hours = int(seconds / 3600)
        minutes = int((seconds % 3600) / 60)
        return f"{hours}h {minutes}m"


def print_dashboard(stats: Dict, refresh_interval: int):
    """Print the monitoring dashboard."""
    clear_screen()
    
    print("=" * 80)
    print("REUPLOAD PROGRESS MONITOR".center(80))
    print("=" * 80)
    print()
    
    # Progress overview
    total = stats['total']
    completed = stats['completed']
    failed = stats['failed']
    in_progress = stats['in_progress']
    pending = stats['pending']
    
    processed = completed + failed
    if total > 0:
        pct_complete = (completed / total) * 100
        pct_failed = (failed / total) * 100
        pct_processed = (processed / total) * 100
    else:
        pct_complete = pct_failed = pct_processed = 0
    
    print("WORKITEM STATUS")
    print("-" * 80)
    print(f"Total WorkItems:        {total:,}")
    print(f"  ✓ Completed:          {completed:,} ({pct_complete:.1f}%)")
    print(f"  ✗ Failed:             {failed:,} ({pct_failed:.1f}%)")
    print(f"  ⟳ In Progress:        {in_progress:,}")
    print(f"  ◯ Pending:            {pending:,}")
    print()
    
    # Progress bar
    bar_width = 60
    if total > 0:
        completed_bar = int((completed / total) * bar_width)
        failed_bar = int((failed / total) * bar_width)
        in_progress_bar = int((in_progress / total) * bar_width)
        pending_bar = bar_width - completed_bar - failed_bar - in_progress_bar
        
        bar = (
            "█" * completed_bar +
            "▓" * failed_bar +
            "▒" * in_progress_bar +
            "░" * pending_bar
        )
        print(f"[{bar}] {pct_processed:.1f}%")
        print()
    
    # File stats
    total_files = stats['total_files']
    processed_files = stats['processed_files']
    if total_files > 0:
        pct_files = (processed_files / total_files) * 100
    else:
        pct_files = 0
    
    print("FILE STATUS")
    print("-" * 80)
    print(f"Total Files:            {total_files:,}")
    print(f"  Processed:            {processed_files:,} ({pct_files:.1f}%)")
    print(f"  Remaining:            {total_files - processed_files:,}")
    print(f"Avg Files/WorkItem:     {stats['avg_files_per_workitem']:.1f}")
    print()
    
    # Throughput
    print("THROUGHPUT")
    print("-" * 80)
    if stats['earliest_start']:
        try:
            start_time = datetime.fromisoformat(stats['earliest_start'])
            elapsed = datetime.now(__import__('datetime').timezone.utc) - start_time
            elapsed_seconds = elapsed.total_seconds()
            
            if elapsed_seconds > 0:
                workitem_rate = completed / elapsed_seconds * 60  # per minute
                file_rate = processed_files / elapsed_seconds * 60  # per minute
                
                print(f"Elapsed Time:           {format_duration(elapsed_seconds)}")
                print(f"WorkItems/min:          {workitem_rate:.1f}")
                print(f"Files/min:              {file_rate:.1f}")
                
                # Recent throughput (last 5 min)
                recent_rate = stats['recent_completions']  # already 5-min window
                print(f"Recent (5 min):         {recent_rate} workitems")
            else:
                print("Not enough data yet...")
        except:
            print("Calculating...")
    else:
        print("Not started yet...")
    print()
    
    # ETA
    eta = calculate_eta(stats)
    if eta:
        print("ESTIMATED COMPLETION")
        print("-" * 80)
        print(f"ETA:                    {eta}")
        print()
    
    # Top errors
    if stats['top_errors']:
        print("TOP ERRORS")
        print("-" * 80)
        for error, count in stats['top_errors'][:3]:
            error_short = error[:60] + "..." if len(error) > 60 else error
            print(f"  [{count:,}x] {error_short}")
        print()
    
    # Footer
    print("=" * 80)
    print(f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
          f"Refresh: {refresh_interval}s | Press Ctrl+C to exit")


def monitor(db_path: str, refresh_interval: int):
    """Monitor the state database in a loop."""
    try:
        while True:
            stats = get_stats(db_path)
            print_dashboard(stats, refresh_interval)
            
            # Exit if complete
            if stats['total'] > 0 and stats['pending'] == 0 and stats['in_progress'] == 0:
                print("\n✓ All workitems processed!")
                break
            
            time.sleep(refresh_interval)
    except KeyboardInterrupt:
        print("\n\nMonitoring stopped.")
        sys.exit(0)


def main():
    parser = argparse.ArgumentParser(
        description='Monitor reupload progress in real-time'
    )
    parser.add_argument(
        '--state-db',
        default='reupload_state.db',
        help='Path to SQLite state database (default: reupload_state.db)'
    )
    parser.add_argument(
        '--refresh',
        type=int,
        default=10,
        help='Refresh interval in seconds (default: 10)'
    )
    parser.add_argument(
        '--once',
        action='store_true',
        help='Print stats once and exit (no loop)'
    )
    
    args = parser.parse_args()
    
    if args.once:
        stats = get_stats(args.state_db)
        print_dashboard(stats, args.refresh)
    else:
        monitor(args.state_db, args.refresh)


if __name__ == '__main__':
    main()
