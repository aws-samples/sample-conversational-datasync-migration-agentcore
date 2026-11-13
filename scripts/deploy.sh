#!/usr/bin/env bash
# Build, package, deploy and finish setting up the MigrationBot sample.
#
# Usage: scripts/deploy.sh [stack-name]
#
# Requires: AWS CLI, curl, zip, and python3 with pip (to build the arm64 agent package).
#
# Environment variables (all optional):
#   ARTIFACT_BUCKET   S3 bucket for the Lambda code and the agent package. If
#                     unset, a private bucket named <stack>-artifacts-<account>-<region>
#                     is created.
#   ACTIVATION_CIDR   CIDR allowed to reach the DataSync agent on TCP 80 during
#                     activation. Defaults to your public IP (/32).
#   BEDROCK_MODEL_ID  Overrides the BedrockModelId template parameter.
#   LAMBDA_RESERVED_CONCURRENCY
#                     ENABLED or DISABLED. If unset, the script checks the
#                     account's Lambda concurrency and uses ENABLED when the
#                     reservation fits, DISABLED (with a warning) otherwise.
#   AWS_REGION        Deployment Region (us-east-1 or us-west-2).

set -euo pipefail

STACK_NAME="${1:-${STACK_NAME:-migrationbot}}"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region || true)}}"
if [[ -z "${REGION}" ]]; then
  echo "Set AWS_REGION or configure a default region." >&2
  exit 1
fi
export AWS_DEFAULT_REGION="${REGION}"

if [[ "${REGION}" != "us-east-1" && "${REGION}" != "us-west-2" ]]; then
  echo "Warning: this sample targets us-east-1 and us-west-2; ${REGION} is untested." >&2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${SCRIPT_DIR}/.."
PACKAGED="${ROOT}/.packaged-template.yaml"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
echo "Deploying stack '${STACK_NAME}' to account ${ACCOUNT_ID} in ${REGION}"

if [[ -z "${ACTIVATION_CIDR:-}" ]]; then
  MY_IP="$(curl -fsS --max-time 10 https://checkip.amazonaws.com | tr -d '[:space:]')"
  ACTIVATION_CIDR="${MY_IP}/32"
fi
echo "Agent activation (TCP 80) will be allowed from ${ACTIVATION_CIDR} until activation completes"

if [[ -z "${ARTIFACT_BUCKET:-}" ]]; then
  ARTIFACT_BUCKET="${STACK_NAME}-artifacts-${ACCOUNT_ID}-${REGION}"
  if ! aws s3api head-bucket --bucket "${ARTIFACT_BUCKET}" 2>/dev/null; then
    echo "Creating artifact bucket ${ARTIFACT_BUCKET}"
    if [[ "${REGION}" == "us-east-1" ]]; then
      aws s3api create-bucket --bucket "${ARTIFACT_BUCKET}" >/dev/null
    else
      aws s3api create-bucket --bucket "${ARTIFACT_BUCKET}" \
        --create-bucket-configuration "LocationConstraint=${REGION}" >/dev/null
    fi
    aws s3api put-public-access-block --bucket "${ARTIFACT_BUCKET}" \
      --public-access-block-configuration \
      BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
    # The bucket holds the code that Lambda and AgentCore Runtime run, so
    # refuse any request that is not made over TLS.
    aws s3api put-bucket-policy --bucket "${ARTIFACT_BUCKET}" --policy "{
      \"Version\": \"2012-10-17\",
      \"Statement\": [{
        \"Sid\": \"DenyInsecureTransport\", \"Effect\": \"Deny\", \"Principal\": \"*\",
        \"Action\": \"s3:*\",
        \"Resource\": [\"arn:aws:s3:::${ARTIFACT_BUCKET}\", \"arn:aws:s3:::${ARTIFACT_BUCKET}/*\"],
        \"Condition\": {\"Bool\": {\"aws:SecureTransport\": \"false\"}}
      }]
    }"
  fi
fi

# Build the AgentCore Runtime package. Runtime runs on arm64 and does not install
# dependencies at start-up, so the zip must contain arm64 wheels for Python 3.12.
# The object key is derived from the inputs, so unchanged code is not re-uploaded
# and the Runtime is only updated when the agent or its dependencies change.
if command -v sha256sum >/dev/null 2>&1; then
  HASH_CMD=(sha256sum)
else
  HASH_CMD=(shasum -a 256)
fi
AGENT_HASH="$(cat "${ROOT}/agent/migrationbot.py" "${ROOT}/agent/requirements.txt" \
  | "${HASH_CMD[@]}" | cut -c1-16)"
AGENT_KEY="agent/${STACK_NAME}-${AGENT_HASH}.zip"
if aws s3api head-object --bucket "${ARTIFACT_BUCKET}" --key "${AGENT_KEY}" >/dev/null 2>&1; then
  echo "==> Agent package ${AGENT_KEY} already uploaded"
else
  echo "==> Building the agent package (arm64, Python 3.12)"
  BUILD_DIR="$(mktemp -d)"
  trap 'rm -rf "${BUILD_DIR}"' EXIT
  python3 -m pip install --quiet --target "${BUILD_DIR}/package" \
    --platform manylinux2014_aarch64 --only-binary=:all: \
    --python-version 3.12 --implementation cp \
    -r "${ROOT}/agent/requirements.txt"
  rm -rf "${BUILD_DIR}/package/bin"
  find "${BUILD_DIR}/package" -name '__pycache__' -type d -prune -exec rm -rf {} +
  cp "${ROOT}/agent/migrationbot.py" "${BUILD_DIR}/package/"
  (cd "${BUILD_DIR}/package" && zip -qr -X "${BUILD_DIR}/agent.zip" .)
  aws s3 cp "${BUILD_DIR}/agent.zip" "s3://${ARTIFACT_BUCKET}/${AGENT_KEY}" --only-show-errors
fi

aws cloudformation package \
  --template-file "${ROOT}/template.yaml" \
  --s3-bucket "${ARTIFACT_BUCKET}" \
  --output-template-file "${PACKAGED}" >/dev/null

PARAMS=(
  "ResourcePrefix=${STACK_NAME}"
  "AgentActivationCidr=${ACTIVATION_CIDR}"
  "AgentCodeBucket=${ARTIFACT_BUCKET}"
  "AgentCodeKey=${AGENT_KEY}"
)
if [[ -n "${BEDROCK_MODEL_ID:-}" ]]; then
  PARAMS+=("BedrockModelId=${BEDROCK_MODEL_ID}")
fi

# Reserved concurrency (5 for the orchestrator, 1 for the index creator) caps
# how many tool calls can run at once. Lambda rejects a reservation that would
# leave the account with less unreserved concurrency than its minimum, which is
# 100, or the whole limit in accounts whose limit is below 100 (new accounts can
# start at 10). Unless LAMBDA_RESERVED_CONCURRENCY is set, check the account and
# turn the reservation off only when it cannot fit.
RESERVED_NEEDED=6
if [[ -z "${LAMBDA_RESERVED_CONCURRENCY:-}" ]]; then
  read -r LAMBDA_LIMIT LAMBDA_UNRESERVED < <(aws lambda get-account-settings \
    --query '[AccountLimit.ConcurrentExecutions,AccountLimit.UnreservedConcurrentExecutions]' \
    --output text)
  if [[ ! "${LAMBDA_LIMIT:-}" =~ ^[0-9]+$ || ! "${LAMBDA_UNRESERVED:-}" =~ ^[0-9]+$ ]]; then
    echo "Could not read the Lambda concurrency limit. Set LAMBDA_RESERVED_CONCURRENCY to ENABLED or DISABLED." >&2
    exit 1
  fi
  # On a stack update, this stack's own reservations are already subtracted
  # from the unreserved pool, so add them back before comparing.
  for fn in MigrationOrchestratorFunction IndexCreatorFunction; do
    fn_name="$(aws cloudformation describe-stack-resource --stack-name "${STACK_NAME}" \
      --logical-resource-id "${fn}" --query StackResourceDetail.PhysicalResourceId \
      --output text 2>/dev/null || true)"
    if [[ -n "${fn_name}" && "${fn_name}" != "None" ]]; then
      fn_reserved="$(aws lambda get-function-concurrency --function-name "${fn_name}" \
        --query ReservedConcurrentExecutions --output text 2>/dev/null || true)"
      if [[ "${fn_reserved}" =~ ^[0-9]+$ ]]; then
        LAMBDA_UNRESERVED=$((LAMBDA_UNRESERVED + fn_reserved))
      fi
    fi
  done
  LAMBDA_MIN_UNRESERVED=$((LAMBDA_LIMIT < 100 ? LAMBDA_LIMIT : 100))
  if ((LAMBDA_UNRESERVED - RESERVED_NEEDED >= LAMBDA_MIN_UNRESERVED)); then
    LAMBDA_RESERVED_CONCURRENCY=ENABLED
  else
    LAMBDA_RESERVED_CONCURRENCY=DISABLED
    echo "Warning: this account's Lambda concurrency limit is ${LAMBDA_LIMIT} (${LAMBDA_UNRESERVED} unreserved)," >&2
    echo "  too low to reserve ${RESERVED_NEEDED}. Deploying without reserved concurrency. Request a" >&2
    echo "  Lambda concurrent executions quota increase and re-run to enable it." >&2
  fi
fi
case "${LAMBDA_RESERVED_CONCURRENCY}" in
  ENABLED | DISABLED) ;;
  *)
    echo "LAMBDA_RESERVED_CONCURRENCY must be ENABLED or DISABLED." >&2
    exit 1
    ;;
esac
echo "Lambda reserved concurrency: ${LAMBDA_RESERVED_CONCURRENCY}"
PARAMS+=("LambdaReservedConcurrency=${LAMBDA_RESERVED_CONCURRENCY}")

# Stack tags propagate to the resources CloudFormation creates, so leftover
# resources can be found and attributed to this sample.
TAGS=(
  "Project=conversational-datasync-migration"
  "Purpose=sample"
)

echo "==> Deploying CloudFormation stack (this takes several minutes)"
aws cloudformation deploy \
  --template-file "${PACKAGED}" \
  --stack-name "${STACK_NAME}" \
  --capabilities CAPABILITY_IAM \
  --no-fail-on-empty-changeset \
  --parameter-overrides "${PARAMS[@]}" \
  --tags "${TAGS[@]}"

"${SCRIPT_DIR}/post-deploy.sh" "${STACK_NAME}"

echo "==> Closing TCP 80 on the DataSync agent security group"
CLOSE_PARAMS=("${PARAMS[@]}")
CLOSE_PARAMS[1]="AgentActivationCidr="
aws cloudformation deploy \
  --template-file "${PACKAGED}" \
  --stack-name "${STACK_NAME}" \
  --capabilities CAPABILITY_IAM \
  --no-fail-on-empty-changeset \
  --parameter-overrides "${CLOSE_PARAMS[@]}" \
  --tags "${TAGS[@]}"

echo
echo "MigrationBot is ready. Start a conversation with:"
echo "  python3 scripts/chat.py ${STACK_NAME}"
echo "See README.md for example prompts."
