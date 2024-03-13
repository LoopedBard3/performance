from azure.storage.queue import QueueClient, TextBase64EncodePolicy
from azure.storage.blob import ContainerClient, ContentSettings
from azure.core.exceptions import ResourceExistsError
from logging import getLogger
from performance.logger import setup_loggers
from multiprocessing.dummy import Pool as ThreadPool
from logging import DEBUG
import csv
import os
import time
import functools
import json
import requests
from performance.common import retry_on_exception

# For each link in MissingURIs.csv (link to console.log), read the corresponding uri, parse out the perf-lab-report.json file names, and upload them to the queue
# Get env var for sas token
pool_size = 16
sas_token = os.getenv('SAS_TOKEN')
setup_loggers(False)
sess = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size)
sess.mount('https://', adapter)
queue_client = QueueClient(account_url="https://pvscmdupload.queue.core.windows.net", queue_name="resultsqueue", credential=sas_token, message_encode_policy=TextBase64EncodePolicy())
container_client = ContainerClient(account_url="https://pvscmdupload.blob.core.windows.net", container_name="results", credential=sas_token, message_encode_policy=TextBase64EncodePolicy(), session=sess)
pool = ThreadPool(pool_size)
failed_workitems = []

def update_blob_by_workitem(workitem_name:str) -> None:
    try:
        enable_download = True
        enable_update = True and enable_download
        enable_upload = True and enable_update
        # Get, download, update, and upload file names to the queue
        getLogger().warning("Processing blobs for %s", workitem_name)
        blob_count = 0
        for blob_url in container_client.list_blob_names(name_starts_with=workitem_name):
            if blob_url.endswith("combined-perf-lab-report.json"):
                continue
            blob_count += 1
            getLogger().debug("Processing blob %d in %s", blob_count, workitem_name)
            blobclient = container_client.get_blob_client(blob_url)
            data = None
            # Download the file
            if enable_download:
                getLogger().debug("Downloading %s", blob_url)
                try:
                    data = json.loads(blobclient.download_blob().readall())
                    getLogger().debug("Downloaded %s with branch %s", blob_url, data["build"]["branch"])
                    if data is None:
                        getLogger().error("download failed for blob %s", blob_url)
                        raise Exception("download failed for blob %s", blob_url)
                except Exception as ex:
                    failed_workitems.append(workitem_name)
                    getLogger().error("download failed for blob %s workitem %s", blob_url, workitem_name)
                    getLogger().error('%s: %s', type(ex), str(ex))
            # Update the file
            updated_data = ""
            if enable_update and data is not None and data["build"]["repo"] == "dotnet/core-sdk" and data["build"]["branch"] == "refs/heads/main":
                getLogger().debug("Updating %s", blob_url)
                if data is not None and data["build"]["repo"] == "dotnet/core-sdk" and data["build"]["branch"] == "refs/heads/main":
                    tfm = data["build"]["additionalData"]["targetFrameworks"]
                    productVersion = data["build"]["additionalData"]["productVersion"]
                    getLogger().debug("Found targetFrameworks: %s and productVersion: %s for blob %s", tfm, productVersion, blob_url)
                    if tfm is not None and productVersion is not None and tfm[3] == productVersion[0] and (tfm[3] == "8" or tfm[3] == "9"):
                        data["build"]["branch"] = tfm[3] + ".0"
                        updated_data = json.dumps(data, indent=2)
                        getLogger().debug("Updated %s with branch %s", blob_url, data["build"]["branch"])
                    elif tfm is not None and productVersion is not None and tfm[3] != productVersion[0]:
                        getLogger().error("targetFrameworks: %s and productVersion: %s do not match for blob %s", tfm, productVersion, blob_url)
            # Upload the file
            if enable_upload and updated_data != "":
                full_blob_url = f"https://pvscmdupload.blob.core.windows.net/results/{blob_url}{sas_token}"
                getLogger().debug("Uploading %s full_url %s", blob_url, full_blob_url)
                upload_succeded = False
                try:
                    retry_on_exception(functools.partial(blobclient.upload_blob, updated_data, blob_type="BlockBlob", content_settings=ContentSettings(content_type="application/json"), overwrite=True), raise_exceptions=[ResourceExistsError])
                    upload_succeded = True
                except Exception as ex:
                    failed_workitems.append(workitem_name)
                    getLogger().error("upload failed for blob %s", full_blob_url)
                    getLogger().error('%s: %s', type(ex), str(ex))
                if upload_succeded:
                    try:
                        retry_on_exception(functools.partial(queue_client.send_message, full_blob_url))
                        getLogger().debug("upload and queue complete for %s", full_blob_url)
                    except Exception as ex:
                        failed_workitems.append(workitem_name)
                        getLogger().error("queue failed for workitem %s", workitem_name)
                        getLogger().error('%s: %s', type(ex), str(ex))
        getLogger().warning("Processed %d blobs for %s", blob_count, workitem_name)
    except Exception as ex:
        failed_workitems.append(workitem_name)
        getLogger().error('%s: %s', type(ex), str(ex))
        getLogger().error("Failed to process workitem %s", workitem_name)

start_time = time.time()
with open('WorkitemNamesToUpdateSample.csv', 'r') as file:
    csv_reader = csv.reader(file)
    workitems : list[str] = []
    for row in csv_reader:
        if len(row) > 0:
            workitems.append(row[0])
    getLogger().warning(f"Found {len(workitems)} links")
    pool.map(update_blob_by_workitem, workitems)
    pool.close()
    pool.join()

if len(failed_workitems) > 0:
    getLogger().error("Failed to process workitems: %s", failed_workitems)
    with open('FailedWorkitems.csv', 'w') as file:
        csv_writer = csv.writer(file)
        for workitem in failed_workitems:
            csv_writer.writerow([workitem])
end_time = time.time()
runtime = end_time - start_time
getLogger().warning(f"Runtime: {runtime} seconds")
  