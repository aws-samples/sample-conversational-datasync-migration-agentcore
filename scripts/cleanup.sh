#!/usr/bin/env bash
# Remove everything the MigrationBot sample created, including resources the
# bot created outside CloudFormation (DataSync agent, locations, and tasks).
#
# Usage: scripts/cleanup.sh [stack-name] [-y]
#
# Only DataSync resources that belong to this sample are deleted:
#   - the agent named <stack>-agent
#   - locations that point at the sample NFS server or destination bucket
#   - tasks that use those locations
# The AgentCore Runtime log groups and the packaged-code artifact bucket created
# by deploy.sh are also deleted. A bucket you supplied through ARTIFACT_BUCKET is
# left alone (delete its agent/<stack>-*.zip and Lambda code objects yourself).

set -euo pipefail

STACK_NAME="migrationbot"
ASSUME_YES="false"
for arg in "$@"; do
  case "${arg}" in
    -y | --yes) ASSUME_YES="true" ;;
    *) STACK_NAME="${arg}" ;;
  esac
done

REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region || true)}}"
if [[ -z "${REGION}" ]]; then
  echo "Set AWS_REGION or configure a default region." >&2
  exit 1
fi
export AWS_DEFAULT_REGION="${REGION}"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
ARTIFACT_BUCKET="${STACK_NAME}-artifacts-${ACCOUNT_ID}-${REGION}"

output() {
  aws cloudformation describe-stacks --stack-name "${STACK_NAME}" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue | [0]" --output text
}

if ! aws cloudformation describe-stacks --stack-name "${STACK_NAME}" >/dev/null 2>&1; then
  echo "Stack ${STACK_NAME} was not found in ${REGION}. Nothing to clean up." >&2
  exit 1
fi

DEST_BUCKET="$(output MigrationDestinationBucketName)"
KB_BUCKET="$(output KBDocsBucketName)"
NFS_IP="$(output NfsServerPrivateIp)"

echo "This will permanently delete, in account ${ACCOUNT_ID} (${REGION}):"
echo "  - stack ${STACK_NAME} and all of its resources"
echo "  - ALL objects and versions in s3://${DEST_BUCKET} and s3://${KB_BUCKET}"
echo "  - the DataSync agent ${STACK_NAME}-agent, plus sample locations and tasks"
echo "  - the artifact bucket s3://${ARTIFACT_BUCKET} (if it exists)"
if [[ "${ASSUME_YES}" != "true" ]]; then
  read -r -p "Type the stack name to continue: " CONFIRM
  if [[ "${CONFIRM}" != "${STACK_NAME}" ]]; then
    echo "Aborted."
    exit 1
  fi
fi

empty_bucket() {
  local bucket="$1" kind objs
  for kind in Versions DeleteMarkers; do
    while :; do
      objs="$(aws s3api list-object-versions --bucket "${bucket}" \
        --query "{Objects: ${kind}[:1000].{Key:Key,VersionId:VersionId}}" --output json)"
      grep -q '"Key"' <<<"${objs}" || break
      aws s3api delete-objects --bucket "${bucket}" --delete "${objs}" >/dev/null
    done
  done
}

echo "==> Deleting DataSync tasks and locations created for this sample"
LOCATION_ARNS="$(aws datasync list-locations --output text \
  --query "Locations[?starts_with(LocationUri, 'nfs://${NFS_IP}/') || starts_with(LocationUri, 's3://${DEST_BUCKET}')].LocationArn")"
for task in $(aws datasync list-tasks --query 'Tasks[].TaskArn' --output text); do
  [[ "${task}" == "None" ]] && continue
  ends="$(aws datasync describe-task --task-arn "${task}" \
    --query '[SourceLocationArn,DestinationLocationArn]' --output text)"
  for loc in ${ends}; do
    if grep -qwF -- "${loc}" <<<"${LOCATION_ARNS}"; then
      aws datasync delete-task --task-arn "${task}" || echo "Could not delete ${task}" >&2
      break
    fi
  done
done
for loc in ${LOCATION_ARNS}; do
  [[ "${loc}" == "None" ]] && continue
  aws datasync delete-location --location-arn "${loc}" || echo "Could not delete ${loc}" >&2
done

AGENT_ARN="$(aws datasync list-agents \
  --query "Agents[?Name=='${STACK_NAME}-agent'].AgentArn | [0]" --output text)"
if [[ "${AGENT_ARN}" != "None" && -n "${AGENT_ARN}" ]]; then
  aws datasync delete-agent --agent-arn "${AGENT_ARN}"
fi

echo "==> Emptying buckets"
empty_bucket "${DEST_BUCKET}"
empty_bucket "${KB_BUCKET}"

echo "==> Deleting stack ${STACK_NAME}"
aws cloudformation delete-stack --stack-name "${STACK_NAME}"
aws cloudformation wait stack-delete-complete --stack-name "${STACK_NAME}"

RUNTIME_NAME="${STACK_NAME//-/_}_MigrationBot"
for group in $(aws logs describe-log-groups \
  --log-group-name-prefix "/aws/bedrock-agentcore/runtimes/${RUNTIME_NAME}-" \
  --query 'logGroups[].logGroupName' --output text); do
  [[ "${group}" == "None" ]] && continue
  echo "==> Deleting log group ${group}"
  aws logs delete-log-group --log-group-name "${group}"
done

if aws s3api head-bucket --bucket "${ARTIFACT_BUCKET}" 2>/dev/null; then
  echo "==> Deleting artifact bucket ${ARTIFACT_BUCKET}"
  aws s3 rb "s3://${ARTIFACT_BUCKET}" --force
fi

echo "Cleanup complete."
