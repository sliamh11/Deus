# DevOps Review Rules — Wardens/devops-reviewer

> Rules the `devops-reviewer` agent checks against infrastructure-as-code, cloud topology,
> CI/CD pipelines, and deploy procedures BEFORE an apply/merge with production blast radius.
> Add a new rule by appending a section. No agent edit needed.
>
> Format per rule: `Severity`, `Applies when`, `Check`, `Rule`.
> Severity: `blocking` (must fix before SHIP) · `warning` (should address) · `informational`
> (author's awareness). Security and data-loss findings default to blocking; cost findings
> default to warning unless egregious.

## iac-idempotency
**Severity:** blocking
**Applies when:** Change touches Terraform/CloudFormation resource definitions or module versions.
**Check:** Would a `terraform apply` (or equivalent) destroy-and-recreate a stateful resource (RDS, EBS, data buckets) unexpectedly? Are `lifecycle` guards (`prevent_destroy`, `ignore_changes`) present where churn or accidental deletion is a real risk? Are provider/module versions pinned (no floating `latest`)? Are variable defaults safe rather than required-but-unset?
**Rule:** No apply should silently destroy or unexpectedly recreate stateful, data-bearing resources. Pin versions; guard anything whose accidental deletion would be costly.

## state-backend-safety
**Severity:** blocking
**Applies when:** Change touches remote state configuration for shared infrastructure.
**Check:** Is a remote state backend configured (S3+lock/DynamoDB or equivalent), not local state for shared infra? Is the state bucket encrypted, versioned, and public-access-blocked, with a lock table present? Does each environment use a distinct state key so one env cannot clobber another's state?
**Rule:** Shared infrastructure state must never be local, unencrypted, unlocked, or shared across environments.

## cost-efficiency
**Severity:** warning
**Applies when:** Change provisions or resizes compute, storage, NAT, or sets log/backup retention.
**Check:** Is compute right-sized for the environment's actual load (cheapest viable tier for non-prod)? Is NAT cost addressed for low-traffic environments (managed NAT gateway is expensive per AZ per month — flag if a cheaper pattern would fit)? Are interruption-tolerant workloads using Spot/equivalent where sensible? Is retention matched to the environment (short for staging)? Are there idle or orphaned paid resources?
**Rule:** Cost findings are warnings by default — escalate to blocking only when the waste is egregious (e.g. an order of magnitude over actual need with no stated justification).

## security-least-privilege
**Severity:** blocking
**Applies when:** Change touches security groups, IAM policies/roles, secrets handling, TLS configuration, or deletion-protection settings.
**Check:** Is any security group open to `0.0.0.0/0` on a sensitive port? Is the database ever publicly accessible? Are IAM roles/policies least-privilege and resource-scoped, with CI deploy roles using OIDC rather than long-lived keys? Do secrets come from a runtime secret store rather than being baked into images or committed? Is TLS enforced end-to-end where data sensitivity warrants it? Are deletion/data-loss guards (deletion protection, skip-final-snapshot=false) present on production data, with any deliberate absence on throwaway environments clearly intentional?
**Rule:** Security-boundary and data-loss-guard gaps are blocking by default — a throwaway environment may deliberately trade durability for cost, but that trade-off must be visibly deliberate, not accidental.

## deploy-safety-reversibility
**Severity:** blocking
**Applies when:** Change alters the deploy process, database migrations, or a pipeline that auto-migrates a live database.
**Check:** Is the change's blast radius understood and minimized — can a single apply/merge take down unrelated production? Are migrations deploy-window-safe (additive; FK additions via NOT VALID + deferred VALIDATE; no blocking locks)? Is there an explicit, cheap rollback path, with irreversible steps (data deletion, type rewrites, cert/NS changes near a renewal window) called out and given a recovery plan? Is deploy concurrency controlled, and are health checks / an auto-rollback circuit breaker configured?
**Rule:** Every change with production blast radius needs an explicit, understood rollback path. Irreversible steps must be named, not discovered after the fact.

## observability-operability
**Severity:** informational
**Applies when:** Change introduces a new component, service, or deploy path that could fail silently.
**Check:** Are logs shipped with sane retention? Do critical failure modes have at least minimal alerting, or is the absence an accepted, documented gap? Are egress/static-IP assumptions (e.g. partner allowlists vs. an ephemeral NAT IP) correct for the environment? Does a non-trivial stack have a documented apply/runbook procedure?
**Rule:** Observability gaps are informational by default — don't force alerting on an environment where production itself also lacks it and the risk is low, but do confirm the gap is a deliberate, known trade-off rather than an oversight.

---

## Remediation Details

### iac-idempotency
**Cite:** `devops-reviewer` agent's rubric section A (IaC correctness & idempotency)
**Remediation:** Add the missing `lifecycle` block, pin the floating version, or fix the variable default that would fail mid-apply.

### state-backend-safety
**Cite:** `devops-reviewer` agent's rubric section B (State & backend safety)
**Remediation:** Migrate to a remote backend with locking, enable encryption/versioning/public-block on the state bucket, or give the environment its own distinct state key.

### cost-efficiency
**Cite:** `devops-reviewer` agent's rubric section C (Cost efficiency)
**Remediation:** Right-size the resource, switch to a cheaper NAT/compute pattern for the environment, or remove the idle/orphaned resource.

### security-least-privilege
**Cite:** `devops-reviewer` agent's rubric section D (Security posture & least privilege)
**Remediation:** Scope the security-group ingress, tighten the IAM policy to the specific resource/action, move the secret to a runtime store, or add the missing deletion-protection flag.

### deploy-safety-reversibility
**Cite:** `devops-reviewer` agent's rubric section E (Deploy safety, blast radius & reversibility)
**Remediation:** Add the missing rollback step, split the migration into deploy-window-safe phases, or add the health-check/circuit-breaker configuration.

### observability-operability
**Cite:** `devops-reviewer` agent's rubric section F (Observability & operability)
**Remediation:** Add the missing log retention setting, minimal alert, or runbook section — or explicitly document the accepted gap if that's the deliberate choice.
