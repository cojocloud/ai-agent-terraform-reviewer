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
