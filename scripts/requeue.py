from azure.storage.queue import QueueClient, TextBase64EncodePolicy
from logging import getLogger
from multiprocessing.dummy import Pool as ThreadPool
import csv
import os
import re
import time
import requests
from performance.common import retry_on_exception

# For each link in MissingURIs.csv (link to console.log), read the corresponding uri, parse out the perf-lab-report.json file names, and upload them to the queue
# Get env var for sas token
sas_token = os.getenv('SAS_TOKEN')
queue_client = QueueClient(account_url="https://pvscmdupload.queue.core.windows.net", queue_name="resultsqueue", credential=sas_token, message_encode_policy=TextBase64EncodePolicy())
pool = ThreadPool(16)

def parse_blob_urls(file: requests.Response) -> list[str]:
    parsed_blob_urls : list[str] = []
    # Grep the file for https://pvscmdupload.blob.core.windows.net/results/e762d3bc-4d55-4167-afbc-acdb4794dfdc-System.Collections.Tests.Add_Remove_SteadyState_String_-perf-lab-report.json
    matches = re.findall(r'https://pvscmdupload.blob.core.windows.net/results/[\S]+-perf-lab-report\.json', file.text)
    parsed_blob_urls.extend(matches)
    return parsed_blob_urls

def download_parse_queue(link:str) -> None:
    link_clean = link[link.index('https://'):]
    try:
        file_download = requests.get(link_clean, timeout=5)
        blob_urls = parse_blob_urls(file_download)
        # Upload file names to the queue
        for blob_url in blob_urls:
            full_blob_url = f"{blob_url}{sas_token}"
            try:
                retry_on_exception(lambda: queue_client.send_message(full_blob_url))
                getLogger().info(f"upload and queue complete for {full_blob_url}")
            except Exception as ex:
                getLogger().error("queue failed")
                getLogger().error('{0}: {1}'.format(type(ex), str(ex)))
    except Exception as ex:
        getLogger().error('{0}: {1}'.format(type(ex), str(ex)))
        getLogger().error(f"Failed to download {link_clean}")

start_time = time.time()
with open('MissingURIs.csv', 'r') as file:
    csv_reader = csv.reader(file)
    links : list[str] = []
    for row in csv_reader:
        if len(row) > 0 and row[0].startswith("https://helixri107v0xdeko0k025g8.blob.core.windows.net"):
            links.append(row[0])
    print(f"Found {len(links)} links")
    pool.map(download_parse_queue, links)
    pool.close()
    pool.join()

end_time = time.time()
runtime = end_time - start_time
print(f"Runtime: {runtime} seconds")
  