"""DataSync tools for MigrationBot.

Each function wraps exactly one DataSync API call and returns a short string
the model can relay to the user. The S3 access role and the CloudWatch log group
come from environment variables, so the user never has to repeat them.
"""
import os

import boto3

datasync = boto3.client("datasync")

DEFAULT_S3_ROLE_ARN = os.environ.get("DATASYNC_S3_ROLE_ARN", "")
DEFAULT_LOG_GROUP_ARN = os.environ.get("LOG_GROUP_ARN", "")


def create_nfs_location(server_hostname: str, subdirectory: str, agent_arn: str) -> str:
    """Register an NFS file share as a DataSync source location."""
    response = datasync.create_location_nfs(
        ServerHostname=server_hostname,
        Subdirectory=subdirectory,
        OnPremConfig={"AgentArns": [agent_arn]},
    )
    return f"NFS location created: {response['LocationArn']}"


def create_s3_location(s3_bucket_arn: str, role_arn: str = "") -> str:
    """Register an Amazon S3 bucket as a DataSync destination location."""
    response = datasync.create_location_s3(
        S3BucketArn=s3_bucket_arn,
        S3Config={"BucketAccessRoleArn": role_arn or DEFAULT_S3_ROLE_ARN},
    )
    return f"S3 location created: {response['LocationArn']}"


def create_migration_task(
    source_location_arn: str,
    destination_location_arn: str,
    task_name: str = "MigrationBotTask",
) -> str:
    """Create a DataSync task that links an NFS source to an S3 destination."""
    params = {
        "SourceLocationArn": source_location_arn,
        "DestinationLocationArn": destination_location_arn,
        "Name": task_name,
        "Options": {"LogLevel": "TRANSFER", "VerifyMode": "ONLY_FILES_TRANSFERRED"},
    }
    if DEFAULT_LOG_GROUP_ARN:
        params["CloudWatchLogGroupArn"] = DEFAULT_LOG_GROUP_ARN
    response = datasync.create_task(**params)
    return f"Migration task created: {response['TaskArn']}"


def start_migration(task_arn: str) -> str:
    """Start a DataSync task execution to begin the transfer."""
    response = datasync.start_task_execution(TaskArn=task_arn)
    return f"Migration started. Execution ARN: {response['TaskExecutionArn']}"


def get_migration_status(task_execution_arn: str) -> str:
    """Report the status of a DataSync task execution."""
    r = datasync.describe_task_execution(TaskExecutionArn=task_execution_arn)
    return (
        f"Status: {r.get('Status', 'UNKNOWN')} | "
        f"Files transferred: {r.get('FilesTransferred', 0)} | "
        f"Bytes transferred: {r.get('BytesTransferred', 0):,}"
    )
