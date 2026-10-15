"""CloudFormation custom resource that creates the Amazon OpenSearch Serverless
vector index used by the Bedrock Knowledge Base.

Amazon Bedrock does not create the index for you when you bring your own
OpenSearch Serverless collection, so the stack creates it here. The index
dimension must match the embedding model: Amazon Titan Text Embeddings V2
produces 1,024-dimension vectors.
"""
import hashlib
import json
import logging
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Hosts this function may call: the OpenSearch Serverless collection endpoint and
# the pre-signed Amazon S3 URL that CloudFormation provides for the response.
ALLOWED_HOST_SUFFIXES = (".amazonaws.com", ".amazonaws.com.cn", ".on.aws")

# Data access policies can take a minute or more to propagate after creation.
RETRY_SECONDS = 15
SAFETY_MARGIN_MS = 30_000
SETTLE_SECONDS = 20


def checked_https_url(url):
    """Return url only if it is an https URL on an AWS-owned host.

    urllib also opens file: and custom schemes, so every URL that reaches it is
    validated first. The values come from CloudFormation, not from end users.
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not host.endswith(ALLOWED_HOST_SUFFIXES):
        raise ValueError(f"Refusing to call unexpected URL: {url}")
    return url


def index_body(dimension):
    return {
        "settings": {"index": {"knn": True, "knn.algo_param.ef_search": 512}},
        "mappings": {
            "properties": {
                "embedding": {
                    "type": "knn_vector",
                    "dimension": dimension,
                    "method": {
                        "engine": "faiss",
                        "space_type": "l2",
                        "name": "hnsw",
                        "parameters": {},
                    },
                },
                "text": {"type": "text"},
                "metadata": {"type": "text"},
            }
        },
    }


def signed_request(method, url, body, region):
    url = checked_https_url(url)
    payload = json.dumps(body).encode() if body is not None else b""
    request = AWSRequest(
        method=method,
        url=url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            # OpenSearch Serverless requires the payload hash header.
            "x-amz-content-sha256": hashlib.sha256(payload).hexdigest(),
        },
    )
    credentials = boto3.Session().get_credentials().get_frozen_credentials()
    SigV4Auth(credentials, "aoss", region).add_auth(request)
    http_request = urllib.request.Request(
        url, data=payload or None, method=method, headers=dict(request.headers)
    )
    try:
        # URL validated by checked_https_url above.
        # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
        with urllib.request.urlopen(http_request, timeout=30) as response:  # nosec B310
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def ensure_index(endpoint, index_name, dimension, region, context):
    url = f"{endpoint.rstrip('/')}/{index_name}"
    while True:
        status, text = signed_request("PUT", url, index_body(dimension), region)
        logger.info("PUT %s -> %s", url, status)
        if status in (200, 201):
            break
        if status == 400 and "resource_already_exists_exception" in text:
            logger.info("Index already exists")
            break
        if context.get_remaining_time_in_millis() < SAFETY_MARGIN_MS:
            raise RuntimeError(f"Index creation did not succeed (last status {status}): {text}")
        time.sleep(RETRY_SECONDS)
    while True:
        status, _ = signed_request("GET", url, None, region)
        if status == 200:
            break
        if context.get_remaining_time_in_millis() < SAFETY_MARGIN_MS:
            raise RuntimeError(f"Index {index_name} is not visible (last status {status})")
        time.sleep(RETRY_SECONDS)
    time.sleep(SETTLE_SECONDS)


def send_response(event, context, status, reason, physical_id):
    body = json.dumps(
        {
            "Status": status,
            "Reason": reason,
            "PhysicalResourceId": physical_id,
            "StackId": event["StackId"],
            "RequestId": event["RequestId"],
            "LogicalResourceId": event["LogicalResourceId"],
            "Data": {},
        }
    ).encode()
    response_url = checked_https_url(event["ResponseURL"])
    request = urllib.request.Request(
        response_url, data=body, method="PUT", headers={"Content-Type": ""}
    )
    # URL validated by checked_https_url above.
    # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
    with urllib.request.urlopen(request, timeout=30):  # nosec B310
        pass


def handler(event, context):
    logger.info("Request type: %s", event["RequestType"])
    props = event["ResourceProperties"]
    physical_id = event.get("PhysicalResourceId") or f"{props['IndexName']}"
    try:
        if event["RequestType"] in ("Create", "Update"):
            ensure_index(
                props["CollectionEndpoint"],
                props["IndexName"],
                int(props["Dimension"]),
                props["Region"],
                context,
            )
            physical_id = props["IndexName"]
        # Delete: nothing to do. The index is removed with the collection.
        send_response(event, context, "SUCCESS", "OK", physical_id)
    except Exception as exc:
        logger.exception("Custom resource failed")
        send_response(event, context, "FAILED", str(exc)[:500], physical_id)
