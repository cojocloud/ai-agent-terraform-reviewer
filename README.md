# AI Terraform Review Agent

An automated Terraform PR security reviewer that uses **Terrascan + Google Gemini** as a CI/CD security gate. When a pull request is opened against any Terraform in this repository, a GitHub Actions workflow scans the infrastructure code, invokes an AWS Lambda AI agent, and either approves or rejects the PR based on risk thresholds — with a structured reason and remediation steps on rejection.

---

## Architecture Overview

```
Pull Request
     │
     ▼
GitHub Actions
     │
     ├── Terrascan scan (IaC static analysis)
     │        │
     │        └── terrascan_report.json
     │
     └── AWS Lambda (AI Review Agent)
              │
              ├── Extracts violations from Terrascan output
              ├── Calls Google Gemini API (gemini-2.5-flash)
              ├── Applies risk-based decision policy
              │
              └── Returns:
                   ├── verdict: APPROVE | APPROVE_WITH_CHANGES | REJECT
                   ├── rejection_reason  (on REJECT)
                   ├── remediation       (on REJECT)
                   └── ai_review         (full human-readable report)
```

The application itself is a Mario game container running on **AWS ECS Fargate**, served over HTTPS via an **Application Load Balancer** with a custom domain managed in **Route 53**.

---

## Project Structure

```
.
├── .github/
│   └── workflows/
│       └── main.yml                  # CI/CD pipeline
└── terraform-review-agent/
    ├── lambda/
    │   └── lambda_function.py        # AI review agent (Python 3.11)
    └── terraform/
        ├── alb.tf                    # ALB, HTTPS listener, ACM cert, Route 53 records
        ├── ecs.tf                    # ECS Fargate cluster, task, service
        ├── iam.tf                    # IAM roles and policies
        ├── lambda.tf                 # Lambda function resource
        ├── networking.tf             # VPC module
        ├── security.tf               # Security groups
        ├── secrets.tf                # Secrets Manager (Gemini API key)
        ├── backend.tf                # S3 remote state with locking
        ├── variables.tf
        └── outputs.tf
```

---

## CI/CD Pipeline

Every pull request that touches `terraform-review-agent/**` triggers the workflow:

1. **Checkout** — clone the PR branch
2. **Configure AWS credentials** — using GitHub Actions secrets
3. **Install Terrascan** — v1.18.3
4. **Run Terrascan** — scans the `terraform/` directory against AWS policies, outputs `terrascan_report.json`
5. **Invoke Lambda** — sends the Terrascan JSON to the AI review Lambda
6. **Enforce verdict** — exits with code 1 (fails the PR) if verdict is `REJECT`

---

## AI Decision Policy

The Lambda applies a strict, deterministic policy on top of Gemini's analysis:

| Condition | Verdict |
|---|---|
| Any HIGH or CRITICAL severity finding | REJECT |
| 4 or more MEDIUM severity findings | REJECT |
| ALB has no HTTPS listener at all | REJECT |
| 1–3 MEDIUM severity findings | APPROVE_WITH_CHANGES |
| Only LOW or INFO findings | APPROVE |

On `REJECT`, the response always includes:
- `rejection_reason` — the exact policy rule violated
- `remediation` — concrete Terraform steps to fix it

---

## Infrastructure

### Application (ECS Fargate)
- **Container:** `sevenajay/mario:latest` on port 80
- **Cluster:** `mario-game-cluster`
- **CPU/Memory:** 256 vCPU / 512 MB
- **Networking:** public subnets, ALB as ingress

### Load Balancer (ALB)
- Port 80 → 301 redirect to HTTPS
- Port 443 → forward to ECS target group
- SSL policy: `ELBSecurityPolicy-2016-08`
- Certificate: ACM-issued for `mario.cojocloudsolutions.com`

### DNS & TLS
- Hosted zone: `cojocloudsolutions.com` (Route 53)
- ACM certificate validated via DNS (CNAME record auto-created by Terraform)
- `aws_acm_certificate_validation` ensures Terraform waits for issuance before attaching the cert to the ALB

### Lambda
- Runtime: Python 3.11
- Timeout: 60 seconds
- Gemini API key stored in AWS Secrets Manager, fetched at cold start and cached in memory

### State Backend
- S3 bucket: `my-aws-bucket-backend`
- Key: `ecs/serverless/terraform.tfstate`
- Encryption enabled, native S3 lock file

---

## Prerequisites

- AWS account with permissions for ECS, Lambda, ALB, ACM, Route 53, Secrets Manager, S3
- Route 53 hosted zone for your domain
- Google Gemini API key
- Terraform >= 1.9
- GitHub Actions secrets configured:
  - `AWS_ACCESS_KEY_ID`
  - `AWS_SECRET_ACCESS_KEY`

---

## Setup

```bash
# 1. Package the Lambda function
cd terraform-review-agent/lambda
zip lambda.zip lambda_function.py

# 2. Initialise Terraform
cd ../terraform
terraform init

# 3. Apply (will prompt for gemini_api_key3)
terraform apply
```

### Required Terraform variables

| Variable | Description |
|---|---|
| `project_name` | Resource name prefix (default: `mario-game`) |
| `gemini_api_key3` | Your Google Gemini API key (sensitive) |

---

## Teardown

```bash
cd terraform-review-agent/terraform
terraform destroy
```

---

## Live App

`https://mario.cojocloudsolutions.com`

---

## Tech Stack

| Layer | Technology |
|---|---|
| IaC | Terraform |
| Cloud | AWS (Lambda, ECS Fargate, ALB, ACM, Route 53, Secrets Manager, IAM, VPC) |
| CI/CD | GitHub Actions |
| Static analysis | Terrascan v1.18.3 |
| AI model | Google Gemini 2.5 Flash |
| Container | Docker (sevenajay/mario) |
| State backend | S3 + native lock file |
