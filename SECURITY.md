# Security

## Reporting Vulnerabilities

If you discover a potential security issue in this project, we ask that you notify AWS/Amazon
Security via our [vulnerability reporting page](https://aws.amazon.com/security/vulnerability-reporting/).
Please do **not** create a public GitHub issue.

## Scope

This repository is sample code for demonstration purposes. It deploys a simulated on-premises
NFS server, an AWS DataSync agent, an Amazon Bedrock AgentCore Runtime and Gateway, an AWS Lambda
orchestrator, and an Amazon Bedrock Knowledge Base backed by Amazon OpenSearch Serverless. It is
not production ready. Deploy it in an account you use for testing and delete it afterwards with
`scripts/cleanup.sh`.

## Security Controls in This Sample

- **Limited action surface.** The agent can call only the six tools published on the AgentCore
  Gateway. None of them deletes resources.
- **Tool input validation.** The orchestrator function validates every tool argument before it
  calls an AWS API. DataSync ARNs must match the DataSync format and belong to the stack's account
  and Region. Hostnames, export paths, task names and knowledge base queries must match strict
  patterns. The model cannot choose the IAM role that DataSync uses.
- **Scoped IAM.** `datasync:CreateLocationNfs` and `datasync:CreateTask` are limited to agents and
  locations in the stack's account and Region. The orchestrator can pass only the DataSync S3 role,
  and that role can reach only the destination bucket. Each IAM role has a description of its
  purpose.
- **Bounded concurrency.** The orchestrator and index creator functions have reserved concurrency
  of 5 and 1 (template parameter `LambdaReservedConcurrency`).
- **Authenticated invocation.** AgentCore Runtime invocation and the Runtime-to-Gateway connection
  both use AWS IAM (SigV4).
- **Temporary activation port.** TCP 80 on the DataSync agent is opened only to the deployer's
  public IP address during activation, and `scripts/deploy.sh` removes the rule afterwards. The
  template rejects `0.0.0.0/0` for this rule.
- **Storage.** All S3 buckets block public access, use SSE-S3, and deny requests not made over TLS.
  The template's buckets are versioned. EBS volumes are encrypted.
- **Logging.** The agent logs the length of each prompt, not its text.
- **Tagging.** `scripts/deploy.sh` tags the stack with `Project=conversational-datasync-migration`
  and `Purpose=sample`.

## Production Security Recommendations

Before using any patterns from this repository in a production environment, implement the
following controls.

### IAM and Access Control
- Once the DataSync agent exists, scope `datasync:CreateLocationNfs` and `datasync:CreateTask` to
  its ARN and to the locations you approve, and add tag-based conditions.
- Restrict who can call `bedrock-agentcore:InvokeAgentRuntime` on the Runtime.
- Review IAM role usage with AWS IAM Access Analyzer.

### Agent and Prompt Safety
- Apply Amazon Bedrock Guardrails to the agent's inputs and outputs.
- Allow only approved destination buckets and Regions in the orchestrator function.
- Add an explicit confirmation step before adding any tool that deletes or overwrites data.

### Network Security
- Activate the DataSync agent through a DataSync VPC interface endpoint from a private network.
- Place the DataSync agent and NFS server in private subnets, use VPC endpoints for DataSync, S3,
  Systems Manager and CloudWatch Logs, and remove the internet gateway.
- Use a VPC endpoint network policy for the OpenSearch Serverless collection instead of public
  access, if your Amazon Bedrock setup supports it.
- Run AgentCore Runtime in VPC mode, and use inbound JWT authentication where appropriate.
- Export the NFS share to the agent's address only, with the minimum squash options your data needs.

### Encryption and Logging
- Use customer managed AWS KMS keys for S3 buckets, CloudWatch log groups and Lambda environment
  variables where your policy requires them.
- Enable S3 server access logging, VPC Flow Logs and CloudTrail data events for the buckets and
  network that hold real data.
- Set `OpenSearchStandbyReplicas` to `ENABLED` for redundancy across Availability Zones.

### Shell Scripts
- The scripts use `set -euo pipefail`, quote their variables, and require an explicit stack-name
  confirmation in `scripts/cleanup.sh`. Validate any values you pass to them before running them in
  automation.

## Accepted Security Debt

The following items are intentional design choices that keep the sample simple to deploy and run.

| Item | Rationale | Production Recommendation |
|------|-----------|--------------------------|
| `datasync:CreateLocationS3` on `Resource: '*'` | The action's only resource type, `agent`, is used only for S3 on Outposts, so a request for a Regional bucket has no resource ARN to scope to. The orchestrator can pass only the DataSync S3 role, which reaches only the destination bucket | Add tag-based conditions and an approved-bucket check |
| OpenSearch Serverless network policy allows public access | Amazon Bedrock and the index creator function reach the collection over its public endpoint. Access is limited by IAM and a data access policy that names only the Knowledge Base role and the index creator role | Use a VPC endpoint network policy |
| Instances in a public subnet with public IP addresses | The DataSync agent needs outbound HTTPS to the DataSync public endpoint. Security groups allow only TCP 2049 from the agent to the NFS server, and TCP 80 to the agent only during activation | Use private subnets and VPC endpoints |
| HTTPS (TCP 443) and NTP (UDP 123) egress to `0.0.0.0/0` | AWS publishes no static CIDR list for the DataSync, Systems Manager and package repository endpoints | Use VPC endpoints and remove internet egress |
| AgentCore Runtime in `PUBLIC` network mode | Keeps the sample free of VPC configuration. Invocation still requires IAM authentication | Use VPC mode |
| NFS export to the VPC CIDR with `no_root_squash` | Lets DataSync read every file in the simulated share | Export to the agent address only, with the minimum squash options |
| SSE-S3 and default encryption instead of customer managed KMS keys (Checkov CKV_AWS_158, CKV_AWS_173) | Avoids key management for a short-lived sample | Use customer managed KMS keys where required |
| No S3 server access logging (Checkov CKV_AWS_18) | The buckets hold only generated sample data and one public best-practices document | Enable access logging |
| Lambda functions not in a VPC and without a dead-letter queue (Checkov CKV_AWS_117, CKV_AWS_116) | The functions call only AWS APIs and are invoked synchronously by the Gateway and CloudFormation, which receive any error directly | Run them in a VPC with endpoints if your network policy requires it |
| OpenSearch Serverless standby replicas disabled | Lowers the minimum capacity and cost of the demo collection | Set `OpenSearchStandbyReplicas` to `ENABLED` |
