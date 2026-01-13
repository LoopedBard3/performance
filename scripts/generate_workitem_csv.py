"""
Helper script to query Kusto and generate CSV of WorkItemId and JobId
for failed upload workitems.
"""

import csv
import argparse
from azure.kusto.data import KustoClient, KustoConnectionStringBuilder
from azure.identity import DefaultAzureCredential
from logging import getLogger, INFO, basicConfig

KUSTO_CLUSTER = "https://engsrvprod.kusto.windows.net/"
KUSTO_DATABASE = "engineeringdata"


def query_failed_workitems(machines: list[str], output_csv: str):
    """Query Kusto for failed upload workitems and write to CSV."""
    
    # Build the query
    machines_str = '", "'.join(machines)
    query = f"""
    let dateLookback = make_datetime(2025, 11, 06, 00, 00, 00);
    let fixedDate = make_datetime(2026, 01, 09, 00, 00, 00);
    let failedUploadWorkItems = WorkItems
    | where Finished between (dateLookback .. fixedDate)
    | where Status == "Pass"
    | where MachineName in ("{machines_str}")
    | project MachineName, JobId, WorkItemId, Name, ConsoleUri
    | where ConsoleUri !contains "ddfun-refs-heads";
    failedUploadWorkItems
    | project JobId, WorkItemId, WorkItemName = Name
    """
    
    getLogger().info("Connecting to Kusto...")
    kcsb = KustoConnectionStringBuilder.with_azure_token_credential(
        KUSTO_CLUSTER,
        DefaultAzureCredential()
    )
    client = KustoClient(kcsb)
    
    getLogger().info("Executing query...")
    response = client.execute(KUSTO_DATABASE, query)
    
    # Write results to CSV
    workitems = []
    for row in response.primary_results[0]:
        workitems.append({
            'JobId': row['JobId'],
            'WorkItemId': row['WorkItemId'],
            'WorkItemName': row['WorkItemName']
        })
    
    getLogger().info(f"Found {len(workitems)} workitems")
    
    with open(output_csv, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['JobId', 'WorkItemId', 'WorkItemName'])
        writer.writeheader()
        writer.writerows(workitems)
    
    getLogger().info(f"Wrote {len(workitems)} workitems to {output_csv}")
    return len(workitems)


def main():
    parser = argparse.ArgumentParser(
        description='Query Kusto for failed workitems and generate CSV'
    )
    parser.add_argument(
        '--output',
        default='failed_workitems.csv',
        help='Output CSV file path (default: failed_workitems.csv)'
    )
    parser.add_argument(
        '--machines',
        nargs='+',
        default=[
            "PERFVIPER088", "PERFVIPER096", "PERFVIPER092", "PERFVIPER102",
            "PERFVIPER091", "PERFVIPER094", "PERFVIPER085", "PERFVIPER098",
            "PERFVIPER074", "PERFVIPER077", "PERFVIPER073", "PERFVIPER093",
            "PERFVIPER100", "PERFVIPER070", "PERFVIPER076", "PERFVIPER097",
            "PERFVIPER086", "PERFVIPER071", "PERFVIPER087", "PERFVIPER095",
            "PERFVIPER089", "PERFVIPER090", "PERFVIPER099", "PERFVIPER101"
        ],
        help='Machine names to filter by'
    )
    
    args = parser.parse_args()
    
    basicConfig(
        level=INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    count = query_failed_workitems(args.machines, args.output)
    print(f"\n✓ Successfully generated {args.output} with {count} workitems")
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(main())
