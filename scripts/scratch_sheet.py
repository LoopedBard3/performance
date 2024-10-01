from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import ContainerClient, ContentSettings
from azure.storage.queue import QueueClient, TextBase64EncodePolicy
from azure.identity import DefaultAzureCredential
from glob import glob
from performance.common import retry_on_exception

from logging import getLogger

import os
import json


class QueueMessage:
    container_name: str
    blob_name: str

    def __init__(self, container: str, name: str):
        self.container_name = container
        self.blob_name = name

# Download from blob storage, blobs that match the given prefix
def download_blobs(prefix: str, destination: str, credential):
    import concurrent.futures

    def download_blob(blob_name: str, container_client, destination: str):
        try:
            print("Downloading {}".format(blob_name))
            blob_client = container_client.get_blob_client(blob_name)
            # Read the blob into a json object
            json_data = json.loads(blob_client.download_blob().readall())
            if len(json_data["run"]["configurations"]) == 2 and str.lower(json_data["run"]["configurations"]["CompilationMode"]) == "tiered" and json_data["run"]["configurations"]["RunKind"] == "micro_mono":
                json_data["run"]["configurations"]["LLVM"] = "True"
                json_data["run"]["configurations"]["MonoInterpreter"] = "False"
                json_data["run"]["configurations"]["MonoAOT"] = "True"

            blob_name = blob_name.replace(prefix, prefix + "-fix")

            with open(os.path.join(destination, blob_name), "w") as data:
                data.write(json.dumps(json_data, indent=2))

        except Exception as ex:
            print("Failed to download and update {}: {}: {}".format(blob_name, type(ex), str(ex)))

    try:
        container = "results"
        container_client = ContainerClient(account_url='https://pvscmdupload.{}.core.windows.net'.format('blob'), container_name=container, credential=credential)
        blobs = container_client.list_blobs(name_starts_with=prefix)
        # Make sure the destination directory exists
        if not os.path.exists(destination):
            os.makedirs(destination)
        print("Downloading blobs with prefix: {} to destination: {}".format(prefix, os.path.abspath(destination)))

        with concurrent.futures.ThreadPoolExecutor() as executor:
            futures = [executor.submit(download_blob, blob.name, container_client, destination) for blob in blobs]
            concurrent.futures.wait(futures)

    except Exception as ex:
        print("download failed")
        print('{0}: {1}'.format(type(ex), str(ex)))
        return 1
    return 0 # 0 if download succeeded, 1 otherwise
    
# Upload function that takes the files from the download_blobs function and uploads them to the blob storage and queues them
def upload_blobs(prefix: str, directory:str, credential):
    container = "results"
    queue = "resultsqueue"
    container_client = ContainerClient(account_url='https://pvscmdupload.{}.core.windows.net'.format('blob'), container_name=container, credential=credential)
    queue_client = QueueClient(account_url="https://pvscmdupload.{}.core.windows.net".format('queue'), queue_name=queue, credential=credential, message_encode_policy=TextBase64EncodePolicy())
    files = glob(f"{directory}/{prefix}*.json", recursive=True)
    any_upload_or_queue_failed = False
    file_number = 0
    number_failed = 0
    for infile in files:
        file_number += 1
        blob_name = os.path.basename(infile)
        print("[{}/{}] uploading {} from {}".format(file_number, len(files), blob_name, infile))
        
        upload_succeded = False
        with open(infile, "rb") as data:
            try:
                retry_on_exception(lambda: container_client.upload_blob(blob_name, data, blob_type="BlockBlob", content_settings=ContentSettings(content_type="application/json")), raise_exceptions=[ResourceExistsError])
                upload_succeded = True
            except Exception as ex:
                any_upload_or_queue_failed = True
                print("upload failed")
                print('{0}: {1}'.format(type(ex), str(ex)))
                number_failed += 1

        if upload_succeded:
            try:
                message = QueueMessage(container, blob_name)
                print("Queueing message: {}".format(json.dumps(message.__dict__)))
                retry_on_exception(lambda: queue_client.send_message(json.dumps(message.__dict__)))
                print("upload and queue complete")
            except Exception as ex:
                any_upload_or_queue_failed = True
                print("queue failed")
                print('{0}: {1}'.format(type(ex), str(ex)))
    print("Processed {} files, {} failed".format(len(files), number_failed))
    return any_upload_or_queue_failed # 0 (False) if all uploads and queues succeeded, 1 (True) otherwise


# Main function
def main():
    # Open refill_workitems.txt and read each line, taking the text after the underscore and trimming the white space
    # Each line should be of the form <RunCorrelationID's/JobNames>_<WorkItemID>, e.g. ed95b305-af42-48b4-b44d-a1aed518943e_2faac959-4d9c-4be8-9986-a162c7d68ee7. This can be generated using ADX.
    # Then call download_blobs with the trimmed text as the prefix and "results_download" as the destination
    print("Starting")
    print("Current directory: {}".format(os.getcwd()))
    credential = DefaultAzureCredential()
    with open(".\\scripts\\refill_workitems.txt", "r") as f:
        for line in f:
            print("Reading line: {}".format(line))
            prefix = line.split("_")[1].strip()
            print("Downloading blobs with prefix: {}".format(prefix))
            download_return = download_blobs(prefix, "results_download", credential)
            if download_return == 0:
                print("Finished downloading blobs with prefix: {}".format(prefix))
                print("Uploading blobs with prefix: {}".format(prefix))
            else:
                print("Failed to download blobs with prefix: {}".format(prefix))
                return 1
            upload_return = upload_blobs(prefix, "results_download", credential)
            if upload_return == 0:
                print("Finished uploading blobs with prefix: {}".format(prefix))
            else:
                print("Failed to upload blobs with prefix: {}".format(prefix))
                return 1
            print("Finished downloading and uploading blobs with prefix: {}".format(prefix))
    
    print("Finished with everything")

if __name__ == "__main__":
    main()