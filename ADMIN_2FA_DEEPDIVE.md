# ADMIN_2FA_DEEPDIVE — an agent-safe second factor for human admins

**Question:** what is the cleanest **agent-safe** way to give human admins (esp. the
super-admin `dodgymike@gmail.com`, in `spec-admins`) a real second authentication factor on
the deployed Spec Server, without breaking the AI agents that mint tokens non-interactively?

**Scope:** INVESTIGATION ONLY. No AWS resource was mutated; no deploy; no code committed.
This doc + a recommendation + AWS costs + a SPEC-ready task breakdown.

**Pool under investigation:** `eu-west-1_S1fUqxuKv` (account `985722751424`, `eu-west-1`).

---

## 0. Evidence provenance & what I could NOT verify (no silent caps)

- **Terraform source** (`infra/terraform/cognito.tf`, `customauth.tf`, `terraform.tfvars`,
  `dynamodb.tf`, `ses.tf`) and the CUSTOM_AUTH Lambda source (`define_auth_lambda/handler.py`)
  were read directly this session — quoted with `file:line` below.
- **Live pool state** is cited from the prior read-only capture recorded in
  `SECURITY_DEEPDIVE.md` §SEC-DRIFT-1 (`describe-user-pool` output). **I could NOT
  independently re-run `aws cognito-idp describe-user-pool` this session:** the sanctioned
  read-only profiles `spec-server-readonly` / `spec-server-infra` are **not configured** in
  `~/.aws` on this host (`The config profile (spec-server-readonly) could not be found`), and
  the auto-mode classifier blocks AWS calls made through the other local profiles. So the
  live-vs-Terraform facts below rest on the SEC-DRIFT-1 capture, not a fresh call. **Before
  acting, aws-infra should re-run `describe-user-pool` / `describe-user-pool-client` to confirm
  the live state is unchanged** (a 30-second read-only check).
- One load-bearing Cognito-behaviour claim (native MFA does **not** interpose on a
  `CUSTOM_AUTH` flow) is asserted from Cognito's documented design, not from a live test in
  this pool. Its disproof test is stated explicitly in §3.

---

## 1. Symptom

The only working human sign-in is **single-factor email-OTP**. The super-admin
`dodgymike@gmail.com` holds full write authority (all task writes + the HA-5 user-lifecycle
API) behind **mailbox possession alone**: whoever controls that inbox can complete an
email-OTP challenge and become admin. The passkey hardening that was *supposed* to fix this
(HA-1) is recorded in Terraform state but **is not live on the pool** (SEC-DRIFT-1).

The naive fix — "turn on Cognito MFA" — is a **trap**: MFA in Cognito is pool-wide, the agents
share the pool, and REQUIRED MFA would hand every agent an MFA challenge and break their
non-interactive token minting.

---

## 2. Evidence

### 2.1 Live pool reality (from SECURITY_DEEPDIVE.md §SEC-DRIFT-1)

> Terraform state = `user_pool_tier=ESSENTIALS`, `web_authn_configuration{relying_party_id=
> spec.elasticninja.com, user_verification=required}`, `sign_in_policy{allowed_first_auth_
> factors=[PASSWORD,WEB_AUTHN]}`. **Live `describe-user-pool` = `UserPoolTier=null (LITE)`,
> `WebAuthnConfiguration=null`, `SignInPolicy=null`, `MFA=OFF`, `AdvancedSecurityMode=OFF`.**
> The AWS provider (5.100) recorded the config in state but the UpdateUserPool API never
> applied tier/WebAuthn.

Consequence: the pool is on the free **LITE** plan, WebAuthn is off, so **passkeys cannot be
enrolled**. `allowed_first_auth_factors` is null → the `USER_AUTH` choice flow has no
WEB_AUTHN option. The only human path that works is the `CUSTOM_AUTH` email-OTP chain.

### 2.2 What the Terraform *intends* (not live)

- `cognito.tf:254` `user_pool_tier = "ESSENTIALS"` — required for native WebAuthn.
- `cognito.tf:273-276` `web_authn_configuration { relying_party_id = var.webauthn_rp_id;
  user_verification = "required" }`.
- `cognito.tf:283-285` `sign_in_policy { allowed_first_auth_factors = ["PASSWORD","WEB_AUTHN"] }`
  — **PASSWORD stays allowed**, which is what keeps agents alive under the intended design.
- `terraform.tfvars:37` `webauthn_rp_id = "spec.elasticninja.com"` (the UI now lives there;
  the variable default at `cognito.tf:135` still points at the old CloudFront host —
  a latent mismatch, see §6).

### 2.3 The human sign-in path is CUSTOM_AUTH email-OTP (single round)

`infra/terraform/define_auth_lambda/handler.py` (DefineAuthChallenge state machine):

> "This is a **SINGLE-ROUND email-OTP chain** (the FIRST authentication factor for onboarding
> + passkey recovery): email-OTP -> issue tokens. The bird reference runs a two-round
> `email-OTP -> TOTP` chain; **that second factor is DEFERRED for HA-3.**"

The decision table issues `issueTokens = true` the moment **one** CUSTOM_CHALLENGE (email-OTP)
succeeds (`handler.py`, `if any(... challengeResult is True ...): resp["issueTokens"]=True`).
There is no second factor. `otp.py` confirms: *"TOTP (the bird SECOND factor) is intentionally
NOT included here."* The three custom-auth Lambda roles are deliberately given **no DynamoDB
access** today (`customauth.tf` least-privilege).

The human `ui` client (`cognito.tf:479-512`) exposes `ALLOW_USER_AUTH`, `ALLOW_CUSTOM_AUTH`,
`ALLOW_REFRESH_TOKEN_AUTH` — **no** `USER_PASSWORD_AUTH`/`SRP`. Humans have no password; there
is no self-service password (recovery is admin-only, `cognito.tf:290-295`). So the standard
password/SRP flows that native MFA hooks into are **not a path humans can use**.

### 2.4 The agents path — and exactly how MFA would break it

Agents authenticate on a **separate app client** `agents` (`cognito.tf:443-470`) with
`ALLOW_USER_PASSWORD_AUTH` / `SRP` / `REFRESH`. `scripts/agent_token.py:196-206` does:

```python
resp = self._idp().initiate_auth(AuthFlow="USER_PASSWORD_AUTH", ClientId=..., AuthParameters={USERNAME, PASSWORD})
self._store_result(resp, keep_refresh_on_missing=False)
```

and `_store_result` (`agent_token.py:227-232`):

```python
token = (resp.get("AuthenticationResult") or {}).get("AccessToken")
if not token:
    raise TokenError("Cognito did not return an access token (challenge required?).")
```

**This is the exact breakage vector.** If the pool's MFA were set to **REQUIRED/ON**, Cognito
returns a `ChallengeName` (`SOFTWARE_TOKEN_MFA` or `MFA_SETUP`) with **no** `AuthenticationResult`
→ `_store_result` raises `TokenError` → every agent loses its token. There is no
challenge-handling code. **Pool-wide REQUIRED MFA is therefore disqualified.**

### 2.5 The tfvars coupling that mis-frames the fix

`cognito.tf:116-120,328,344`: `enable_mfa` simultaneously flips `mfa_configuration=OPTIONAL`
**and** `advanced_security_mode=ENFORCED`. The variable doc admits: *"advanced_security_mode=
ENFORCED requires the Cognito PLUS feature plan … Turning enable_mfa on in prod now also
requires bumping user_pool_tier to PLUS."* So the repo's existing "MFA switch" drags in the
**PLUS** tier (a per-MAU cost bump) — and, as §3 shows, still wouldn't actually protect the
human email-OTP path.

---

## 3. Root cause & the key correction

**Confirmed root cause of the exposure:** SEC-DRIFT-1 — the ESSENTIALS/WebAuthn hardening never
applied to the live pool, so passkeys were never enrollable and the human first (and only)
factor is email-OTP via `CUSTOM_AUTH`. Single mailbox compromise = full admin.

**Key correction to the SEC-DRIFT-1 interim suggestion ("enable TOTP MFA for admins"):**

> **Cognito's native TOTP/SMS MFA only interposes on the standard password/SRP-based flows
> (`USER_PASSWORD_AUTH`, `USER_SRP_AUTH`, `ADMIN_USER_PASSWORD_AUTH`, and the password/passkey
> legs of `USER_AUTH`). It does NOT inject a second challenge into a `CUSTOM_AUTH` flow — that
> flow is driven entirely by the DefineAuthChallenge Lambda.**

Because humans sign in via `CUSTOM_AUTH` (email-OTP) and have no password:

- **REQUIRED MFA** → no effect on the human path, and **breaks agents** (§2.4). ✗
- **OPTIONAL MFA** → agents are not broken (they never enroll a factor, so are never
  challenged), **but it is a no-op for humans too**: it never layers onto their `CUSTOM_AUTH`
  sign-in. It would only bite if a human authenticated via a password/SRP flow with an enrolled
  TOTP — a path humans don't have. So "enable OPTIONAL TOTP MFA for admins" **does not deliver
  a second factor here.** ✗ (This is the convenient-but-wrong answer; label it dead.)

Therefore a real second factor for the current human path must be delivered **either** inside
the `CUSTOM_AUTH` chain (add a TOTP round — Option A) **or** by fixing the drift and moving
humans onto phishing-resistant passkeys (Option B). Native pool MFA is not the lever.

- **Disproof test for the "native MFA doesn't touch CUSTOM_AUTH" claim** (do this on a throwaway
  pool, never the live one): create a LITE pool, set `mfa_configuration=OPTIONAL` + software
  token, enroll a test user's TOTP, then run `initiate_auth AuthFlow=CUSTOM_AUTH` and observe
  whether the response carries `ChallengeName=SOFTWARE_TOKEN_MFA`. If it does, Option A's
  premise weakens; documented behaviour says it will not.

---

## 4. The options

Legend: **Agent-safe?** = does it leave `USER_PASSWORD_AUTH` on the `agents` client untouched?
**Cost** = incremental AWS $/mo at *current* scale (1 human MAU, ~6 agent MAU).

### Option A — Add a TOTP round to the existing CUSTOM_AUTH chain  ⭐ RECOMMENDED

Extend the DefineAuthChallenge state machine from one round (email-OTP) to two
(email-OTP → TOTP) for human sign-in — exactly the "deferred" bird design the code already
documents. TOTP is implemented **in our own Lambdas** (RFC 6238), *not* via Cognito's MFA
subsystem, so it is entirely decoupled from the pool's `mfa_configuration`.

- Round 1: email-OTP (as today). Round 2: DefineAuthChallenge issues another
  `CUSTOM_CHALLENGE`; CreateAuthChallenge marks it `TOTP`; VerifyAuthChallengeResponse computes
  the expected TOTP from the user's stored secret and constant-time compares. `issueTokens` only
  after **both** succeed. New admins/humans without an enrolled TOTP secret can be gated
  (fail-closed) or bootstrapped via an enrollment step.
- **Storage:** the TOTP shared secret per user in the existing single-table DynamoDB
  (`spec-server-dev-app`, PK/SK — `dynamodb.tf:36-40`), e.g. `PK=USER#<sub>`, `SK=TOTP`,
  server-side-encrypted. No new table needed. Grant **only** the verify (and enroll) Lambda a
  scoped `GetItem`/`PutItem` on that table + key pattern (the custom-auth roles have no DynamoDB
  access today — this is the one IAM addition).
- **Enrollment UI:** a "set up your authenticator" step (show `otpauth://` QR + verify a code)
  in the SPA, admin-only to start.
- **Agent-safe?** ✅ **By construction.** Agents use `USER_PASSWORD_AUTH` on the `agents`
  client and never enter `CUSTOM_AUTH`. Nothing on the agent path changes. No pool `mfa_config`
  is touched.
- **Tier change?** ❌ None. Stays on **LITE**. No per-MAU reprice for agents.
- **Cost:** ~**$0/mo**. Marginal DynamoDB (a handful of tiny items, on-demand → pennies) +
  Lambda invocations (scale-to-zero) + reuse existing SES. Optional KMS CMK for the secret
  ~$1/mo if you don't use the free AWS-owned DynamoDB SSE key.
- **Reversibility:** ✅ Full. Revert the Lambda chain to single-round + drop the TOTP items.
  Agents never affected.
- **Genuine second factor?** ✅ Yes — "something you have" (authenticator app), distinct from
  the email possession of round 1.
- **Cons:** we own the TOTP crypto + a small DynamoDB item type + an enrollment UX; secret
  recovery/reset flow needed (admin-reset, mirroring the existing admin-only recovery posture).

### Option B — Fix the drift: ESSENTIALS + native WebAuthn passkeys (HA-1 as intended)

Reconcile SEC-DRIFT-1 so passkeys actually enroll, and make the human first factor a passkey
(`user_verification=required` → possession-of-device + local biometric/PIN). A passkey is a
strong, **phishing-resistant** authenticator — arguably a stronger *primary* factor than
"email + TOTP", though it is technically a single (multi-property) factor, not a literal 2FA.

- **What it actually takes** (the provider silently no-op'd tier/WebAuthn under 5.100):
  1. Either upgrade the AWS provider to a version that correctly applies `user_pool_tier` +
     `web_authn_configuration` + `sign_in_policy`, then `terraform apply`; **or** apply
     out-of-band with the CLI — `aws cognito-idp update-user-pool --user-pool-id
     eu-west-1_S1fUqxuKv --user-pool-tier ESSENTIALS` plus the `--web-authn-configuration
     RelyingPartyId=spec.elasticninja.com,UserVerification=required` and the `--sign-in-policy`
     allowed-first-auth-factors update (recent CLI required — the repo already shells the CLI
     for managed-login branding, so a capable CLI is present).
  2. Add a **post-apply assertion** (deploy-coordinator) that live `UserPoolTier=ESSENTIALS`
     and `WebAuthnConfiguration!=null` — the drift must never silently return.
- **Agent-safe?** ✅ *Functionally*, provided `PASSWORD` stays in `allowed_first_auth_factors`
  (`cognito.tf:284` already includes it) and the `agents` client is untouched. Agents' password
  flow keeps working.
  - ⚠️ **Cost coupling:** ESSENTIALS is a **pool-level** tier, so **every** active user in the
    pool — including the ~6 agent users who authenticate monthly — becomes billable MAU at the
    ESSENTIALS rate. At current scale that's cents; it grows with agent/human count.
- **Tier change?** ✅ Yes → **ESSENTIALS** (a paid plan). In-place `UpdateUserPool`, not a
  replacement (no users/groups lost).
- **Cost:** ESSENTIALS ≈ **$0.015 / MAU** (per the `cognito.tf:46-50` header note; confirm
  against the current AWS Cognito price list for eu-west-1, and note any promotional free-tier
  window). At today's scale ≈ 7 MAU → ~**$0.10/mo**. The real cost is *future* MAU growth and
  the fact that agents now count as ESSENTIALS MAU.
- **Reversibility:** ✅ Downgrade to LITE via `UpdateUserPool`, but you lose WebAuthn/ESSENTIALS
  features and any enrolled passkeys stop working. **0 human users today → the reset is a
  no-op**, so now is the cheapest possible time.
- **Second factor?** ⚠️ A passkey is a strong *primary* factor, not an additive *second* factor.
  Cognito does **not** prompt TOTP after a WEB_AUTHN first factor. So Option B *replaces* the
  weak factor with a strong one rather than *adding* one. If the requirement is literally "two
  factors", pair it with Option A's TOTP round (passkey primary + TOTP), or accept the passkey
  as the phishing-resistant answer.
- **Cons:** paid tier + the drift-reconciliation gymnastics (provider upgrade or CLI + assertion)
  + a WebAuthn register/authenticate ceremony in the SPA + the `webauthn_rp_id` domain-pin
  landmine (§6). Needs a **user cost decision**.

### Option C — Separate user pool for humans vs agents

Give humans their own pool (ESSENTIALS or REQUIRED-MFA, free to be as strict as you like),
leaving the agents pool exactly as-is on LITE.

- **Agent-safe?** ✅ Perfectly isolated — the agents pool is never touched, stays LITE-priced.
- **Cost:** a second user pool is free (MAU-priced); but **high engineering cost**: the API-GW
  JWT authorizer and the app-level validator today accept a **single** issuer/JWKS
  (`COGNITO_ISSUER`, `COGNITO_JWKS_URI`, audiences `[agents,ui]`). Two pools = two issuers →
  multi-issuer JWKS validation, duplicated groups/authz, duplicated SES + CUSTOM_AUTH wiring,
  duplicated invite/onboarding, new outputs, new secret. Large blast radius across `apigw.tf`,
  `lambda.tf`, the app validator, and the UI.
- **Reversibility:** possible but requires re-migrating humans + reverting dual-issuer support.
- **Verdict:** cleanest *isolation*, worst *complexity*. Overkill for one super-admin today.

### Option D — Separate app client for humans (does NOT work)

A separate app client does **not** isolate MFA: `mfa_configuration` is **pool-wide**, not
per-client. So a distinct human client cannot carry a stricter MFA policy than the agents
client. **Per-app-client MFA is not a thing** — ruled out. (A separate *pool*, Option C, is the
only clean isolation.)

### Ruled out — pool-wide REQUIRED MFA

Breaks every agent (§2.4). Never enable `mfa_configuration=ON/REQUIRED` on the shared pool.
Also avoid the repo's `enable_mfa=true` as-is: it sets `advanced_security_mode=ENFORCED` which
forces the **PLUS** tier (§2.5) and still doesn't protect the CUSTOM_AUTH human path.

---

## 5. Recommendation

**Primary: Option A — add a TOTP second round to the CUSTOM_AUTH chain.** It is the cleanest
agent-safe path: agent-safe *by construction* (agents never touch CUSTOM_AUTH), **no tier
change, ~$0 incremental cost**, fully reversible, delivers a *genuine* second factor
(authenticator app) on the human path that is actually used, and it is the design the code
already anticipates ("bird runs a two-round email-OTP → TOTP chain; deferred for HA-3"). It
does **not** depend on reconciling SEC-DRIFT-1 and does not require any pool `mfa_configuration`
change (so it can never leak an MFA challenge onto agents).

**Secondary / strategic: Option B (fix the drift → passkeys).** Still worth doing because it is
the intended end-state and gives *phishing-resistant* human auth (email-OTP and TOTP are both
phishable). But it is a **paid-tier decision** (ESSENTIALS, and it makes agent users billable
MAU) and carries the drift-reconciliation + RP-ID landmines. **Flag for the user:** the
ESSENTIALS per-MAU cost sign-off and the RP-ID/domain pin. Do it *because* there are 0 human
users today (cheapest time to enroll passkeys), not as the 2FA quick-win.

**Do not** pursue Option C/D or pool-wide REQUIRED MFA for this requirement.

A pragmatic sequencing: ship **Option A now** (immediate, zero-cost second factor for the
super-admin), then take **Option B** as a separate, user-approved hardening once the ESSENTIALS
cost is signed off — at which point admins get passkey-primary + TOTP-second, a strong 2FA.

---

## 6. Latent landmines found along the way

- **RP-ID mismatch (Option B):** `webauthn_rp_id` default is the old CloudFront host
  (`cognito.tf:135 = "do153mulmuok3.cloudfront.net"`) but tfvars now sets
  `spec.elasticninja.com` (`terraform.tfvars:37`). Passkeys are domain-pinned; enroll against
  the wrong RP-ID and every passkey silently fails from the real origin. Confirm the applied
  RP-ID matches the served UI origin (`spec.elasticninja.com`) before anyone enrolls.
- **`enable_mfa` drags in PLUS (§2.5):** the existing toggle couples OPTIONAL MFA with
  `advanced_security_mode=ENFORCED` → PLUS tier. Don't reach for it as the "2FA switch"; it is
  both more expensive than needed and ineffective for the CUSTOM_AUTH human path.
- **Custom-auth roles have no DynamoDB access:** Option A must add a *scoped* grant to only the
  verify/enroll Lambda (`customauth.tf` roles are least-privilege today). Keep the create/define
  roles DynamoDB-free.
- **Adapter parity is NOT affected:** this is Cognito/auth infra, not the Postgres/DynamoDB
  storage layer. The TOTP secret item (Option A) lives in the app DynamoDB table but is an
  auth-plane artifact; it does **not** touch the claim/reserve/optimistic-lock invariants or
  require a Postgres mirror. Call this out so no one "adds parity" where none is owed.
- **Fail-closed on enrollment gap (Option A):** decide the behaviour for a human who has an
  email-OTP identity but no TOTP secret yet — either force enrollment before issuing tokens, or
  a bounded grace window. DefineAuthChallenge already fails closed on any evaluation error
  (`define_auth_lambda/handler.py` `except Exception: failAuthentication=True`); keep that
  posture for "no TOTP secret found".
- **Post-apply drift assertion (Option B):** the root cause of SEC-DRIFT-1 is that the provider
  silently didn't apply tier/WebAuthn. Any Option-B apply MUST be followed by a live
  `describe-user-pool` assertion, or the drift returns unnoticed.

---

## 7. AWS cost summary

| Option | Tier | Incremental $/mo (today) | Cost driver | Reversible |
|---|---|---|---|---|
| **A · CUSTOM_AUTH TOTP round** | LITE (unchanged) | **~$0** (pennies DynamoDB; $1 if a KMS CMK) | none material | ✅ full |
| B · ESSENTIALS + passkeys | ESSENTIALS | ~$0.10 now (~$0.015/MAU × ~7); grows with MAU; **agents become billable MAU** | per-MAU tier | ✅ (0 humans → cheap) |
| C · Separate human pool | agents LITE / humans ESSENTIALS-or-PLUS | pool free; **high eng. cost** | dual-issuer engineering | ⚠️ migration |
| D · Separate app client | — | — | **doesn't work (MFA is pool-wide)** | — |
| REQUIRED pool MFA | — | — | **breaks agents** | — |

(Confirm the exact ESSENTIALS/PLUS per-MAU rate and any free-tier window against the current
AWS Cognito eu-west-1 price list at decision time; the $0.015 figure is the repo's own note,
not a fresh price-list read.)

---

## 8. SPEC-ready task breakdown

Atomic tasks for the orchestrator/spec-keeper to add via
`POST /api/v1/projects/spec-server/tasks`. `SPEC.md` is a generated mirror — do not hand-edit.
Sequenced; owners named. Reserve any migration/table number via the orchestrator reservation
API — never pick one by hand.

### Track A — RECOMMENDED: TOTP second factor in CUSTOM_AUTH (ship first)

- **A1 — Design the TOTP-in-CUSTOM_AUTH round (deep-diver/architecture-reviewer).**
  Nail the two-round DefineAuthChallenge state machine (email-OTP → TOTP), the DynamoDB item
  shape (`PK=USER#<sub>`, `SK=TOTP`, encrypted secret + created-at), the enrollment vs
  fail-closed policy for users without a secret, and secret reset/recovery (admin-only). Output:
  a one-page design note appended to this doc / DECISIONS.md. *No code.*
- **A2 — VerifyAuthChallengeResponse: add RFC-6238 TOTP verify (implementer).**
  Extend `verify_auth_lambda` with a constant-time TOTP compare (mirror `otp.py`'s
  `hmac.compare_digest` discipline; ±1 step skew). Reads the user's secret from DynamoDB.
- **A3 — CreateAuthChallenge / DefineAuthChallenge: sequence the TOTP round (implementer).**
  DefineAuthChallenge issues a second `CUSTOM_CHALLENGE` after a successful email-OTP round and
  only `issueTokens` after the TOTP round passes; CreateAuthChallenge tags the round `TOTP` and
  emits no code by email. Keep the fail-closed default. Bump `OTP_MAX_ATTEMPTS` handling to
  cover the TOTP round.
- **A4 — TOTP secret store + scoped IAM (aws-infra).**
  Reuse the existing `spec-server-dev-app` table (no new table) for the `USER#/TOTP` item;
  grant **only** the verify + enroll Lambda role scoped `GetItem`/`PutItem` on that table + key
  pattern. Keep define/create roles DynamoDB-free. Server-side encryption (AWS-owned key, or a
  KMS CMK if the cost is approved). *Terraform change; plan-only in this track, applied by
  deploy-coordinator in A7.*
- **A5 — Enrollment endpoint + UI (implementer + ui-reviewer).**
  An admin-authenticated "set up authenticator" flow: generate a secret, return an
  `otpauth://…` URI + QR, verify a first code before persisting. CSP-clean SPA step
  (ui-reviewer in the chain). Admin-only initially.
- **A6 — Parity/behaviour + auth tests (test-engineer).**
  Unit tests for the TOTP verify (known-answer vectors, skew, replay/expiry, constant-time),
  DefineAuthChallenge two-round transitions, and the "no secret → fail closed" path. Note
  explicitly: **no storage-adapter parity task is owed** (auth-plane, not the claim/reserve
  invariants).
- **A7 — Coordinated deploy + smoke (deploy-coordinator).**
  Deploy the three Lambdas + IAM in one wave; run the unauthenticated-route smoke check; then
  prove: (a) an **agent** can still mint a token via `USER_PASSWORD_AUTH`
  (`scripts/agent_token.py`) unchanged, and (b) a human sign-in now requires email-OTP **and**
  TOTP. This is the agent-safety gate.
- **A8 — Docs (documentation).** Update README "Secrets & tokens" / `AGENTS_API.md`
  "Authenticating" to describe human 2FA vs the unchanged agent flow; note MFA is NOT pool-wide
  Cognito MFA.

### Track B — STRATEGIC: fix SEC-DRIFT-1, enable passkeys (needs user cost sign-off)

- **B0 — USER DECISION (flag).** Approve the **ESSENTIALS per-MAU cost** (and that agent users
  become billable MAU), and confirm the WebAuthn **RP-ID = `spec.elasticninja.com`** / any
  branding/domain change. Blocks B1+.
- **B1 — Reconcile the tier/WebAuthn drift (aws-infra).**
  Either upgrade the AWS provider to a version that reliably applies `user_pool_tier` +
  `web_authn_configuration` + `sign_in_policy`, or apply out-of-band via CLI
  (`update-user-pool --user-pool-tier ESSENTIALS` + `--web-authn-configuration` +
  `--sign-in-policy`). Fix the `webauthn_rp_id` default (`cognito.tf:135`) to match tfvars.
  Keep `PASSWORD` in `allowed_first_auth_factors` so agents are untouched. *Plan-only here.*
- **B2 — Post-apply drift assertion (deploy-coordinator).**
  After apply, assert live `UserPoolTier=ESSENTIALS` and `WebAuthnConfiguration!=null` and
  `SignInPolicy` includes `PASSWORD,WEB_AUTHN`; fail the wave otherwise. Prevents silent
  re-drift.
- **B3 — SPA WebAuthn register/authenticate ceremony (implementer + ui-reviewer).**
  Native passkey enroll + sign-in in the React SPA against the ESSENTIALS pool; CSP-clean.
- **B4 — Agent-safety regression proof (test-engineer + deploy-coordinator).**
  Re-prove `USER_PASSWORD_AUTH` on the `agents` client still returns `AuthenticationResult`
  (no challenge) after the tier bump. Non-negotiable gate.
- **B5 — Docs (documentation).** Passkey enrollment + recovery; the ESSENTIALS cost note in the
  infra README.

(Sequence: ship **Track A** first for an immediate zero-cost second factor; take **Track B**
after B0 sign-off. If both land, humans get passkey-primary + TOTP-second — real, phishing-
resistant 2FA — while agents keep `USER_PASSWORD_AUTH` untouched throughout.)

---

## 9. Risk / rollback

- **Track A rollback:** revert the three Lambda versions to the single-round chain and stop
  reading/writing the `USER#/TOTP` item; instantaneous, agents never involved. Lowest-risk
  change of the three options.
- **Track B rollback:** `UpdateUserPool --user-pool-tier LITE` returns to the free plan (loses
  ESSENTIALS features + any enrolled passkeys). Cheap **today** (0 human users); gets expensive
  once humans have enrolled passkeys — decide before onboarding humans.
- **Cross-cutting agent-safety gate (both tracks):** every deploy MUST re-run
  `scripts/agent_token.py` for a real agent and confirm a token is minted with **no** MFA/NEW_
  PASSWORD challenge. If a challenge ever appears, roll back immediately — that is the agents
  breaking.
- **Never** set `mfa_configuration=ON/REQUIRED` on the shared pool, and don't flip the existing
  `enable_mfa=true` (it forces PLUS and doesn't protect the human path).

---

## 10. Residual unknowns

- The **live** pool state was not re-confirmed this session (sanctioned read-only AWS profiles
  absent locally). aws-infra should re-run `describe-user-pool` / `describe-user-pool-client`
  before acting to confirm SEC-DRIFT-1 still holds.
- The "native MFA does not interpose on CUSTOM_AUTH" premise is from Cognito's documented
  design, not a live test — verify with the throwaway-pool disproof test in §3 if you want
  certainty before building Track A.
- The exact ESSENTIALS/PLUS per-MAU price + free-tier window for eu-west-1 should be read from
  the current AWS price list at decision time (the $0.015 figure is the repo's own note).
- Whether AWS provider ≥ a specific version reliably applies `user_pool_tier`/WebAuthn (vs the
  5.100 silent no-op) needs a quick provider-changelog check before choosing provider-upgrade
  vs CLI for B1.
