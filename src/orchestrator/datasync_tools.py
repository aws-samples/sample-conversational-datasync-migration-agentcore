"""DataSync tools for MigrationBot.

Each function wraps exactly one DataSync API call and returns a short string
the model can relay to the user. The S3 access role and the CloudWatch log group
come from environment variables, so the user never has to repeat them.

Every argument comes from a model acting on user text, so each one is validated
before it reaches the DataSync API. ARNs must belong to this account and Region.
"""
import os
import re

import boto3

datasync = boto3.client("datasync")

S3_ROLE_ARN = os.environ.get("DATASYNC_S3_ROLE_ARN", "")
LOG_GROUP_ARN = os.environ.get("LOG_GROUP_ARN", "")
ACCOUNT_ID = os.environ.get("ACCOUNT_ID", "")
REGION = os.environ.get("AWS_REGION", "")

# ID formats follow the DataSync API model: agent and location IDs are
# [0-9a-z]{17}, task and execution IDs are [0-9a-f]{17}.
_ARN = rf"arn:aws[a-z-]*:datasync:{re.escape(REGION)}:{re.escape(ACCOUNT_ID)}:"
_LOCATION = re.compile(_ARN + r"location/loc-[0-9a-z]{17}")
PATTERNS = {
    "agent_arn": re.compile(_ARN + r"agent/agent-[0-9a-z]{17}"),
    "source_location_arn": _LOCATION,
    "destination_location_arn": _LOCATION,
    "task_arn": re.compile(_ARN + r"task/task-[0-9a-f]{17}"),
    "task_execution_arn": re.compile(
        _ARN + r"task/task-[0-9a-f]{17}/execution/exec-[0-9a-f]{17}"
    ),
    "s3_bucket_arn": re.compile(r"arn:aws[a-z-]*:s3:::[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]"),
    "server_hostname": re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?"),
    "subdirectory": re.compile(r"/[A-Za-z0-9._/-]{0,1023}"),
    "task_name": re.compile(r"[A-Za-z0-9 _.-]{1,256}"),
}


def validate(name, value):
    """Return value if it is a string that fully matches the pattern for name.

    The error names the argument but does not echo the value back to the model.
    """
    if (
        not ACCOUNT_ID
        or not REGION
        or not isinstance(value, str)
        or not PATTERNS[name].fullmatch(value)
        or ".." in value
    ):
        raise ValueError(f"Invalid value for {name}")
    return value


def create_nfs_location(server_hostname: str, subdirectory: str, agent_arn: str) -> str:
    """Register an NFS file share as a DataSync source location."""
    response = datasync.create_location_nfs(
        ServerHostname=validate("server_hostname", server_hostname),
        Subdirectory=validate("subdirectory", subdirectory),
        OnPremConfig={"AgentArns": [validate("agent_arn", agent_arn)]},
    )
    return f"NFS location created: {response['LocationArn']}"


def create_s3_location(s3_bucket_arn: str) -> str:
    """Register an Amazon S3 bucket as a DataSync destination location.

    DataSync always uses the stack's S3 access role, which can only reach the
    sample's destination bucket. The model cannot choose a different role.
    """
    response = datasync.create_location_s3(
        S3BucketArn=validate("s3_bucket_arn", s3_bucket_arn),
        S3Config={"BucketAccessRoleArn": S3_ROLE_ARN},
    )
    return f"S3 location created: {response['LocationArn']}"


def create_migration_task(
    source_location_arn: str,
    destination_location_arn: str,
    task_name: str = "MigrationBotTask",
) -> str:
    """Create a DataSync task that links an NFS source to an S3 destination."""
    params = {
        "SourceLocationArn": validate("source_location_arn", source_location_arn),
        "DestinationLocationArn": validate("destination_location_arn", destination_location_arn),
        "Name": validate("task_name", task_name),
        "Options": {"LogLevel": "TRANSFER", "VerifyMode": "ONLY_FILES_TRANSFERRED"},
    }
    if LOG_GROUP_ARN:
        params["CloudWatchLogGroupArn"] = LOG_GROUP_ARN
    response = datasync.create_task(**params)
    return f"Migration task created: {response['TaskArn']}"


def start_migration(task_arn: str) -> str:
    """Start a DataSync task execution to begin the transfer."""
    response = datasync.start_task_execution(TaskArn=validate("task_arn", task_arn))
    return f"Migration started. Execution ARN: {response['TaskExecutionArn']}"


def get_migration_status(task_execution_arn: str) -> str:
    """Report the status of a DataSync task execution."""
    r = datasync.describe_task_execution(
        TaskExecutionArn=validate("task_execution_arn", task_execution_arn)
    )
    return (
        f"Status: {r.get('Status', 'UNKNOWN')} | "
        f"Files transferred: {r.get('FilesTransferred', 0)} | "
        f"Bytes transferred: {r.get('BytesTransferred', 0):,}"
    )
