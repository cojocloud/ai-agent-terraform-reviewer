# Challenges & Fixes

A record of real issues encountered while building and deploying the AI Terraform Review Agent, and exactly how each one was resolved.

---

## Challenge 1 — ACM Certificate Attached Before Validation

### Error
```
Error: creating ELBv2 Listener: UnsupportedCertificate: The certificate
'arn:aws:acm:us-east-1:...' must have a fully-qualified domain name,
a supported signature, and a supported key size.
```

### Root Cause
The HTTPS listener was wired directly to `aws_acm_certificate.mario_cert.arn`:

```hcl
certificate_arn = aws_acm_certificate.mario_cert.arn
```

`aws_acm_certificate` is created the moment Terraform calls the ACM API, but the certificate is in `PENDING_VALIDATION` state at that point. AWS refuses to attach an unissued certificate to an ALB listener.

### Fix
Three resources were added to enforce the correct ordering:

```hcl
# 1. Create the DNS validation CNAME record in Route 53
resource "aws_route53_record" "mario_cert_validation" {
  for_each = {
    for dvo in aws_acm_certificate.mario_cert.domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  }
  zone_id = data.aws_route53_zone.main.zone_id
  name    = each.value.name
  type    = each.value.type
  records = [each.value.record]
  ttl     = 60
}

# 2. Block until ACM marks the certificate ISSUED
resource "aws_acm_certificate_validation" "mario_cert" {
  certificate_arn         = aws_acm_certificate.mario_cert.arn
  validation_record_fqdns = [for record in aws_route53_record.mario_cert_validation : record.fqdn]
}

# 3. Reference the validated cert, not the raw cert resource
resource "aws_lb_listener" "https" {
  certificate_arn = aws_acm_certificate_validation.mario_cert.certificate_arn
  ...
}
```

**Key insight:** `aws_acm_certificate_validation.certificate_arn` and `aws_acm_certificate.arn` output the same ARN value — but referencing the validation resource creates a hard Terraform dependency that forces the listener to wait until the cert is truly issued.

---

## Challenge 2 — DuplicateListener on Port 80

### Error
```
Error: creating ELBv2 Listener: DuplicateListener: A listener already exists
on this port for this load balancer
  with aws_lb_listener.http, on alb.tf line 62
```

### Root Cause
The original Terraform file had a port 80 listener named `aws_lb_listener.app_listener`. During a refactor it was renamed to `aws_lb_listener.http`. Terraform treats a resource rename as destroy-old + create-new.

The listener had been applied to AWS at some point but its state entry had been lost (state drift). Terraform had no record of the old listener to destroy — it jumped straight to creating the new one. AWS rejected it because a port 80 listener already existed on the ALB.

### Fix
The stale untracked listener was identified using:

```bash
aws elbv2 describe-listeners \
  --load-balancer-arn <alb-arn> \
  --region us-east-1 \
  --output json
```

The listener had already been cleaned up by the time investigation completed (the partially-failed apply ran its destroy step). A subsequent `terraform apply` created `aws_lb_listener.http` cleanly.

**Lesson:** When renaming a Terraform resource that already exists in AWS, use `terraform state mv <old> <new>` to transfer the state entry without destroying and recreating the real resource.

---

## Challenge 3 — App Domain Not Resolving

### Symptom
`https://mario.cojocloudsolutions.com` showed "connection not secure" / could not connect, even though the ACM certificate was `ISSUED` and a CNAME record was visible in Route 53.

### Root Cause
The CNAME record visible in Route 53 was the **ACM DNS validation record** (`_xyz.mario.cojocloudsolutions.com`), not an app record. It proves domain ownership to ACM — it does not route traffic.

No Route 53 record existed to resolve `mario.cojocloudsolutions.com` to the ALB:

```bash
aws route53 list-resource-record-sets \
  --hosted-zone-id Z09634432V4R01XN9AQK7 \
  --query "ResourceRecordSets[?Name=='mario.cojocloudsolutions.com.']"
# result: []
```

### Fix
Added an alias A record pointing the domain to the ALB:

```hcl
resource "aws_route53_record" "mario_app" {
  zone_id = data.aws_route53_zone.main.zone_id
  name    = "mario.cojocloudsolutions.com"
  type    = "A"

  alias {
    name                   = aws_lb.app_alb.dns_name
    zone_id                = aws_lb.app_alb.zone_id
    evaluate_target_health = true
  }
}
```

An alias A record is preferred over a CNAME for apex/subdomain-to-ALB mappings because it is free, resolves faster, and supports health-check evaluation.

After `terraform apply`, the domain resolved immediately via Route 53's global anycast network.

---

## Challenge 4 — Local DNS Cache Masking a Live App

### Symptom
After the Route 53 A record was created and `terraform apply` returned success, `https://mario.cojocloudsolutions.com` still failed to load in the browser. `terraform apply` reported "No changes."

### Diagnosis
```bash
# Local resolver — returned nothing
dig mario.cojocloudsolutions.com +short

# Google's public resolver — returned IPs correctly
dig mario.cojocloudsolutions.com @8.8.8.8 +short
# 34.193.73.215
# 100.50.216.41

# Direct connectivity test bypassing DNS
curl --resolve mario.cojocloudsolutions.com:443:34.193.73.215 \
  https://mario.cojocloudsolutions.com
# HTTP/2 200 — cert valid, app responding
```

The infrastructure was fully correct. The local DNS resolver had a stale negative cache entry.

### Fix
Flush the local DNS cache:

```bash
# macOS
sudo dscacheutil -flushcache && sudo killall -HUP mDNSResponder

# Windows
ipconfig /flushdns
```

In Chrome, `chrome://net-internals/#dns` → "Clear host cache" also works.

**Lesson:** Always verify the infrastructure layer independently (using `--resolve` or `@8.8.8.8`) before concluding the infrastructure is broken. DNS propagation and local caching are separate concerns from Terraform correctness.

---

## Challenge 5 — Lambda Returned Verdict Without Reason or Remediation

### Symptom
When the AI agent rejected a PR, the GitHub Actions log showed the verdict (`REJECT`) but gave no indication of *why* the PR was rejected or how to fix it. Engineers had to read the raw `ai_review` text to find the issue.

### Root Cause
The Lambda response only included `verdict`, `summary`, and `ai_review` (a free-form string). There were no structured fields for the rejection reason or remediation steps. The Gemini prompt also did not explicitly require these sections.

### Fix
The prompt was updated to require two additional output sections when the verdict is REJECT:

```
REJECTION_REASON:
<one or two sentences stating the exact policy rule violated>

REMEDIATION:
<concrete, step-by-step Terraform fix>
```

Two extraction helpers were added to parse these sections from the AI response:

```python
def _extract_section(review_text: str, section_header: str) -> str | None:
    lines = review_text.splitlines()
    result_lines = []
    capturing = False
    for line in lines:
        if line.strip().upper().startswith(section_header.upper() + ":"):
            capturing = True
            inline = line.split(":", 1)[1].strip()
            if inline:
                result_lines.append(inline)
            continue
        if capturing:
            if line.strip() == "":
                break
            result_lines.append(line.strip())
    return "\n".join(result_lines).strip() if result_lines else None
```

Hardcoded fallbacks were added for the missing HTTPS listener case, so the caller always receives actionable output even if the AI omits the structured sections.

The Lambda response on REJECT now includes:

```json
{
  "verdict": "REJECT",
  "rejection_reason": "The Application Load Balancer has no HTTPS listener configured...",
  "remediation": "Add an aws_lb_listener resource with protocol = HTTPS...",
  "ai_review": "..."
}
```

---

## Challenge 6 — Lambda Always Returned REJECT Regardless of Actual Findings

### Symptom
Every PR was rejected by the AI agent even after the HTTPS listener was correctly added to the Terraform code. The verdict never changed to `APPROVE` or `APPROVE_WITH_CHANGES`.

### Root Cause
The `extract_verdict` function searched the **entire** Gemini response text for the word `"REJECT"`:

```python
if "FINAL VERDICT" in text:
    if "REJECT" in text:   # searches the whole response, not just the verdict line
        return "REJECT"
```

Gemini echoes back the decision policy as part of its reasoning (e.g. *"REJECT if any HIGH severity finding exists..."*), so the word `"REJECT"` was almost always present in the full response — regardless of the actual verdict at the end.

### Fix
Replaced the full-text scan with a regex anchored to the `"Final verdict:"` line, so only the word immediately following that label is read:

```python
match = re.search(
    r"final\s+verdict[^:\n]*:\s*\*{0,2}(APPROVE_WITH_CHANGES|APPROVE|REJECT)\*{0,2}",
    review_text,
    re.IGNORECASE,
)
if match:
    return match.group(1).upper()
return "REJECT"  # fail-safe
```

**Lesson:** When extracting a structured field from an LLM response, always anchor the search to the specific label. A broad `"word in full_text"` check will match anywhere the word appears — including in the model's reasoning, examples, or policy descriptions.

---

## Challenge 7 — Lambda Returning 500 AccessDeniedException

### Symptom
Lambda returned a 500 error on every invocation:

```json
{
  "statusCode": 500,
  "verdict": "REJECT",
  "error": "An error occurred (AccessDeniedException) when calling the GetSecretValue
  operation: User: arn:aws:sts::...:assumed-role/terraform-ai-review-agent-role/...
  is not authorized to perform: secretsmanager:GetSecretValue on resource: gemini-api-key-5"
}
```

The `ai_review` field was `null`, so the workflow printed fallback messages instead of real output.

### Root Cause
A name mismatch between the secret name hardcoded in the Lambda and the secret actually created by Terraform:

| Location | Secret name |
|---|---|
| `lambda_function.py` | `gemini-api-key-5` |
| `secrets.tf` (Terraform-managed) | `gemini-api-key0` |
| IAM policy resource | `aws_secretsmanager_secret.gemini_api_key0.arn` |

The Lambda was requesting a secret that did not exist and was not covered by the IAM policy.

### Fix
Updated the constant in `lambda_function.py` to match the Terraform-managed secret name:

```python
SECRET_NAME = "gemini-api-key0"
```

**Lesson:** Secret names used in application code must exactly match the names defined in infrastructure code. A mismatch causes a silent 500 at runtime because the IAM policy ARN scopes access to the specific secret ARN, not a wildcard.

---

## Challenge 8 — Gemini Markdown Formatting Breaking Section Extraction

### Symptom
Lambda returned `verdict: REJECT` but `rejection_reason` and `remediation` were `null`, causing the workflow to print fallback messages:

```
Reason:      See ai_review for details
Remediation: No remediation provided
```

### Root Cause
Gemini wraps section headers in markdown bold formatting: `**REJECTION_REASON:**`. The `_extract_section` parser checked for a plain string match:

```python
if line.strip().upper().startswith(section_header.upper() + ":"):
```

`**REJECTION_REASON:**` starts with `**`, not `REJECTION_REASON`, so the match always failed and the function returned `None`.

### Fix
Strip markdown bold/italic markers before matching:

```python
clean = re.sub(r"\*+", "", line).strip()
if clean.upper().startswith(section_header.upper() + ":"):
```

**Lesson:** LLMs apply markdown formatting inconsistently. Any parser that reads structured sections from AI output must normalise the text first — at minimum stripping `*`, `_`, `#`, and surrounding whitespace before comparing headers.

---

## Challenge 9 — Terrascan Violations-Only Output Causing False REJECT

### Symptom
The pipeline rejected PRs with the reason "ALB has no HTTPS listener" even though `aws_lb_listener.https` was correctly defined in the Terraform code with `protocol = "HTTPS"` on port 443.

### Root Cause
Terrascan is a **violations-only reporter**. Correctly configured resources produce no output and are invisible to Gemini. The only ALB listener that appeared in the Terrascan JSON was `aws_lb_listener.http` (the port 80 redirect), flagged by rule `AC_AWS_0491` (`listenerNotHttps`).

Gemini received:
- One finding: `aws_lb_listener.http` is not HTTPS
- Zero findings for: `aws_lb_listener.https` (because it was correctly configured)

With no evidence that an HTTPS listener existed, Gemini correctly (from its limited perspective) concluded the ALB had no HTTPS and applied the rejection rule.

Prompt-level workarounds (telling Gemini to "ignore AC_AWS_0491 on redirect listeners") did not work because Gemini had no way to distinguish a redirect listener from a plain HTTP listener using only the violations list.

### Fix
The workflow was updated to grep the actual Terraform files for HTTPS listener count and inject this as verified context into the Lambda payload:

```yaml
- name: Build Lambda payload with Terraform context
  run: |
    HTTPS_LISTENERS=$(grep -rE 'protocol\s*=\s*"HTTPS"' terraform-review-agent/terraform/*.tf | wc -l | tr -d ' ')
    HTTP_REDIRECTS=$(grep -rA5 'port\s*=\s*"?80"?' terraform-review-agent/terraform/*.tf | grep -c 'redirect' || true)

    jq --argjson https "$HTTPS_LISTENERS" --argjson redirects "$HTTP_REDIRECTS" \
      '. + {"terraform_context": {"https_listener_count": $https, "http_redirect_count": $redirects}}' \
      terrascan_report.json > payload.json
```

The Lambda reads this context and injects it at the top of the Gemini prompt as authoritative ground truth:

```python
if https_count > 0:
    https_status = (
        f"VERIFIED: The Terraform code contains {https_count} HTTPS listener(s). "
        f"The ALB IS correctly secured. AC_AWS_0491 on HTTP redirect listeners are false positives."
    )
```

Gemini now sees the verified listener count before evaluating any Terrascan findings, and the `AC_AWS_0491` false positive can no longer override it.

**Lesson:** Static analysis tools report what is wrong, not what is right. When an LLM reasons from violations-only output, the absence of a finding does not mean the absence of a resource — it means the resource is compliant. For binary security properties (HTTPS exists / does not exist), inject ground truth from the source code directly rather than relying on the absence of a violation as proof of compliance.
