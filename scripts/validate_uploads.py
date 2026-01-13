"""
Validation script to verify uploaded files exist in target storage.

This script checks that files were successfully uploaded by querying
the target storage account and comparing against the state database.
"""

import sqlite3
import argparse
from typing import List, Tuple
from logging import getLogger, INFO, basicConfig
from concurrent.futures import ThreadPoolExecutor, as_completed

from azure.storage.blob import ContainerClient
from azure.core.exceptions import ResourceNotFoundError
from azure.identity import DefaultAzureCredential

TARGET_STORAGE_URI = "https://pvscmdupload.{}.core.windows.net"
TARGET_CONTAINER = "results"


class ValidationResult:
    def __init__(self):
        self.total_checked = 0
        self.found = 0
        self.missing = 0
        self.missing_blobs = []
    
    def print_summary(self):
        print("\n" + "="*80)
        print("VALIDATION SUMMARY")
        print("="*80)
        print(f"Total Files Checked:    {self.total_checked:,}")
        if self.total_checked > 0:
            print(f"  ✓ Found in Target:    {self.found:,} ({self.found/self.total_checked*100:.1f}%)")
            print(f"  ✗ Missing:            {self.missing:,} ({self.missing/self.total_checked*100:.1f}%)")
        else:
            print(f"  ✓ Found in Target:    {self.found:,}")
            print(f"  ✗ Missing:            {self.missing:,}")
        print("="*80)
        
        if self.missing_blobs:
            print("\nMISSING BLOBS (first 20):")
            print("-"*80)
            for blob_name, workitem_id, filename in self.missing_blobs[:20]:
                print(f"  {blob_name} (WorkItem: {workitem_id}, File: {filename})")
            if len(self.missing_blobs) > 20:
                print(f"  ... and {len(self.missing_blobs) - 20} more")
            print()


def get_completed_files(db_path: str) -> List[Tuple[str, str, str]]:
    """Get list of files marked as completed in state database.
    
    Returns:
        List of (workitem_id, filename, blob_name) tuples
    """
    import os
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT w.workitem_id, w.workitem_name, f.filename
        FROM files f
        JOIN workitems w ON f.workitem_id = w.workitem_id AND f.job_id = w.job_id
        WHERE f.status = 'completed'
        ORDER BY w.workitem_id, f.filename
    """)
    
    files = []
    for workitem_id, workitem_name, filename in cursor.fetchall():
        # Match the naming scheme from get_unique_name() in upload.py
        # Format: {WorkItemName}-{basename(filename)}
        blob_name = f"{workitem_name}-{os.path.basename(filename)}"
        files.append((workitem_id, filename, blob_name))
    
    conn.close()
    getLogger().info(f"Found {len(files)} completed files in state database")
    return files


def check_blob_exists(container_client: ContainerClient, blob_name: str) -> bool:
    """Check if a blob exists in the container."""
    try:
        blob_client = container_client.get_blob_client(blob_name)
        blob_client.get_blob_properties()
        return True
    except ResourceNotFoundError:
        return False
    except Exception as e:
        getLogger().warning(f"Error checking blob {blob_name}: {e}")
        return False


def validate_files(db_path: str, sample_size: int = None, workers: int = 10) -> ValidationResult:
    """Validate that completed files exist in target storage.
    
    Args:
        db_path: Path to state database
        sample_size: If set, only validate a random sample of this size
        workers: Number of parallel workers
    
    Returns:
        ValidationResult with summary statistics
    """
    result = ValidationResult()
    
    # Get credential and container client
    getLogger().info("Connecting to target storage...")
    credential = DefaultAzureCredential()
    container_client = ContainerClient(
        account_url=TARGET_STORAGE_URI.format('blob'),
        container_name=TARGET_CONTAINER,
        credential=credential
    )
    
    # Get list of files to check
    files = get_completed_files(db_path)
    
    if sample_size and sample_size < len(files):
        import random
        files = random.sample(files, sample_size)
        getLogger().info(f"Validating random sample of {sample_size} files")
    else:
        getLogger().info(f"Validating all {len(files)} files")
    
    result.total_checked = len(files)
    
    # Check files in parallel
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_file = {
            executor.submit(check_blob_exists, container_client, blob_name): (workitem_id, filename, blob_name)
            for workitem_id, filename, blob_name in files
        }
        
        checked = 0
        for future in as_completed(future_to_file):
            workitem_id, filename, blob_name = future_to_file[future]
            checked += 1
            
            try:
                exists = future.result()
                if exists:
                    result.found += 1
                else:
                    result.missing += 1
                    result.missing_blobs.append((blob_name, workitem_id, filename))
                    getLogger().warning(f"Missing blob: {blob_name}")
                
                # Progress update
                if checked % 100 == 0:
                    getLogger().info(f"Progress: {checked}/{result.total_checked} "
                                   f"(Found: {result.found}, Missing: {result.missing})")
            except Exception as e:
                getLogger().error(f"Error validating {blob_name}: {e}")
                result.missing += 1
                result.missing_blobs.append((blob_name, workitem_id, filename))
    
    return result


def export_missing_to_csv(result: ValidationResult, output_path: str, db_path: str):
    """Export missing blobs to CSV for reprocessing."""
    import csv
    
    if not result.missing_blobs:
        getLogger().info("No missing blobs to export")
        return
    
    # Get WorkItem and Job info from state DB
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['JobId', 'WorkItemId', 'FileName', 'BlobName'])
        writer.writeheader()
        
        for blob_name, workitem_id, filename in result.missing_blobs:
            cursor.execute("""
                SELECT job_id FROM files 
                WHERE workitem_id = ? AND filename = ?
                LIMIT 1
            """, (workitem_id, filename))
            row = cursor.fetchone()
            job_id = row[0] if row else "UNKNOWN"
            
            writer.writerow({
                'JobId': job_id,
                'WorkItemId': workitem_id,
                'FileName': filename,
                'BlobName': blob_name
            })
    
    conn.close()
    getLogger().info(f"Exported {len(result.missing_blobs)} missing files to {output_path}")


def main():
    parser = argparse.ArgumentParser(
        description='Validate uploaded files exist in target storage'
    )
    parser.add_argument(
        '--state-db',
        default='reupload_state.db',
        help='Path to SQLite state database (default: reupload_state.db)'
    )
    parser.add_argument(
        '--sample',
        type=int,
        help='Validate only a random sample of N files (useful for quick checks)'
    )
    parser.add_argument(
        '--workers',
        type=int,
        default=10,
        help='Number of parallel workers (default: 10)'
    )
    parser.add_argument(
        '--export-missing',
        help='Export missing files to CSV for reprocessing'
    )
    
    args = parser.parse_args()
    
    basicConfig(
        level=INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    getLogger().info("Starting validation...")
    
    result = validate_files(args.state_db, args.sample, args.workers)
    result.print_summary()
    
    if args.export_missing and result.missing_blobs:
        export_missing_to_csv(result, args.export_missing, args.state_db)
    
    return 1 if result.missing > 0 else 0


if __name__ == '__main__':
    import sys
    sys.exit(main())
