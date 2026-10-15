#!/usr/bin/env python3
"""Chat with MigrationBot, the agent hosted on Amazon Bedrock AgentCore Runtime.

Usage:
    python3 scripts/chat.py [stack-name]            interactive conversation
    python3 scripts/chat.py [stack-name] -p "text"  send one prompt and exit

Each run uses one runtimeSessionId, so AgentCore Runtime keeps the conversation
(ARNs from earlier turns) in one isolated session. Requires a recent boto3 that
includes the 'bedrock-agentcore' client.
"""
import argparse
import json
import sys
import uuid

import boto3


def stack_outputs(region, stack_name):
    cfn = boto3.client("cloudformation", region_name=region)
    stack = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}


def find_agent_arn(region, stack_name):
    datasync = boto3.client("datasync", region_name=region)
    for agent in datasync.list_agents().get("Agents", []):
        if agent.get("Name") == f"{stack_name}-agent":
            return agent["AgentArn"]
    return "<not found - run scripts/post-deploy.sh>"


def invoke(client, runtime_arn, session_id, prompt):
    response = client.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        runtimeSessionId=session_id,  # must be 33-256 characters; a UUID string is 36
        payload=json.dumps({"prompt": prompt}).encode("utf-8"),
        qualifier="DEFAULT",
    )
    body = response["response"].read().decode("utf-8")
    try:
        return json.loads(body).get("result", body)
    except (ValueError, AttributeError):
        return body


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("stack_name", nargs="?", default="migrationbot")
    parser.add_argument("--region", default=None)
    parser.add_argument("-p", "--prompt", default=None, help="send a single prompt and exit")
    args = parser.parse_args()

    region = args.region or boto3.Session().region_name
    if not region:
        sys.exit("Set AWS_REGION or pass --region.")

    outputs = stack_outputs(region, args.stack_name)
    runtime_arn = outputs["AgentRuntimeArn"]
    client = boto3.client("bedrock-agentcore", region_name=region)
    session_id = str(uuid.uuid4())

    if args.prompt:
        print(invoke(client, runtime_arn, session_id, args.prompt))
        return

    print("MigrationBot - type 'exit' to quit. Values you can use in prompts:")
    print(f"  NFS server            : {outputs['NfsServerPrivateIp']}:/exports/data")
    print(f"  DataSync agent ARN    : {find_agent_arn(region, args.stack_name)}")
    print(f"  Destination bucket ARN: {outputs['MigrationDestinationBucketArn']}")
    print()
    while True:
        try:
            prompt = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if prompt.lower() in ("exit", "quit"):
            break
        if prompt:
            print(f"\nmigrationbot> {invoke(client, runtime_arn, session_id, prompt)}\n")


if __name__ == "__main__":
    main()
