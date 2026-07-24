# SECURITY_DEEPDIVE.md — Spec Server (deployed)

Scope: the deployed Spec Server — `https://spec.elasticninja.com` (UI) + `https://api.spec.elasticninja.com`
(API, Cloudflare → API Gateway → Cognito-JWT Lambda → DynamoDB, eu-west-1, acct `985722751424`).
Method: 5 parallel READ-ONLY reviewers (SEC-1..5) auditing code **and** live AWS config, findings
cross-checked and corroborated against the live pool / Lambda env by the orchestrator.

## Verdict

**No P0. No auth bypass, no injection, no leaked secret.** The core machinery is genuinely
well-built — JWT validation, invite burn, email-OTP, group authz, and all four concurrency
guarantees hold in both code and live config. The weaknesses are **posture/hardening gaps**, and the
single most important one is a **Terraform↔AWS drift**: the human passkey hardening was never
actually applied to the live pool.

Severity tally: **P0 = 0 · P1 = 4 · P2 = 6 · P3 = ~7.**

---

## P1 — fix before real users / real traffic

### SEC-DRIFT-1 · Passkey/ESSENTIALS hardening is NOT live (Terraform state ≠ AWS)
- **Evidence:** `terraform state` = `user_pool_tier=ESSENTIALS`, `web_authn_configuration{relying_party_id=spec.elasticninja.com, user_verification=required}`, `sign_in_policy{allowed_first_auth_factors=[PASSWORD,WEB_AUTHN]}`. **Live `describe-user-pool` = `UserPoolTier=null (LITE)`, `WebAuthnConfiguration=null`, `SignInPolicy=null`, `MFA=OFF`, `AdvancedSecurityMode=OFF`.** The AWS provider (5.100) recorded the config in state but the UpdateUserPool API never applied tier/WebAuthn.
- **Impact:** passkeys can't be enrolled (pool is LITE) — the only working human sign-in is **email-OTP (CUSTOM_AUTH)**. The super-admin `dodgymike@gmail.com` (in `spec-admins`) is therefore protected by **single-factor email possession**; compromise of that mailbox = full admin (all task writes + the HA-5 user-lifecycle API).
- **Fix:** reconcile the drift — apply ESSENTIALS + WebAuthn via a mechanism that actually takes (provider upgrade, or CLI `update-user-pool --user-pool-tier ESSENTIALS` + the WebAuthn config API), then require passkeys for `spec-admins`. **Until passkeys are real, enable TOTP MFA (advanced-security ENFORCED) for admins** so the super-admin isn't single-factor. deploy-coordinator must add a post-apply assertion that the live tier/WebAuthn match the config.

### SEC-EDGE-1 · Raw `execute-api` hostname bypasses Cloudflare
- **Evidence (live):** `https://4hrg4owbil.execute-api.eu-west-1.amazonaws.com/{readyz,openapi.json,docs}` → 200 and `POST /api/v1/signup` → 202, all **without** `cf-ray`/`server: cloudflare`; the same via `api.spec.elasticninja.com` carry the Cloudflare headers. No WAF, no origin-lock in `apigw.tf`.
- **Impact:** every Cloudflare WAF / edge-rate-limit / bot rule is skipped by hitting the origin directly. Data-plane is still JWT-gated (raw `/api/v1/...` → 401), so this is **DoS / edge-control bypass, not auth bypass**.
- **Fix:** origin-lock — a Cloudflare Transform Rule injecting a secret header + an app/authorizer check that 403s when absent (or restrict API GW to Cloudflare IP ranges, or front with AWS WAFv2).

### SEC-DOS-1 · `/signup` is a cost-amplification DoS (defeatable limiter + Turnstile off)
- **Evidence:** ~~`app/blueprints/signup.py` `_client_ip()` trusts spoofable `CF-Connecting-IP`/`X-Forwarded-For`~~ (FIXED, SEC-FIX-5: `app/client_ip.py` `client_ip()` now trusts `CF-Connecting-IP` ONLY when origin-lock is effectively enforcing, drops `X-Forwarded-For` as a source, and otherwise keys on `remote_addr` — so a rotated forwarding header no longer defeats the per-IP floor on the raw path; signup + enroll share the one helper); `app/signup_ratelimit.py` **fails open** on any DynamoDB error; live `TURNSTILE_SECRET=""` (bot gate off) and `SIGNUP_ENFORCE_ORIGIN=false`. Each intake = 1 SQS msg + 1 worker Lambda + 1 Cognito `ListUsers`.
- **Impact:** unbounded intake → SQS/Lambda/Cognito cost amplification + `ListUsers` throttle exhaustion. (Per-victim **mail-bomb is bounded** by the `bump_notify`/`bump_resend` caps — good — but cost/DoS is not.)
- **Fix:** set a real `TURNSTILE_SECRET` (needs a Turnstile site+secret key from the owner); ~~only trust `CF-Connecting-IP` when the request proves it came through Cloudflare~~ (DONE, SEC-FIX-5); make the limiter fail-closed on the intake path; add a WAFv2 rate rule.

### SEC-PII-1 · `SIGNUP_PEPPER` empty → unsalted `SHA-256(email)` as the signups PK
- **Evidence:** live `SIGNUP_PEPPER=""`; `app/signup.py email_hash()` falls back to plain SHA-256; the signups-table partition key is that hash.
- **Impact:** anyone with a table dump or `GetItem`/`Query` (app + worker roles have it) can confirm/enumerate whether a **known** email requested access (dictionary-reversible).
- **Fix:** set a strong `SIGNUP_PEPPER` (ideally sourced from Secrets Manager, not a Lambda env var) and fail the deploy if empty.

---

## P2 — hardening

- **SEC-DATA-1 · `signups` + `invites` tables lack deletion protection.** `dynamodb.tf` (app table) has `deletion_protection_enabled` + `prevent_destroy`; `signups.tf` + `invites.tf` have neither → a `terraform destroy`/raw `DeleteTable` drops the PII/invite store. Add both.
- **SEC-SECRET-1 · `agent-credentials` secret: no rotation, AWS-managed key, value in state.** One secret holds all 5 agents' permanent passwords (incl. 2 admin agents); no CMK, no rotation, and the plaintext is mirrored into `.tfstate`. Attach a CMK, lock the state bucket (SSE-KMS + tight policy), plan rotation.
- **SEC-IAM-1 · Reaper holds account-wide `s3:DeleteObject` (`arn:aws:s3:::*/*`), no tag/bucket scope** (`reaper.tf`). Mitigated by the durable-Deny + dry-run default + no preview-creation flow yet, but scope it to the preview prefix before enabling live reaping — it's the broadest standing mutating grant.
- **SEC-DOS-2 · No per-tenant API throttle.** `apigw.tf default_route_settings` is one stage-wide `rate_limit=100/burst=50` shared across all tokens — one token can starve everyone. Add a WAFv2 rate-based rule keyed on JWT `sub` (or usage plans).
- **SEC-AUTH-2 · Email-OTP brute/email-bomb has no cross-session cap.** The 3-attempt cap is per-session; `InitiateAuth CUSTOM_AUTH` hits cognito-idp directly (not behind the API-GW throttle) and advanced security is OFF; each wrong guess re-emails a fresh code. Enable advanced-security ENFORCED or a per-user rate cap.
- **SEC-EDGE-2 · HSTS drift.** Live header `max-age=7776000` (90d) `preload`, but `cloudfront.tf` intends 2y — the Cloudflare edge is overriding it, and `preload` is ineffective under 1 year. Reconcile the Cloudflare edge HSTS to ≥1y or drop `preload`.

(Note: the reviewers also flagged `COGNITO_AUDIENCE` unset — but the **deployed** Lambda env DOES set it (`agents,ui` client ids), so that app-level defense-in-depth is intact in prod. Not a finding.)

---

## P3 — low / defense-in-depth

- `app/schemas.py` `SignupIn.hp_website` lacks `validate.Length(max=256)` (request/log bloat).
- `signup_worker_lambda/handler.py` f-strings a (pre-normalized, escaped) email into the Cognito `ListUsers` Filter — off the observable path (DLQ on failure), but add a format allow-list before interpolation.
- Non-email-bound invites set `autoVerifyEmail=true` on burn → `email_verified` is untrustworthy for open invites (mitigated: sign-in still needs email-OTP to that mailbox; admin-approve always mints email-bound invites).
- `/openapi.json` + `/docs` are public and advertise the `/api/v1/admin/*` surface — informational (intended contract, no secrets).
- SES in sandbox; production access must be requested; bounce/complaint go to CloudWatch metrics only (no SNS suppression path).
- Unbounded admin invite `scan` (admin-only, TTL-swept); `_provision_signup` re-mints a duplicate single-use invite on retry (cosmetic).

---

## Verified correct (controls that hold — code + live)

JWT validation: RS256 pinned (rejects `alg=none`/HS-confusion), iss/exp/iat/token_use=access, aud-or-client_id ∈ `[agents,ui]`, JWKS cache + unknown-kid anti-amplification. Local/open default **unreachable** in prod (COGNITO_ISSUER set). API-GW authorizer on every data-plane method; public routes = health/docs/openapi/signup/validate only; no `$default`; OPTIONS not routed (preflight only). **CORS correctly scoped** — evil origin → no ACAO, `spec.elasticninja.com` echoed, `allow_credentials=false`, never `*`. Group authz **fail-closed** from the verified token; self-lockout + last-admin guards; invite email-binding atomic in the burn; approve is email-validated-only, no approve-to-admin. **IAM least-priv** — app role DynamoDB scoped to table+`/index/*`, cognito-idp admin scoped to the pool ARN, SES scoped to identity+config-set, no secretsmanager grant to the app role, no `Resource="*"` on the app role. Magic-link single-use (conditional flip) + hash-only + TTL + `hmac.compare_digest`; email-OTP code server-only + 300s TTL + constant-time + DefineAuth fail-closed; non-existent user → no email (PUEE). All DynamoDB expressions parameterized (no injection; `q` filtered client-side). UI: strict CSP (no unsafe-inline, connect-src allow-list), no XSS sinks, tokens in-memory only, decoded-token admin gating is UI-convenience only (server re-checks the **verified** token). No secrets in tracked files; state backend `encrypt=true`.

## Remediation — what's doable now vs needs you

**I can apply now:** origin-lock (Cloudflare secret header via your token + an app check), set `SIGNUP_PEPPER` + make the limiter fail-closed, add `deletion_protection`+`prevent_destroy` to signups/invites, scope the reaper `s3:DeleteObject`, add `hp_website` length + the ListUsers format-assert.
**Needs you:** a Cloudflare **Turnstile** site+secret key; **SES production access** request; a decision on the **passkey drift** (upgrade the provider / apply via CLI vs. enable TOTP MFA for admins as the interim second factor).
**Needs investigation:** the Cognito ESSENTIALS/WebAuthn provider drift (SEC-DRIFT-1) — the root cause of the non-functional passkeys.
