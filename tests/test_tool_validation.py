"""Unit tests for the orchestrator's tool argument validation.

Run from the repository root (no AWS credentials needed):
    python3 -m unittest discover -s tests
"""
import os
import sys
import unittest
from unittest import mock

ACCOUNT = "111122223333"
REGION = "us-east-1"
os.environ.update(
    {
        "ACCOUNT_ID": ACCOUNT,
        "AWS_REGION": REGION,
        "AWS_DEFAULT_REGION": REGION,
        "DATASYNC_S3_ROLE_ARN": f"arn:aws:iam::{ACCOUNT}:role/datasync-s3",
        "KNOWLEDGE_BASE_ID": "KB12345678",
    }
)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "orchestrator"))

import datasync_tools  # noqa: E402
import index  # noqa: E402

PREFIX = f"arn:aws:datasync:{REGION}:{ACCOUNT}:"
AGENT = PREFIX + "agent/agent-0b1c2d3e4f5a6b7c8"
LOCATION = PREFIX + "location/loc-0a1b2c3d4e5f6a7b8"
TASK = PREFIX + "task/task-0123456789abcdef0"
EXECUTION = TASK + "/execution/exec-0123456789abcdef0"
BUCKET = "arn:aws:s3:::migrationbot-destination-111122223333-us-east-1"


class ValidateTest(unittest.TestCase):
    def test_accepts_valid_values(self):
        for name, value in [
            ("agent_arn", AGENT),
            ("source_location_arn", LOCATION),
            ("destination_location_arn", LOCATION),
            ("task_arn", TASK),
            ("task_execution_arn", EXECUTION),
            ("s3_bucket_arn", BUCKET),
            ("server_hostname", "10.0.1.25"),
            ("server_hostname", "nfs.example.com"),
            ("subdirectory", "/exports/data"),
            ("subdirectory", "/"),
            ("task_name", "SampleMigration"),
        ]:
            with self.subTest(name=name, value=value):
                self.assertEqual(datasync_tools.validate(name, value), value)

    def test_rejects_path_traversal(self):
        for value in ("/exports/../etc", "../etc", "/exports/data/.."):
            with self.subTest(value=value), self.assertRaises(ValueError):
                datasync_tools.validate("subdirectory", value)

    def test_rejects_foreign_account_and_region(self):
        for name, value in [
            ("agent_arn", AGENT.replace(ACCOUNT, "444455556666")),
            ("task_arn", TASK.replace(ACCOUNT, "444455556666")),
            ("source_location_arn", LOCATION.replace(REGION, "eu-west-1")),
            ("task_execution_arn", EXECUTION.replace(REGION, "eu-west-1")),
        ]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                datasync_tools.validate(name, value)

    def test_rejects_non_strings(self):
        for value in (None, 42, ["/exports/data"], {"path": "/exports/data"}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                datasync_tools.validate("subdirectory", value)

    def test_rejects_malformed_values(self):
        for name, value in [
            ("agent_arn", AGENT + "\n"),
            ("task_arn", TASK + "/execution/exec-0123456789abcdef0"),
            ("s3_bucket_arn", "arn:aws:s3:::Bad_Bucket"),
            ("server_hostname", "10.0.1.25; rm -rf /"),
            ("subdirectory", "exports/data"),
            ("task_name", "x" * 257),
            ("task_name", ""),
        ]:
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                datasync_tools.validate(name, value)

    def test_error_does_not_echo_value(self):
        with self.assertRaises(ValueError) as caught:
            datasync_tools.validate("subdirectory", "../secret-name")
        self.assertNotIn("secret-name", str(caught.exception))


class ToolTest(unittest.TestCase):
    def test_invalid_argument_never_reaches_datasync(self):
        with mock.patch.object(datasync_tools, "datasync") as client:
            with self.assertRaises(ValueError):
                datasync_tools.create_nfs_location("10.0.1.25", "../etc", AGENT)
            client.create_location_nfs.assert_not_called()

    def test_s3_location_always_uses_stack_role(self):
        with mock.patch.object(datasync_tools, "datasync") as client:
            client.create_location_s3.return_value = {"LocationArn": LOCATION}
            datasync_tools.create_s3_location(BUCKET)
            client.create_location_s3.assert_called_once_with(
                S3BucketArn=BUCKET,
                S3Config={"BucketAccessRoleArn": os.environ["DATASYNC_S3_ROLE_ARN"]},
            )


class HandlerTest(unittest.TestCase):
    @staticmethod
    def context(tool):
        ctx = mock.Mock()
        ctx.client_context.custom = {"bedrockAgentCoreToolName": f"MigrationActions___{tool}"}
        return ctx

    def test_role_arn_override_is_ignored(self):
        with mock.patch.object(datasync_tools, "datasync") as client:
            client.create_location_s3.return_value = {"LocationArn": LOCATION}
            event = {"s3_bucket_arn": BUCKET, "role_arn": f"arn:aws:iam::{ACCOUNT}:role/admin"}
            index.lambda_handler(event, self.context("create_s3_location"))
            role = client.create_location_s3.call_args.kwargs["S3Config"]["BucketAccessRoleArn"]
            self.assertEqual(role, os.environ["DATASYNC_S3_ROLE_ARN"])

    def test_invalid_argument_is_reported_to_the_agent(self):
        with mock.patch.object(datasync_tools, "datasync"):
            result = index.lambda_handler(
                {"task_arn": TASK.replace(ACCOUNT, "444455556666")},
                self.context("start_migration"),
            )
        self.assertEqual(result, {"result": "Error executing start_migration: Invalid value for task_arn"})

    def test_non_object_event_is_rejected(self):
        result = index.lambda_handler(["not", "an", "object"], self.context("start_migration"))
        self.assertIn("must be a JSON object", result["result"])

    def test_knowledge_base_query_is_bounded(self):
        with mock.patch.object(index, "kb_runtime") as kb:
            for query in ("", "   ", "x" * (index.MAX_QUERY_CHARS + 1), 7):
                with self.subTest(query=query):
                    result = index.lambda_handler({"query": query}, self.context("search_knowledge_base"))
                    self.assertIn("Invalid value for query", result["result"])
            kb.retrieve.assert_not_called()


if __name__ == "__main__":
    unittest.main()
