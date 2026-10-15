#!/usr/bin/env bash
# Post-deployment steps for the MigrationBot sample:
#   1. Upload the best-practices document to the Knowledge Base bucket
#   2. Sync (ingest) the Knowledge Base data source
#   3. Activate the DataSync agent and wait for it to report ONLINE
#
# Usage: scripts/post-deploy.sh [stack-name]
# Requires the AWS CLI, curl, and credentials for the account that owns the stack.
# The agent step requires the stack to have been deployed with AgentActivationCidr
# set to your public IP address (scripts/deploy.sh does this for you).

set -euo pipefail

STACK_NAME="${1:-${STACK_NAME:-migrationbot}}"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region || true)}}"
if [[ -z "${REGION}" ]]; then
  echo "Set AWS_REGION or configure a default region." >&2
  exit 1
fi
export AWS_DEFAULT_REGION="${REGION}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOC="${SCRIPT_DIR}/../assets/datasync-best-practices.md"
AGENT_NAME="${STACK_NAME}-agent"

output() {
  aws cloudformation describe-stacks --stack-name "${STACK_NAME}" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue | [0]" --output text
}

KB_BUCKET="$(output KBDocsBucketName)"
KB_ID="$(output KnowledgeBaseId)"
DS_ID="$(output KnowledgeBaseDataSourceId)"
AGENT_IP="$(output DataSyncAgentPublicIp)"

echo "==> Uploading knowledge base document to s3://${KB_BUCKET}/"
aws s3 cp "${DOC}" "s3://${KB_BUCKET}/datasync-best-practices.md" --only-show-errors

echo "==> Starting knowledge base ingestion"
MAX_POLLS=90

JOB_ID="$(aws bedrock-agent start-ingestion-job --knowledge-base-id "${KB_ID}" \
  --data-source-id "${DS_ID}" --query 'ingestionJob.ingestionJobId' --output text)"
for _ in $(seq 1 "${MAX_POLLS}"); do
  STATUS="$(aws bedrock-agent get-ingestion-job --knowledge-base-id "${KB_ID}" \
    --data-source-id "${DS_ID}" --ingestion-job-id "${JOB_ID}" \
    --query 'ingestionJob.status' --output text)"
  echo "    ingestion status: ${STATUS}"
  case "${STATUS}" in
    COMPLETE) break ;;
    FAILED | STOPPED)
      echo "Ingestion ${STATUS}. Check the job in the Amazon Bedrock console." >&2
      exit 1
      ;;
  esac
  sleep 10
done
if [[ "${STATUS}" != "COMPLETE" ]]; then
  echo "Ingestion did not complete in time (last status ${STATUS})." >&2
  exit 1
fi

EXISTING="$(aws datasync list-agents \
  --query "Agents[?Name=='${AGENT_NAME}'].AgentArn | [0]" --output text)"
if [[ "${EXISTING}" != "None" && -n "${EXISTING}" ]]; then
  AGENT_ARN="${EXISTING}"
  echo "==> DataSync agent ${AGENT_NAME} already exists"
else
  echo "==> Waiting for the DataSync agent at ${AGENT_IP} to serve an activation key"
  KEY=""
  for _ in $(seq 1 40); do
    CANDIDATE="$(curl -fsS --max-time 10 \
      "http://${AGENT_IP}/?gatewayType=SYNC&activationRegion=${REGION}&no_redirect" 2>/dev/null || true)"
    if [[ "${CANDIDATE}" =~ ^[A-Z0-9]{5}(-[A-Z0-9]{5}){4}$ ]]; then
      KEY="${CANDIDATE}"
      break
    fi
    sleep 15
  done
  if [[ -z "${KEY}" ]]; then
    echo "Could not get an activation key. Is AgentActivationCidr set to your current public IP?" >&2
    exit 1
  fi
  echo "==> Activating the DataSync agent"
  AGENT_ARN="$(aws datasync create-agent --activation-key "${KEY}" \
    --agent-name "${AGENT_NAME}" --query AgentArn --output text)"
fi

for _ in $(seq 1 "${MAX_POLLS}"); do
  STATUS="$(aws datasync describe-agent --agent-arn "${AGENT_ARN}" --query Status --output text)"
  echo "    agent status: ${STATUS}"
  if [[ "${STATUS}" == "ONLINE" ]]; then
    break
  fi
  sleep 10
done
if [[ "${STATUS}" != "ONLINE" ]]; then
  echo "The DataSync agent did not reach ONLINE (last status ${STATUS})." >&2
  exit 1
fi

echo
echo "Done. Values to use in your prompts:"
echo "  DataSync agent ARN    : ${AGENT_ARN}"
echo "  NFS server            : $(output NfsServerPrivateIp):/exports/data"
echo "  Destination bucket ARN: $(output MigrationDestinationBucketArn)"
echo "  Knowledge Base ID     : ${KB_ID}"
