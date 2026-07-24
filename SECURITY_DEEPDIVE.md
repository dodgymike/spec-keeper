# SECURITY_DEEPDIVE.md — Spec Server (SEC-1..6 review round)

Scope: the deployed Spec Server — `https://spec.elasticninja.com` (UI) +
`https://api.spec.elasticninja.com` (API: Cloudflare → API Gateway → Cognito-JWT Lambda →
DynamoDB, `eu-west-1`, account `985722751424`), plus the merged JIRA-integration surface.
Method: five parallel **read-only** reviews (SEC-1 auth/identity, SEC-2 API/edge, SEC-3
access/IAM, SEC-4 UI, SEC-5 data/secrets), synthesized here under SEC-6. Live AWS config was
sampled with `boto3`/`AWS_PROFILE=default` to confirm the deployed posture (read-only; nothing
changed).

> This is a **second review round**. The first round's findings (passkey/ESSENTIALS drift, raw
> `execute-api` bypass, signup DoS, `SIGNUP_PEPPER`, deletion-protection, agent-creds CMK, HSTS,
> per-tenant throttle, OTP cross-session cap) are all **remediated** — see the `done` `SEC-DRIFT-1`
> / `SEC-EDGE-*` / `SEC-DOS-*` / `SEC-PII-1` / `SEC-DATA-1` / `SEC-SECRET-1` / `SEC-IAM-1` /
> `SEC-AUTH-2` tasks in the `SECURITY` epic. This round audits the **merged JIRA config surface**
> and remaining **edge/config hardening**.

## Executive summary

**Overall posture: strong.** No P0 in the code this round; **no auth bypass, no injection, no
leaked secret at rest.** The core machinery — RS256 JWT validation, per-project isolation,
group authz, the four concurrency guarantees, IAM least-privilege, PITR — is genuinely
well-built and was re-verified sound (see "What was verified sound").

The findings this round **concentrate in two places**:

1. **The merged JIRA integration surface** — a cross-tenant IDOR + an SSRF in the JIRA config
   endpoints, plus reader-visible raw JIRA exception text. **These are the only P0/P1 code
   findings and remediation is already IN PROGRESS under `SEC-FIX-1`** (in the `SECURITY` epic).
   They are **not** re-filed here.
2. **Edge / configuration hardening** — a set of P2 defense-in-depth gaps (audience-check
   fail-open default, zero clock-skew leeway, spoofable client-IP headers, backend exception-text
   leakage, uncapped free-text and import validation, a wildcard KMS key-policy principal, an
   edge-only CSP, and a below-bar JIRA Fernet key). None is individually exploitable in the
   current deployed configuration, but each is worth closing.

One **cross-cutting, elevated (P1-ops)** item is called out prominently below: the edge
protections all rest on **origin-lock enforcement**, which was **verified live as `enforce`**.

Severity tally (this round): **P0 = 0 · P1 (code, JIRA) = in progress under SEC-FIX-1 ·
P1-ops = 1 · P2 = 10.**

---

## Cross-cutting elevated item (P1-ops) — origin-lock is the keystone

The edge protections (Cloudflare Turnstile, WAF, per-IP rate limits) are **bypassable via the raw
API Gateway `execute-api` host** UNLESS `ORIGIN_LOCK_MODE=enforce` with a non-empty secret is
deployed. The gate lives at `app/__init__.py:90`; the config default is **`off`**
(`app/config.py:183`). Several of the P2 fixes below (notably the spoofable-client-IP fix,
SEC-FIX-5) are only sound *because* origin-lock proves a request actually transited Cloudflare.

- **Live observation (2026-07-24, read-only via boto3 on `spec-server-dev-api`):**
  `ORIGIN_LOCK_MODE = "enforce"`, `ORIGIN_LOCK_HEADER = "X-Origin-Lock"`, and
  `ORIGIN_LOCK_SECRET` is **set and non-empty**. The raw-origin bypass is therefore **currently
  mitigated in prod.**
- **Remaining work (`SEC-FIX-2`, P1):** add a deploy-time / post-apply assertion so a future
  deploy cannot silently regress `ORIGIN_LOCK_MODE` back to `off`/`warn` or blank the secret.

*(Also observed live and consistent with prod being hardened: `COGNITO_AUDIENCE` is set to the
agents + UI client ids; `TURNSTILE_SECRET` and `SIGNUP_PEPPER` are set; `STORAGE_BACKEND=dynamodb`.
`AUTH_LEEWAY` is unset, i.e. the `0` default — see SEC-FIX-4.)*

---

## Per-dimension findings

### SEC-1 — Auth & identity  (no P0/P1)

| # | Finding | Sev | Location | Impact | Fix |
|---|---------|-----|----------|--------|-----|
| 1a | `COGNITO_AUDIENCE` empty → audience check **fails open** | P2 | `app/helpers.py:242`, `app/config.py:203` | Access tokens from an unintended client id would be accepted if the allow-list is empty. Mitigated in prod (live env sets both client ids). | Keep pinned to the agents + UI client ids in prod; add a startup/deploy assertion that fails closed when `COGNITO_ISSUER` is set but the audience list is empty. |
| 1b | `AUTH_LEEWAY = 0` clock-skew intolerance | P2 | `app/config.py:213` | A just-minted token can be rejected on minor Cognito↔Lambda clock skew (exp/nbf/iat). | Set `AUTH_LEEWAY` to 30–60 s. |
| 1c | Signup/enroll per-IP limiter trusts spoofable headers | P2 | `app/blueprints/signup.py:61` | `_client_ip()` reads `CF-Connecting-IP` / `X-Forwarded-For` directly; forgeable on the raw path → limiter defeatable. | Trust those headers only when origin-lock proves a Cloudflare origin; else use the peer address. *(Same finding as SEC-2c; filed once.)* |

### SEC-2 — API & edge  (P1 = JIRA IDOR + SSRF → SEC-FIX-1, in progress)

The two **P1** findings — a **cross-tenant IDOR** and an **SSRF** in the JIRA config endpoints —
are **being remediated under `SEC-FIX-1`** and are **not** re-filed here.

| # | Finding | Sev | Location | Impact | Fix |
|---|---------|-----|----------|--------|-----|
| 2a | Backend exception strings leaked to clients | P2 | `app/blueprints/health.py:31` (`/readyz` echoes `str(exc)`); generic handler `app/__init__.py:159` renders raw `BackendUnavailable(str(exc))` sourced from many `app/storage/*` sites | Internal error detail (backend/driver strings) exposed to unauthenticated callers. | Return a neutral, stable string; log detail server-side only. *(Distinct from the JIRA-error-text leak, which is SEC-FIX-1.)* |
| 2b | Full-fidelity import skips enum/length validation | P2 | `app/schemas.py:362` (`ExportTaskOut`) | `ExportTaskOut` omits the `OneOf` enum checks `TaskIn` enforces (status/priority); `section` is validated nowhere → a crafted import injects out-of-range or oversized values. | Apply the same `OneOf` validators + length caps (incl. `section`) on the import schema. |
| 2c | Signup/enroll rate-limit spoofable headers | P2 | `app/blueprints/signup.py:61` | Same as SEC-1c. | Same as SEC-1c (filed once, SEC-FIX-5). |
| 2d | No `validate.Length(max=)` on free-text fields | P2 | `app/schemas.py` (title/description/body/notes) | Oversized-payload request/storage bloat + log amplification. | Add reasonable max-length validators. |

### SEC-3 — Access control & IAM  (P0 = JIRA IDOR → SEC-FIX-1, in progress)

| # | Finding | Sev | Location | Impact | Fix |
|---|---------|-----|----------|--------|-----|
| 3 | agent-credentials CMK key policy grants `kms:Decrypt` to `principals = ["*"]` | P2 | `infra/terraform/cognito.tf:608-635` | Wildcard decrypt principal. **Bounded today** — no Lambda role holds `secretsmanager:GetSecretValue` on that secret, so the key cannot be used to read it via the service. | Scope the decrypt grant to the intended agent-runner principals, or document + monitor the wildcard with a CloudTrail alarm on `kms:Decrypt` for that key. |

The IAM P0 (JIRA cross-tenant IDOR) is owned by **`SEC-FIX-1`** (in progress).

### SEC-4 — UI security  (no P0/P1)

| # | Finding | Sev | Location | Impact | Fix |
|---|---------|-----|----------|--------|-----|
| 4a | CSP is **edge-only**, absent from the SPA HTML | P2 | `infra/terraform/cloudfront.tf:88` (edge); `ui/index.html` (none) | A direct-origin fetch of the SPA ships no CSP; the strong policy exists only at the CloudFront edge. | Add a `<meta http-equiv="Content-Security-Policy">` baseline in `ui/index.html` as defense-in-depth. |
| 4b | `img-src 'self' data:` may need re-check | P2 | `ui/index.html` CSP (with 4a) | Dropping `data:` could break hashed/inlined images if the build emits them. | Verify `img-src` against the actual build output before tightening; keep `data:` only if needed. *(Folded into SEC-FIX-10.)* |
| 4c | Policy guardrail: HTML-injection sinks are token-exfil-grade | P2 | UI review process / CI | Tokens live in browser memory; any injection sink is auth-material exfiltration. | Standing policy: any future `dangerouslySetInnerHTML`, untrusted markdown/HTML renderer, or CSP `unsafe-inline`/`unsafe-eval` relaxation is an **automatic P0** needing sign-off. |

### SEC-5 — Data & secrets  (no P0)

| # | Finding | Sev | Location | Impact | Fix |
|---|---------|-----|----------|--------|-----|
| 5a | Raw JIRA exception text persisted, reader-visible | P1 | `app/jira_sync.py:75,135` (`jira_sync_error` + events) | Internal JIRA error detail surfaced to readers. | **Being fixed under `SEC-FIX-1`** — not re-filed here. |
| 5b | JIRA Fernet key is a bare env var | P2 | `app/crypto.py:36` | Below the CMK/Secrets-Manager bar used for other secrets. **Fails closed today** — JIRA is prod-disabled. | Before enabling JIRA in prod, source the key from Secrets Manager and use `MultiFernet` for rotation, or document that JIRA is intentionally prod-disabled. |

---

## Consolidated remediation table

| Finding | Dimension | Severity | Filed task |
|---------|-----------|----------|------------|
| Origin-lock keystone: assert `enforce` stays live | Cross-cutting | **P1-ops** | **`SEC-FIX-2`** (`d8fed691`) |
| Pin `COGNITO_AUDIENCE` + assert non-empty | SEC-1a | P2 | `SEC-FIX-3` (`301cb9f8`) |
| Set `AUTH_LEEWAY` 30–60 s | SEC-1b | P2 | `SEC-FIX-4` (`9ac5d68b`) |
| Trust `CF-Connecting-IP`/XFF only when origin-locked | SEC-1c / SEC-2c | P2 | `SEC-FIX-5` (`5ddebd5d`) |
| Stop leaking backend exception strings | SEC-2a | P2 | `SEC-FIX-6` (`b56275d1`) |
| Validate `ExportTaskOut` import like `TaskIn` | SEC-2b | P2 | `SEC-FIX-7` (`9225c21c`) |
| Length caps on free-text fields | SEC-2d | P2 | `SEC-FIX-8` (`6aed1c85`) |
| Scope agent-creds CMK off `principals=["*"]` | SEC-3 | P2 | `SEC-FIX-9` (`0e7015c5`) |
| Add `<meta>` CSP baseline to `ui/index.html` | SEC-4a / SEC-4b | P2 | `SEC-FIX-10` (`ca9331e7`) |
| CSP/XSS auto-P0 review guardrail | SEC-4c | P2 | `SEC-FIX-11` (`8985c94c`) |
| JIRA Fernet key → Secrets Manager + `MultiFernet` | SEC-5b | P2 | `SEC-FIX-12` (`6ad087d6`) |
| **JIRA config IDOR + SSRF + reader-visible error text** | SEC-2 P1 / SEC-3 P0 / SEC-5a | **P0/P1** | **`SEC-FIX-1` — remediation IN PROGRESS** (not re-filed) |

All new tasks are in the `SECURITY` epic of project `spec-server`.

---

## What was verified sound (controls that hold — code + live)

- **JWT validation.** RS256 pinned (rejects `alg=none` / HS-confusion); `iss`/`exp`/`iat`/
  `token_use=access` checked; audience-or-`client_id` matched against the configured allow-list
  (live: agents + UI client ids); JWKS cache + unknown-`kid` anti-amplification
  (`JWKS_MIN_REFRESH_INTERVAL`). The open/local default is unreachable in prod (`COGNITO_ISSUER`
  set).
- **Project isolation.** Per-project scoping is enforced server-side (fail-closed intersection of
  membership and project); no cross-project read/write via the task/epic/reservation surface. The
  only isolation gap this round is the **JIRA-config IDOR**, owned by `SEC-FIX-1`.
- **Injection-clean.** All DynamoDB expressions are parameterized via `ExpressionAttributeValues`;
  no user input is formatted into an expression string. Postgres adapter stays on bound params.
- **IAM least-privilege.** App role's DynamoDB access is scoped to the table + `/index/*`;
  cognito-idp admin scoped to the pool ARN; SES scoped to identity + config-set; **no
  `secretsmanager:GetSecretValue`** grant to the app role (which is exactly why the SEC-3 CMK
  wildcard is bounded); no `Resource="*"` on the app role.
- **PITR / durability.** Point-in-time recovery + deletion protection on the app/signups/invites
  tables (closed in the prior round, `SEC-DATA-1`), state backend encrypted.
- **Strict CSP at the edge.** CloudFront serves a strict policy (no `unsafe-inline`, `connect-src`
  allow-list); tokens are held in memory only; no XSS sinks in the SPA. The only gap is the
  **edge-only** delivery (SEC-4a → `SEC-FIX-10`).
- **Concurrency guarantees** (atomic claim, collision-proof reservation, optimistic-locking/412,
  multi-item atomicity) hold on both backends — unchanged and out of scope this round.

---

## Appendix — live posture sampled (read-only, 2026-07-24)

`spec-server-dev-api` Lambda environment (secrets redacted):

| Key | Value | Note |
|-----|-------|------|
| `ORIGIN_LOCK_MODE` | `enforce` | keystone control ON |
| `ORIGIN_LOCK_SECRET` | *(set, non-empty)* | required for enforce to bite |
| `COGNITO_AUDIENCE` | agents + UI client ids | SEC-1a mitigated in prod |
| `AUTH_LEEWAY` | *(unset → `0`)* | SEC-1b applies |
| `TURNSTILE_SECRET` | *(set)* | bot gate ON |
| `SIGNUP_PEPPER` | *(set)* | prior-round fix intact |
| `STORAGE_BACKEND` | `dynamodb` | prod backend |

Nothing was modified. Origin-lock mode was read only, not changed, per the review's read-only
mandate.
