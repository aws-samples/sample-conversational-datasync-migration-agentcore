"""MigrationBot: a Strands agent hosted on Amazon Bedrock AgentCore Runtime.

The agent reasons with an Amazon Bedrock model and reaches its DataSync and
knowledge base tools through an AgentCore Gateway (MCP, IAM/SigV4 authentication).
"""
import logging
import os

import boto3
from bedrock_agentcore import BedrockAgentCoreApp
from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("migrationbot")

app = BedrockAgentCoreApp()

SYSTEM_PROMPT = """You are MigrationBot, an expert AWS data migration assistant.
You orchestrate AWS DataSync migrations through natural language.

- When the user provides every required parameter for an operation, call the tool
  immediately. Do not ask for confirmation on routine, non-destructive steps.
- Only ask a follow-up question when a required parameter is genuinely missing.
- After each operation, report the result including the full ARN, so the user can
  use it in the next request.
- For strategy and best-practice questions (migration strategy, cutover, DataSync
  guidance), ALWAYS call the knowledge base search tool first (its name ends in
  search_knowledge_base), answer from the returned passages, and cite the source
  document. If it returns no relevant passages, say so instead of guessing.
- Use only the tools you were given. Do not claim to have performed an action
  you did not perform.
"""

_agent = None


def _get_agent():
    """Create the agent on first use and keep it for the life of the session.

    AgentCore Runtime routes every request for a session id to the same isolated
    environment, so keeping the agent in a module-level variable preserves the
    conversation (ARNs from earlier turns) within a session without sharing it
    across sessions. Network connections are opened lazily here, not at import.
    """
    global _agent
    if _agent is None:
        region = os.environ.get("MIGRATIONBOT_REGION") or boto3.Session().region_name
        gateway_url = os.environ["GATEWAY_URL"].rstrip("/")
        if not gateway_url.endswith("/mcp"):
            gateway_url += "/mcp"
        gateway = MCPClient(
            lambda: aws_iam_streamablehttp_client(
                endpoint=gateway_url,
                aws_region=region,
                aws_service="bedrock-agentcore",
            )
        )
        _agent = Agent(
            model=BedrockModel(model_id=os.environ["MODEL_ID"], region_name=region),
            system_prompt=SYSTEM_PROMPT,
            tools=[gateway],
        )
    return _agent


@app.entrypoint
def invoke(payload):
    user_message = (payload or {}).get("prompt", "")
    if not user_message:
        return {"result": "Send a JSON payload such as {\"prompt\": \"What tools do you have?\"}."}
    logger.info("User input: %s", user_message)
    try:
        agent = _get_agent()
    except Exception:
        # Typically the gateway is unreachable or the runtime role cannot invoke it.
        logger.exception("Could not connect to the AgentCore Gateway")
        return {"result": "MigrationBot could not connect to its tools. Check the runtime logs."}
    result = agent(user_message)
    return {"result": str(result).strip()}


if __name__ == "__main__":
    app.run()
