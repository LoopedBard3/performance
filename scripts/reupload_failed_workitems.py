"""
Script to reupload failed workitem files from source Azure Blob Storage to target storage.

This script:
1. Reads WorkItemId and JobId from CSV
2. Queries Kusto for file metadata (URI, FileName)
3. Downloads files from source blob storage
4. Uploads to target storage using existing upload infrastructure
5. Tracks progress in SQLite database for resume capability
"""

import sys
import csv
import json
import sqlite3
import argparse
import os
import signal
import time
from datetime import datetime
from typing import Optional, List, Dict, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed, CancelledError
from logging import getLogger, INFO, basicConfig
from dataclasses import dataclass

# Azure SDK imports
from azure.kusto.data import KustoClient, KustoConnectionStringBuilder
from azure.kusto.data.exceptions import KustoServiceError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobClient
from azure.storage.queue import QueueClient, TextBase64EncodePolicy
from azure.core.exceptions import ResourceExistsError

# Local imports
from performance.common import retry_on_exception

# QueueMessage class (from upload.py)
class QueueMessage:
    container_name: str
    blob_name: str

    def __init__(self, container: str, name: str):
        self.container_name = container
        self.blob_name = name

# Configuration
KUSTO_CLUSTER = "https://engsrvprod.kusto.windows.net/"
KUSTO_DATABASE = "engineeringdata"
TARGET_STORAGE_URI = "https://pvscmdupload.{}.core.windows.net"
TARGET_CONTAINER = "results"
TARGET_QUEUE = "resultsqueue"

# Performance tuning
MAX_WORKITEM_WORKERS = 20  # Parallel WorkItems
MAX_FILE_WORKERS = 10      # Parallel files per WorkItem
DOWNLOAD_CHUNK_SIZE = 4 * 1024 * 1024  # 4MB chunks for streaming


@dataclass
class FileMetadata:
    """Metadata for a file to be reuploaded."""
    job_id: str
    workitem_id: str
    workitem_name: str
    source_uri: str
    filename: str


@dataclass
class WorkItemStatus:
    """Status of a WorkItem processing."""
    workitem_id: str
    job_id: str
    status: str  # pending, in_progress, completed, failed
    files_total: int
    files_processed: int
    error_message: Optional[str]
    started_at: Optional[datetime]
    completed_at: Optional[datetime]


class StateTracker:
    """SQLite-based state tracker for resume capability with thread-safe access."""
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        # Don't store connection - create per thread instead
        self._lock = __import__('threading').Lock()
        self._thread_local = __import__('threading').local()
        
        # Initialize database schema with a temporary connection
        self._init_db()
    
    def _get_connection(self):
        """Get or create a connection for the current thread."""
        if not hasattr(self._thread_local, 'conn') or self._thread_local.conn is None:
            self._thread_local.conn = sqlite3.connect(self.db_path, timeout=30.0)
        return self._thread_local.conn
    
    def _init_db(self):
        """Initialize database schema."""
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        cursor = conn.cursor()
        
        # WorkItems table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS workitems (
                workitem_id TEXT,
                workitem_name TEXT,
                job_id TEXT,
                status TEXT,
                files_total INTEGER DEFAULT 0,
                files_processed INTEGER DEFAULT 0,
                error_message TEXT,
                started_at TEXT,
                completed_at TEXT,
                PRIMARY KEY (workitem_id, job_id)
            )
        """)
        
        # Files table for detailed tracking
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS files (
                workitem_id TEXT,
                job_id TEXT,
                filename TEXT,
                source_uri TEXT,
                status TEXT,
                error_message TEXT,
                uploaded_at TEXT,
                PRIMARY KEY (workitem_id, job_id, filename)
            )
        """)
        
        conn.commit()
        conn.close()
    
    def add_workitem(self, workitem_id: str, workitem_name: str, job_id: str):
        """Add a new workitem to track."""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR IGNORE INTO workitems (workitem_id, workitem_name, job_id, status)
                VALUES (?, ?, ?, 'pending')
            """, (workitem_id, workitem_name, job_id))
            conn.commit()
    
    def update_workitem_status(self, workitem_id: str, job_id: str, status: str, 
                               error_message: Optional[str] = None):
        """Update workitem status."""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            timestamp = datetime.now(__import__('datetime').timezone.utc).isoformat()
            
            if status == 'in_progress':
                cursor.execute("""
                    UPDATE workitems 
                    SET status = ?, started_at = ?
                    WHERE workitem_id = ? AND job_id = ?
                """, (status, timestamp, workitem_id, job_id))
            elif status in ('completed', 'failed'):
                cursor.execute("""
                    UPDATE workitems 
                    SET status = ?, completed_at = ?, error_message = ?
                    WHERE workitem_id = ? AND job_id = ?
                """, (status, timestamp, error_message, workitem_id, job_id))
            else:
                cursor.execute("""
                    UPDATE workitems 
                    SET status = ?, error_message = ?
                    WHERE workitem_id = ? AND job_id = ?
                """, (status, error_message, workitem_id, job_id))
            
            conn.commit()
    
    def update_workitem_file_count(self, workitem_id: str, job_id: str, files_total: int):
        """Update the total file count for a workitem."""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE workitems 
                SET files_total = ?
                WHERE workitem_id = ? AND job_id = ?
            """, (files_total, workitem_id, job_id))
            conn.commit()
    
    def add_file(self, workitem_id: str, job_id: str, filename: str, source_uri: str):
        """Add a file to track."""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR IGNORE INTO files (workitem_id, job_id, filename, source_uri, status)
                VALUES (?, ?, ?, ?, 'pending')
            """, (workitem_id, job_id, filename, source_uri))
            conn.commit()
    
    def update_file_status(self, workitem_id: str, job_id: str, filename: str, 
                          status: str, error_message: Optional[str] = None):
        """Update file upload status."""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            timestamp = datetime.now(__import__('datetime').timezone.utc).isoformat()
            
            cursor.execute("""
                UPDATE files 
                SET status = ?, error_message = ?, uploaded_at = ?
                WHERE workitem_id = ? AND job_id = ? AND filename = ?
            """, (status, error_message, timestamp, workitem_id, job_id, filename))
            
            conn.commit()
            
            # Update workitem progress
            if status == 'completed':
                cursor.execute("""
                    UPDATE workitems 
                    SET files_processed = files_processed + 1
                    WHERE workitem_id = ? AND job_id = ?
                """, (workitem_id, job_id))
                conn.commit()
    
    def get_workitem_status(self, workitem_id: str, job_id: str) -> Optional[str]:
        """Get current status of a workitem."""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT status FROM workitems 
                WHERE workitem_id = ? AND job_id = ?
            """, (workitem_id, job_id))
            result = cursor.fetchone()
            return result[0] if result else None

    def get_file_status(self, workitem_id: str, job_id: str, filename: str) -> Optional[str]:
        """Get current status of a file."""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT status FROM files
                WHERE workitem_id = ? AND job_id = ? AND filename = ?
            """, (workitem_id, job_id, filename))
            result = cursor.fetchone()
            return result[0] if result else None

    def claim_file(self, workitem_id: str, job_id: str, filename: str) -> bool:
        """Claim a file for processing if it's not already in progress or completed."""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            timestamp = datetime.now(__import__('datetime').timezone.utc).isoformat()
            cursor.execute("""
                UPDATE files
                SET status = 'in_progress', error_message = NULL, uploaded_at = ?
                WHERE workitem_id = ? AND job_id = ? AND filename = ?
                  AND status IN ('pending', 'failed')
            """, (timestamp, workitem_id, job_id, filename))
            conn.commit()
            return cursor.rowcount == 1
    
    def get_pending_workitems(self) -> List[Tuple[str, str]]:
        """Get all workitems that are pending or failed."""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT workitem_id, job_id FROM workitems 
                WHERE status IN ('pending', 'failed', 'in_progress')
                ORDER BY workitem_id
            """)
            return cursor.fetchall()
    
    def get_summary(self) -> Dict[str, int]:
        """Get summary statistics."""
        with self._lock:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("""
                SELECT status, COUNT(*) FROM workitems GROUP BY status
            """)
            summary = dict(cursor.fetchall())
            
            cursor.execute("SELECT COUNT(*) FROM workitems")
            summary['total'] = cursor.fetchone()[0]
            
            # Count files from the files table for accuracy
            cursor.execute("""
                SELECT COUNT(*) FROM files
            """)
            summary['total_files'] = cursor.fetchone()[0]
            
            cursor.execute("""
                SELECT COUNT(*) FROM files WHERE status = 'completed'
            """)
            summary['processed_files'] = cursor.fetchone()[0]
            
            return summary
    
    def close(self):
        """Close database connection for current thread."""
        if hasattr(self._thread_local, 'conn') and self._thread_local.conn:
            self._thread_local.conn.close()
            self._thread_local.conn = None


class KustoQueryHelper:
    """Helper class for querying Kusto."""
    
    def __init__(self, cluster: str, database: str, credential: DefaultAzureCredential):
        self.cluster = cluster
        self.database = database
        self.credential = credential
        self.client = None
        
        # Pre-cache Kusto token
        self._kusto_token = None
        self._kusto_token_expiry = 0
        self._warm_up_credential()
    
    def _warm_up_credential(self):
        """Pre-fetch and cache Kusto token to avoid repeated Azure CLI calls."""
        try:
            getLogger().info("Pre-fetching Kusto token to cache...")
            token = self.credential.get_token("https://kusto.kusto.windows.net/.default")
            self._kusto_token = token
            self._kusto_token_expiry = token.expires_on
            getLogger().info(f"Cached Kusto token (expires in ~{(token.expires_on - time.time())/3600:.1f} hours)")
        except Exception as e:
            getLogger().warning(f"Failed to pre-cache Kusto token: {e}")
    
    def _get_client(self) -> KustoClient:
        """Get or create Kusto client with Azure AD authentication."""
        # Check if token needs refresh (within 5 minutes of expiry)
        if self._kusto_token and (self._kusto_token_expiry - time.time()) < 300:
            getLogger().debug("Kusto token expiring soon, refreshing...")
            self._warm_up_credential()
        
        if self.client is None:
            kcsb = KustoConnectionStringBuilder.with_azure_token_credential(
                self.cluster,
                self.credential
            )
            self.client = KustoClient(kcsb)
        return self.client
    
    def query_files_for_workitem(self, workitem_id: str, job_id: str) -> List[FileMetadata]:
        """Query Kusto for all files associated with a WorkItem."""
        query = f"""
        Files
        | where JobId == "{job_id}" and WorkItemId == "{workitem_id}"
        | where FileName endswith "perf-lab-report.json"
        | project JobId, WorkItemId, WorkItemName, Uri, FileName
        | order by FileName asc
        """
        
        try:
            client = self._get_client()
            response = client.execute(self.database, query)
            
            files = []
            for row in response.primary_results[0]:
                files.append(FileMetadata(
                    job_id=row['JobId'],
                    workitem_id=row['WorkItemId'],
                    workitem_name=row['WorkItemName'],
                    source_uri=row['Uri'],
                    filename=row['FileName']
                ))
            
            getLogger().info(f"Found {len(files)} files for WorkItem {workitem_id} (Job {job_id})")
            return files
            
        except KustoServiceError as e:
            getLogger().error(f"Kusto query failed for WorkItem {workitem_id}: {e}")
            raise
        except Exception as e:
            getLogger().error(f"Unexpected error querying Kusto for WorkItem {workitem_id}: {e}")
            raise


class FileReuploader:
    """Handles downloading and reuploading files."""
    
    def __init__(self, target_storage_uri: str, target_container: str, 
                 target_queue: Optional[str], credential: DefaultAzureCredential):
        self.target_storage_uri = target_storage_uri
        self.target_container = target_container
        self.target_queue = target_queue
        self.credential = credential
        
        # Pre-cache tokens at initialization to avoid repeated subprocess calls
        self._storage_token = None
        self._storage_token_expiry = 0
        self._warm_up_credential()
        
        # Create reusable service clients for connection pooling
        from azure.storage.blob import BlobServiceClient
        from azure.storage.queue import QueueServiceClient
        
        self._blob_service_client = BlobServiceClient(
            account_url=self.target_storage_uri.format('blob'),
            credential=credential,
            max_single_put_size=4*1024*1024,  # 4MB max single upload
            max_block_size=4*1024*1024,       # 4MB blocks
            connection_timeout=300,            # 5 min timeout
            read_timeout=300
        )
        
        if self.target_queue:
            self._queue_service_client = QueueServiceClient(
                account_url=self.target_storage_uri.format('queue'),
                credential=credential,
                message_encode_policy=TextBase64EncodePolicy()
            )
            self._queue_client = self._queue_service_client.get_queue_client(self.target_queue)
        else:
            self._queue_client = None
        
        # Get container client for uploads (reuses connections)
        self._container_client = self._blob_service_client.get_container_client(self.target_container)
    
    def _warm_up_credential(self):
        """Pre-fetch and cache tokens to avoid repeated Azure CLI calls."""
        try:
            getLogger().info("Pre-fetching Azure tokens to cache...")
            # Get storage token (valid for ~1 hour)
            token = self.credential.get_token("https://storage.azure.com/.default")
            self._storage_token = token
            self._storage_token_expiry = token.expires_on
            getLogger().info(f"Cached storage token (expires in ~{(token.expires_on - time.time())/3600:.1f} hours)")
        except Exception as e:
            getLogger().warning(f"Failed to pre-cache token: {e}")
    
    def _get_credential(self):
        """Get Azure credential for target storage."""
        # Check if token is expiring soon (within 5 minutes)
        import time
        if self._storage_token and (self._storage_token_expiry - time.time()) > 300:
            # Token is still valid, reuse it
            return self.credential
        else:
            # Token expired or expiring soon, refresh it
            getLogger().debug("Refreshing storage token...")
            self._warm_up_credential()
        return self.credential
    
    def _download_file(self, source_uri: str) -> bytes:
        """Download file from source blob storage with optimizations."""
        # Parse the source URI to extract storage account, container, and blob name
        # Expected format: https://<account>.blob.core.windows.net/<container>/<blobname>
        # OR with SAS: https://<account>.blob.core.windows.net/<container>/<blobname>?<sas_params>
        
        try:
            # Check if URI already has SAS token (query parameters)
            if '?' in source_uri and ('sig=' in source_uri or 'sv=' in source_uri):
                # URI has SAS token - don't use credential (will conflict)
                getLogger().debug(f"Using SAS token from URI for download")
                blob_client = BlobClient.from_blob_url(
                    source_uri,
                    connection_timeout=60,
                    read_timeout=120
                )
            else:
                # No SAS token - use Azure AD credential
                getLogger().debug(f"Using Azure AD credential for download")
                credential = self._get_credential()
                blob_client = BlobClient.from_blob_url(
                    source_uri,
                    credential=credential,
                    connection_timeout=60,
                    read_timeout=120
                )
            
            # Download blob content with timeout settings
            downloader = blob_client.download_blob(max_concurrency=4)
            return downloader.readall()
            
        except Exception as e:
            getLogger().error(f"Failed to download from {source_uri}: {e}")
            raise
    
    def check_blob_exists(self, blob_name: str) -> bool:
        """Check if a blob already exists in target storage."""
        try:
            blob_client = self._container_client.get_blob_client(blob_name)
            return blob_client.exists()
        except Exception as e:
            getLogger().warning(f"Failed to check if blob exists {blob_name}: {e}")
            return False
    
    def _upload_file(self, file_data: bytes, blob_name: str) -> bool:
        """Upload file to target storage using pooled connections."""
        try:
            # Use pooled blob client from container
            blob_client = self._container_client.get_blob_client(blob_name)
            
            def _upload():
                from azure.storage.blob import ContentSettings
                blob_client.upload_blob(
                    file_data,
                    blob_type="BlockBlob",
                    content_settings=ContentSettings(content_type="application/json"),
                    overwrite=False,
                    max_concurrency=4  # Parallel upload for larger files
                )
            
            retry_on_exception(_upload, raise_exceptions=[ResourceExistsError])
            getLogger().info(f"Uploaded blob: {blob_name}")
            
            # Queue message if queue is specified (use pooled client)
            if self._queue_client:
                try:
                    message = QueueMessage(self.target_container, blob_name)
                    retry_on_exception(lambda: self._queue_client.send_message(json.dumps(message.__dict__)))
                    getLogger().info(f"Queued message for: {blob_name}")
                except Exception as e:
                    getLogger().error(f"Failed to queue message for {blob_name}: {e}")
                    # Don't fail the upload if queuing fails
            
            return True
            
        except ResourceExistsError:
            getLogger().info(f"Blob already exists, skipping: {blob_name}")
            return True
        except Exception as e:
            getLogger().error(f"Failed to upload {blob_name}: {e}")
            return False
    
    def reupload_file(self, file_meta: FileMetadata) -> Tuple[bool, Optional[str]]:
        """Download from source and upload to target. Returns (success, error_message)."""
        try:
            # Generate target blob name matching get_unique_name() from upload.py
            # Format: {WorkItemName}-{basename(filename)}
            # WorkItemName from Kusto = HELIX_WORKITEM_ID in production runs
            blob_name = f"{file_meta.workitem_name}-{os.path.basename(file_meta.filename)}"
            
            # Length check (matching upload.py logic)
            if len(blob_name) > 1024:
                from random import randint
                blob_name = f"{file_meta.workitem_name}-{randint(1000, 9999)}-perf-lab-report.json"
                getLogger().warning(f"Blob name too long, using random fallback: {blob_name}")
            
            # Skip download if blob already exists (idempotent behavior)
            if self.check_blob_exists(blob_name):
                getLogger().info(f"Blob already exists, skipping download: {blob_name}")
                return (True, None)
            
            # Download from source
            getLogger().info(f"Downloading {file_meta.filename} from source...")
            file_data = self._download_file(file_meta.source_uri)
            
            # Upload to target
            getLogger().info(f"Uploading {file_meta.filename} to target {blob_name}...")
            success = self._upload_file(file_data, blob_name)
            
            if success:
                return (True, None)
            else:
                return (False, "Upload failed")
                
        except Exception as e:
            error_msg = f"{type(e).__name__}: {str(e)}"
            getLogger().error(f"Failed to reupload {file_meta.filename}: {error_msg}")
            return (False, error_msg)


def process_workitem(workitem_id: str, job_id: str, 
                     kusto_helper: KustoQueryHelper,
                     reuploader: FileReuploader,
                     state_tracker: StateTracker,
                     max_file_workers: int,
                     shutdown_event: __import__('threading').Event) -> bool:
    """Process a single WorkItem: query files, download, and upload."""
    
    try:
        # Check for shutdown at the start
        if shutdown_event.is_set():
            getLogger().info(f"Skipping WorkItem {workitem_id} due to shutdown")
            return False
        getLogger().info(f"Processing WorkItem {workitem_id} (Job {job_id})")
        
        # Check if already completed
        status = state_tracker.get_workitem_status(workitem_id, job_id)
        if status == 'completed':
            getLogger().info(f"WorkItem {workitem_id} already completed, skipping")
            return True
        
        # Update status to in_progress
        state_tracker.update_workitem_status(workitem_id, job_id, 'in_progress')
        
        # Query Kusto for files
        try:
            files = kusto_helper.query_files_for_workitem(workitem_id, job_id)
        except Exception as e:
            error_msg = f"Kusto query failed: {e}"
            state_tracker.update_workitem_status(workitem_id, job_id, 'failed', error_msg)
            return False
        
        if not files:
            getLogger().warning(f"No files found for WorkItem {workitem_id}")
            state_tracker.update_workitem_status(workitem_id, job_id, 'completed')
            return True
        
        # De-duplicate files by blob name (workitem name + basename)
        unique_files = {}
        duplicate_count = 0
        for file_meta in files:
            blob_key = os.path.basename(file_meta.filename)
            if blob_key in unique_files:
                duplicate_count += 1
                getLogger().debug(
                    f"Duplicate file entry for WorkItem {workitem_id}: {file_meta.filename} (source {file_meta.source_uri})"
                )
                continue
            unique_files[blob_key] = file_meta
        if duplicate_count:
            getLogger().debug(
                f"Detected {duplicate_count} duplicate file entries for WorkItem {workitem_id}; "
                "processing unique filenames only"
            )
        files = list(unique_files.values())

        # Check for shutdown before starting file uploads
        if shutdown_event.is_set():
            getLogger().info(f"Aborting WorkItem {workitem_id} - shutdown requested")
            state_tracker.update_workitem_status(workitem_id, job_id, 'pending')
            return False
        
        # Update file count
        state_tracker.update_workitem_file_count(workitem_id, job_id, len(files))
        
        # Add files to tracking and claim for processing
        files_to_process = []
        skipped_claimed = 0
        skipped_completed = 0
        for file_meta in files:
            state_tracker.add_file(workitem_id, job_id, file_meta.filename, file_meta.source_uri)
            status = state_tracker.get_file_status(workitem_id, job_id, file_meta.filename)
            if status == 'completed':
                skipped_completed += 1
                continue
            if state_tracker.claim_file(workitem_id, job_id, file_meta.filename):
                files_to_process.append(file_meta)
            else:
                skipped_claimed += 1

        if skipped_completed:
            getLogger().info(
                f"Skipping {skipped_completed} already-completed files for WorkItem {workitem_id}"
            )
        if skipped_claimed:
            getLogger().info(
                f"Skipping {skipped_claimed} files already claimed for WorkItem {workitem_id}"
            )
        if not files_to_process:
            state_tracker.update_workitem_status(workitem_id, job_id, 'completed')
            getLogger().info(f"All files already completed or claimed for WorkItem {workitem_id}")
            return True
        
        # Process files in parallel
        failed_files = []

        
        with ThreadPoolExecutor(max_workers=max_file_workers) as executor:
            future_to_file = {
                executor.submit(reuploader.reupload_file, file_meta): file_meta
                for file_meta in files_to_process
            }
            
            for future in as_completed(future_to_file):
                # Check for shutdown during file processing
                if shutdown_event.is_set():
                    getLogger().info(f"Canceling remaining files for WorkItem {workitem_id}")
                    executor.shutdown(wait=False, cancel_futures=True)
                    state_tracker.update_workitem_status(workitem_id, job_id, 'pending', 
                                                        'Shutdown requested mid-processing')
                    return False
                
                file_meta = future_to_file[future]
                try:
                    success, error_msg = future.result()
                    if success:
                        state_tracker.update_file_status(
                            workitem_id, job_id, file_meta.filename, 'completed'
                        )
                    else:
                        state_tracker.update_file_status(
                            workitem_id, job_id, file_meta.filename, 'failed', error_msg
                        )
                        failed_files.append(file_meta.filename)
                except Exception as e:
                    error_msg = f"{type(e).__name__}: {str(e)}"
                    state_tracker.update_file_status(
                        workitem_id, job_id, file_meta.filename, 'failed', error_msg
                    )
                    failed_files.append(file_meta.filename)
        
        # Update WorkItem status
        if failed_files:
            error_msg = f"{len(failed_files)} files failed: {', '.join(failed_files[:5])}"
            if len(failed_files) > 5:
                error_msg += f" and {len(failed_files) - 5} more"
            state_tracker.update_workitem_status(workitem_id, job_id, 'failed', error_msg)
            return False
        else:
            state_tracker.update_workitem_status(workitem_id, job_id, 'completed')
            getLogger().info(f"Successfully processed WorkItem {workitem_id}")
            return True
            
    except Exception as e:
        error_msg = f"Unexpected error: {type(e).__name__}: {str(e)}"
        getLogger().error(f"Failed to process WorkItem {workitem_id}: {error_msg}")
        state_tracker.update_workitem_status(workitem_id, job_id, 'failed', error_msg)
        return False


def belongs_to_partition(workitem_id: str, partition: int, total_partitions: int) -> bool:
    """Determine if a workitem belongs to this partition using consistent hashing.
    
    Uses hash(workitem_id) % total_partitions to deterministically assign workitems.
    This ensures the same workitem always goes to the same partition.
    """
    return hash(workitem_id) % total_partitions == partition


def load_workitems_from_csv(csv_path: str, state_tracker: StateTracker, 
                            partition: int = None, total_partitions: int = None) -> List[Tuple[str, str, str]]:
    """Load WorkItem IDs, Names, and Job IDs from CSV and add to state tracker.
    
    Args:
        csv_path: Path to CSV file
        state_tracker: State tracker to add workitems to
        partition: Partition number for this instance (0-based), None for no partitioning
        total_partitions: Total number of partitions, None for no partitioning
    """
    workitems = []
    skipped_partition = 0
    
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            workitem_id = row.get('WorkItemId') or row.get('workitem_id')
            workitem_name = row.get('WorkItemName') or row.get('workitem_name')
            job_id = row.get('JobId') or row.get('job_id')
            
            if not workitem_id or not job_id:
                getLogger().warning(f"Skipping row with missing data: {row}")
                continue
            
            # Check partition assignment
            if partition is not None and total_partitions is not None:
                if not belongs_to_partition(workitem_id, partition, total_partitions):
                    skipped_partition += 1
                    continue
            
            # If WorkItemName not in CSV, use WorkItemId as fallback
            if not workitem_name:
                getLogger().warning(f"No WorkItemName for {workitem_id}, using WorkItemId as name")
                workitem_name = workitem_id
            
            workitems.append((workitem_id, workitem_name, job_id))
            state_tracker.add_workitem(workitem_id, workitem_name, job_id)
    
    if partition is not None:
        getLogger().info(f"Loaded {len(workitems)} workitems from CSV for partition {partition + 1}/{total_partitions} (skipped {skipped_partition} from other partitions)")
    else:
        getLogger().info(f"Loaded {len(workitems)} workitems from CSV")
    return workitems


def print_summary(state_tracker: StateTracker):
    """Print summary of processing status."""
    summary = state_tracker.get_summary()
    
    print("\n" + "="*60)
    print("REUPLOAD SUMMARY")
    print("="*60)
    print(f"Total WorkItems:       {summary.get('total', 0)}")
    print(f"  Completed:           {summary.get('completed', 0)}")
    print(f"  Failed:              {summary.get('failed', 0)}")
    print(f"  In Progress:         {summary.get('in_progress', 0)}")
    print(f"  Pending:             {summary.get('pending', 0)}")
    print(f"\nTotal Files:           {summary.get('total_files', 0)}")
    print(f"  Processed:           {summary.get('processed_files', 0)}")
    print("="*60 + "\n")


def main():
    # Set up graceful shutdown on Ctrl+C
    shutdown_event = __import__('threading').Event()
    
    def signal_handler(signum, frame):
        if not shutdown_event.is_set():
            shutdown_event.set()
            getLogger().warning("\n⚠️  Shutdown requested. Finishing current WorkItems and saving state...")
            getLogger().warning("Press Ctrl+C again to force quit (may lose progress)")
        else:
            getLogger().error("\n❌ Force quit! State may be incomplete.")
            sys.exit(1)
    
    signal.signal(signal.SIGINT, signal_handler)
    
    parser = argparse.ArgumentParser(
        description='Reupload failed workitem files from source to target Azure Blob Storage'
    )
    parser.add_argument(
        '--csv',
        required=True,
        help='Path to CSV file containing WorkItemId and JobId columns'
    )
    parser.add_argument(
        '--state-db',
        default='reupload_state.db',
        help='Path to SQLite state database (default: reupload_state.db)'
    )
    parser.add_argument(
        '--workitem-workers',
        type=int,
        default=MAX_WORKITEM_WORKERS,
        help=f'Number of parallel WorkItem workers (default: {MAX_WORKITEM_WORKERS})'
    )
    parser.add_argument(
        '--file-workers',
        type=int,
        default=MAX_FILE_WORKERS,
        help=f'Number of parallel file workers per WorkItem (default: {MAX_FILE_WORKERS})'
    )
    parser.add_argument(
        '--resume',
        action='store_true',
        help='Resume from previous run (skip completed WorkItems)'
    )
    parser.add_argument(
        '--no-queue',
        action='store_true',
        help='Skip queuing messages after upload'
    )
    parser.add_argument(
        '--partition',
        type=int,
        help='Partition number (0-based) for this instance (e.g., 0 for first machine)'
    )
    parser.add_argument(
        '--total-partitions',
        type=int,
        help='Total number of partitions/machines running in parallel (e.g., 2 for two machines)'
    )
    
    args = parser.parse_args()
    
    # Validate partition arguments
    if (args.partition is not None) != (args.total_partitions is not None):
        print("Error: --partition and --total-partitions must be used together")
        return 1
    
    if args.partition is not None:
        if args.partition < 0 or args.partition >= args.total_partitions:
            print(f"Error: --partition must be between 0 and {args.total_partitions - 1}")
            return 1
        if args.total_partitions < 2:
            print("Error: --total-partitions must be at least 2")
            return 1
        
        getLogger().info(f"Running in partition mode: partition {args.partition + 1}/{args.total_partitions}")
    
    # Setup logging
    basicConfig(
        level=INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Suppress ALL Azure SDK logging except critical errors
    import logging
    import os
    
    # Set environment variable to disable Azure SDK logging globally
    os.environ['AZURE_LOG_LEVEL'] = 'ERROR'
    
    # Reduce verbosity of Azure SDK loggers to avoid spam
    logging.getLogger('azure').setLevel(logging.ERROR)
    logging.getLogger('azure.identity').setLevel(logging.ERROR)
    logging.getLogger('azure.core').setLevel(logging.ERROR)
    logging.getLogger('azure.identity._credentials').setLevel(logging.ERROR)
    logging.getLogger('azure.identity._credentials.chained').setLevel(logging.ERROR)
    logging.getLogger('azure.identity._credentials.azure_cli').setLevel(logging.ERROR)
    logging.getLogger('azure.identity._internal').setLevel(logging.ERROR)
    logging.getLogger('azure.identity._internal.get_token_mixin').setLevel(logging.ERROR)
    logging.getLogger('azure.core.pipeline.policies.http_logging_policy').setLevel(logging.ERROR)
    logging.getLogger('azure.storage').setLevel(logging.ERROR)
    logging.getLogger('azure.storage.blob').setLevel(logging.ERROR)
    logging.getLogger('azure.storage.queue').setLevel(logging.ERROR)
    
    # Completely disable urllib3 and requests logging (used by Azure SDK)
    logging.getLogger('urllib3').setLevel(logging.ERROR)
    logging.getLogger('requests').setLevel(logging.ERROR)
    
    getLogger().info("Starting reupload script")
    getLogger().info(f"CSV input: {args.csv}")
    getLogger().info(f"State DB: {args.state_db}")
    getLogger().info(f"WorkItem workers: {args.workitem_workers}")
    getLogger().info(f"File workers per WorkItem: {args.file_workers}")
    
    # Initialize shared credential (created once, reused for all operations)
    getLogger().info("Initializing Azure credentials...")
    
    # Use InteractiveBrowserCredential instead of DefaultAzureCredential
    # This is more stable and doesn't rely on Azure CLI subprocess calls
    try:
        from azure.identity import InteractiveBrowserCredential
        
        credential = InteractiveBrowserCredential(
            timeout=30,
            additionally_allowed_tenants=["*"]
        )
        
        # Test the credential by getting a token
        getLogger().info("Testing credential by acquiring token (browser window will open)...")
        test_token = credential.get_token("https://kusto.kusto.windows.net/.default")
        getLogger().info("✓ Credential successfully acquired token")
    except Exception as e:
        getLogger().error(f"Failed to initialize credentials: {e}")
        getLogger().error("Browser authentication failed. Falling back to Azure CLI...")
        
        # Fallback to CLI credential if browser fails
        from azure.identity import AzureCliCredential
        try:
            credential = AzureCliCredential(process_timeout=30)
            test_token = credential.get_token("https://kusto.kusto.windows.net/.default")
            getLogger().info("✓ Azure CLI credential working")
        except Exception as cli_error:
            getLogger().error(f"Azure CLI credential also failed: {cli_error}")
            getLogger().error("Please run 'az login' or ensure browser authentication is available")
            return 1
    
    # Initialize components with shared credential
    state_tracker = StateTracker(args.state_db)
    kusto_helper = KustoQueryHelper(KUSTO_CLUSTER, KUSTO_DATABASE, credential)
    reuploader = FileReuploader(
        TARGET_STORAGE_URI,
        TARGET_CONTAINER,
        None if args.no_queue else TARGET_QUEUE,
        credential
    )
    
    try:
        # Load workitems from CSV (only on first run)
        if not args.resume:
            getLogger().info("Loading workitems from CSV...")
            load_workitems_from_csv(args.csv, state_tracker, args.partition, args.total_partitions)
        else:
            getLogger().info("Resuming from previous run...")
        
        # Get pending workitems
        pending_workitems = state_tracker.get_pending_workitems()
        getLogger().info(f"Processing {len(pending_workitems)} workitems")
        
        if not pending_workitems:
            getLogger().info("No pending workitems to process")
            print_summary(state_tracker)
            return 0
        
        # Process workitems in parallel
        completed = 0
        failed = 0
        skipped = 0
        
        executor = ThreadPoolExecutor(max_workers=args.workitem_workers)
        try:
            future_to_workitem = {
                executor.submit(
                    process_workitem,
                    workitem_id,
                    job_id,
                    kusto_helper,
                    reuploader,
                    state_tracker,
                    args.file_workers,
                    shutdown_event  # Pass shutdown event
                ): (workitem_id, job_id)
                for workitem_id, job_id in pending_workitems
            }
            
            for future in as_completed(future_to_workitem):
                # Check for shutdown request
                if shutdown_event.is_set():
                    getLogger().warning("Shutdown in progress - canceling remaining WorkItems...")
                    # Cancel all pending futures
                    for f in future_to_workitem:
                        if not f.done():
                            f.cancel()
                    break
                
                workitem_id, job_id = future_to_workitem[future]
                try:
                    success = future.result(timeout=2)  # Quick timeout
                    if success:
                        completed += 1
                    elif success is False:
                        failed += 1
                    else:
                        skipped += 1
                    
                    # Progress update every 10 workitems
                    if (completed + failed + skipped) % 10 == 0:
                        getLogger().info(
                            f"Progress: {completed + failed + skipped}/{len(pending_workitems)} "
                            f"(Completed: {completed}, Failed: {failed}, Skipped: {skipped})"
                        )
                        
                except CancelledError:
                    skipped += 1
                    getLogger().debug(f"WorkItem {workitem_id} was cancelled")
                except Exception as e:
                    failed += 1
                    getLogger().error(f"Unexpected error processing WorkItem {workitem_id}: {e}")
        
        finally:
            # Shutdown executor - wait for in-progress tasks
            getLogger().info("Shutting down thread pool...")
            executor.shutdown(wait=True, cancel_futures=True)
            getLogger().info("✓ Thread pool shutdown complete")
        
        # Print final summary
        print_summary(state_tracker)
        
        if failed > 0:
            getLogger().warning(f"{failed} workitems failed - check state DB for details")
            return 1
        else:
            getLogger().info("All workitems processed successfully!")
            return 0
            
    finally:
        state_tracker.close()


if __name__ == '__main__':
    sys.exit(main())
