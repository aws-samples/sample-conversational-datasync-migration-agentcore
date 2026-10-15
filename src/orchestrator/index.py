"""MigrationBot tool dispatcher.

Invoked by an Amazon Bedrock AgentCore Gateway Lambda target. The gateway names
each tool '<targetName>___<toolName>' (passed in the Lambda client context) and
passes the tool arguments as the event. This handler dispatches on the tool name.
"""
import inspect
import logging
import os

import boto3

import datasync_tools

logger = logging.getLogger()
logger.setLevel(logging.INFO)

kb_runtime = boto3.client("bedrock-agent-runtime")

DELIMITER = "___"
MAX_PASSAGE_CHARS = 1200


def search_knowledge_base(query: str) -> str:
    """Search the migration best-practices knowledge base."""
    response = kb_runtime.retrieve(
        knowledgeBaseId=os.environ["KNOWLEDGE_BASE_ID"],
        retrievalQuery={"text": query},
        retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": 5}},
    )
    passages = []
    for i, item in enumerate(response.get("retrievalResults", []), start=1):
        text = item.get("content", {}).get("text", "")[:MAX_PASSAGE_CHARS]
        source = item.get("location", {}).get("s3Location", {}).get("uri", "unknown source")
        passages.append(f"[{i}] (source: {source}) {text}")
    if not passages:
        return (
            "No relevant passages were found. The knowledge base may not be "
            "synced yet (run scripts/post-deploy.sh)."
        )
    return "\n\n".join(passages)


TOOLS = {
    "create_nfs_location": datasync_tools.create_nfs_location,
    "create_s3_location": datasync_tools.create_s3_location,
    "create_migration_task": datasync_tools.create_migration_task,
    "start_migration": datasync_tools.start_migration,
    "get_migration_status": datasync_tools.get_migration_status,
    "search_knowledge_base": search_knowledge_base,
}


def get_tool_name(context):
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    full_name = custom.get("bedrockAgentCoreToolName", "")
    return full_name.split(DELIMITER, 1)[1] if DELIMITER in full_name else full_name


def lambda_handler(event, context):
    tool = get_tool_name(context)
    function = TOOLS.get(tool)
    if function is None:
        return {"result": f"Unknown function: {tool}"}
    accepted = inspect.signature(function).parameters
    arguments = {k: v for k, v in (event or {}).items() if k in accepted}
    try:
        return {"result": function(**arguments)}
    except Exception as exc:  # surfaced to the agent so it can report the problem
        logger.exception("Tool %s failed", tool)
        return {"result": f"Error executing {tool}: {exc}"}
