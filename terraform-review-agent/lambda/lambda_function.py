import json
import re
import urllib.request
import urllib.error
import boto3

GEMINI_MODEL = "gemini-2.5-flash"
SECRET_NAME = "gemini-api-key0"
REGION_NAME = "us-east-1"

GEMINI_API_KEY = None


def get_gemini_api_key():
    global GEMINI_API_KEY

    if GEMINI_API_KEY:
        return GEMINI_API_KEY

    client = boto3.client("secretsmanager", region_name=REGION_NAME)
    response = client.get_secret_value(SecretId=SECRET_NAME)
    secret = json.loads(response["SecretString"])

    GEMINI_API_KEY = secret["GEMINI_API_KEY"]
    return GEMINI_API_KEY


def extract_relevant_findings(terrascan_results: dict) -> dict:
    violations = terrascan_results.get("violations", [])
    summary = terrascan_results.get("scan_summary", {})

    structured = {
        "summary": {
            "total_violations": summary.get("violated_policies", 0),
            "high": summary.get("high", 0),
            "medium": summary.get("medium", 0),
            "low": summary.get("low", 0),
        },
        "violations": [],
    }

    for v in violations:
        structured["violations"].append(
            {
                "rule_id": v.get("rule_id"),
                "rule_name": v.get("rule_name"),
                "severity": v.get("severity"),
                "description": v.get("description"),
                "resource_type": v.get("resource_type"),
                "resource_name": v.get("resource_name"),
                "file": v.get("file"),
                "line": v.get("line"),
            }
        )

    return structured


def build_prompt(findings: dict, terraform_context: dict) -> str:
    https_count = terraform_context.get("https_listener_count", 0)
    redirect_count = terraform_context.get("http_redirect_count", 0)

    if https_count > 0:
        https_status = (
            f"VERIFIED: The Terraform code contains {https_count} HTTPS listener(s) "
            f"and {redirect_count} HTTP→HTTPS redirect listener(s). "
            f"The ALB IS correctly secured with HTTPS. "
            f"AC_AWS_0491 violations on HTTP redirect listeners are false positives — ignore them."
        )
    else:
        https_status = (
            "VERIFIED: No HTTPS listener found in the Terraform code. "
            "The ALB is NOT secured with HTTPS. Apply the 'no HTTPS listener' rejection rule."
        )

    return f"""
You are a senior DevOps and Terraform security reviewer acting as a CI/CD security gate.

Your task is to analyze Terrascan findings and decide whether the infrastructure
can be deployed based on **risk thresholds**, not perfection.

VERIFIED TERRAFORM CONTEXT (authoritative — use this to override Terrascan inferences):
{https_status}

Decision Policy (STRICT)
- REJECT if:
  - Any HIGH or CRITICAL severity issue exists
  - OR MEDIUM severity issues ≥ 4
  - OR Application Load Balancer has no HTTPS listener (use the VERIFIED CONTEXT above, not Terrascan AC_AWS_0491)
- APPROVE_WITH_CHANGES if:
  - MEDIUM severity issues are 1–3
- APPROVE if:
  - Only LOW or INFO issues exist

Output Format
Provide the following sections IN ORDER and use the exact headings shown:

1. 🚨 Security issues ordered by severity (summary only)
2. 🛠 Required remediation (only actionable items)
3. ⚖️ Risk justification (1–2 lines)
4. 📌 Final verdict: APPROVE | APPROVE_WITH_CHANGES | REJECT

If the verdict is REJECT, you MUST also include these two additional sections:

REJECTION_REASON:
<one or two sentences stating the exact policy rule that was violated — e.g. missing HTTPS listener, HIGH severity finding, etc.>

REMEDIATION:
<concrete, step-by-step Terraform fix — include resource names and attribute names where relevant>

Rules:
- Be concise
- Use bullet points inside each section
- Focus on AWS (ALB, ECS, VPC, IAM)
- Ignore Terrascan scan_errors
- Do NOT repeat raw JSON
- Verdict must strictly follow the Decision Policy
- REJECTION_REASON and REMEDIATION sections are mandatory when verdict is REJECT

Findings:
{json.dumps(findings, indent=2)}
"""


def call_gemini(prompt: str) -> str:
    api_key = get_gemini_api_key()

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={api_key}"
    )

    payload = {"contents": [{"parts": [{"text": prompt}]}]}

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            result = json.loads(response.read())
            return result["candidates"][0]["content"]["parts"][0]["text"]

    except urllib.error.HTTPError as e:
        return f"Gemini API HTTP error: {e.read().decode()}"

    except Exception as e:
        return f"Unexpected error calling Gemini: {str(e)}"


def extract_verdict(review_text: str) -> str:
    """
    Extracts the verdict from the line immediately following 'Final verdict:'.
    Uses regex so the decision policy text (which also contains REJECT/APPROVE)
    cannot pollute the result.  Defaults to REJECT if the pattern is not found.
    """
    match = re.search(
        r"final\s+verdict[^:\n]*:\s*\*{0,2}(APPROVE_WITH_CHANGES|APPROVE|REJECT)\*{0,2}",
        review_text,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).upper()

    # Fail-safe: never allow silent approval
    return "REJECT"


def _extract_section(review_text: str, section_header: str) -> str | None:
    """
    Extracts the text content that follows `section_header:` up to the next
    blank line or end of string.  Returns None if the header is not found.
    Strips markdown bold/italic markers (**) before matching so Gemini's
    formatted output (e.g. **REJECTION_REASON:**) is handled correctly.
    """
    lines = review_text.splitlines()
    result_lines = []
    capturing = False

    for line in lines:
        # Remove markdown bold/italic so **HEADER:** matches as HEADER:
        clean = re.sub(r"\*+", "", line).strip()

        if clean.upper().startswith(section_header.upper() + ":"):
            capturing = True
            inline = clean.split(":", 1)[1].strip()
            if inline:
                result_lines.append(inline)
            continue

        if capturing:
            if line.strip() == "":
                break
            result_lines.append(line.strip())

    return "\n".join(result_lines).strip() if result_lines else None


def extract_rejection_reason(review_text: str) -> str | None:
    return _extract_section(review_text, "REJECTION_REASON")


def extract_remediation(review_text: str) -> str | None:
    return _extract_section(review_text, "REMEDIATION")


def lambda_handler(event, context):
    try:
        results = event.get("results")
        if not results:
            return {"statusCode": 400, "error": "Missing Terrascan results in payload"}

        terraform_context = event.get("terraform_context", {})
        findings = extract_relevant_findings(results)
        prompt = build_prompt(findings, terraform_context)

        ai_review = call_gemini(prompt)
        verdict = extract_verdict(ai_review)

        response = {
            "statusCode": 200,
            "verdict": verdict,
            "summary": findings["summary"],
            "ai_review": ai_review,
        }

        if verdict == "REJECT":
            rejection_reason = extract_rejection_reason(ai_review)
            remediation = extract_remediation(ai_review)

            # Fallback if Gemini omitted the structured sections
            if not rejection_reason:
                rejection_reason = (
                    "The Application Load Balancer has no HTTPS listener configured. "
                    "All traffic is served over unencrypted HTTP, violating the security policy."
                )
            if not remediation:
                remediation = (
                    'Add an aws_lb_listener resource with protocol = "HTTPS" and a valid '
                    "certificate_arn. Optionally add a second listener on port 80 that redirects "
                    "to HTTPS using a redirect action."
                )

            response["rejection_reason"] = rejection_reason
            response["remediation"] = remediation

        return response

    except Exception as e:
        return {"statusCode": 500, "verdict": "REJECT", "error": str(e)}  # fail closed
