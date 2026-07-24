# UI security guardrails

## Auth tokens live in memory — XSS = full session theft

The dashboard holds the Cognito access / id / refresh tokens **in memory only**
(`src/auth/session.ts`); they are never written to `localStorage` /
`sessionStorage` (only an opt-in *email* string is persisted, never a token).
That design deliberately trades "survives a reload" for "not readable by a
stray script". The corollary is strict:

> **Any XSS in this SPA is token-exfiltration-grade — one injected script reads
> the in-memory session and steals a live, authenticated session.** There is no
> httpOnly-cookie backstop.

## Automatic P0 — requires explicit security review before merge

Because of the above, introducing any of the following is treated as an
**automatic P0 finding** and MUST NOT merge without explicit sign-off from the
`security` agent (and `ui-reviewer`):

- `dangerouslySetInnerHTML`, `Element.innerHTML` / `outerHTML`,
  `insertAdjacentHTML`, `document.write`, or any equivalent raw-HTML sink.
- Adding a Markdown, HTML, or template renderer that emits untrusted content as
  HTML (e.g. `marked`, `markdown-it`, `dompurify`-gated or not — the renderer
  itself is the trigger for review, not a free pass).
- Relaxing the Content-Security-Policy toward `'unsafe-inline'` or
  `'unsafe-eval'` (in the CloudFront header `infra/terraform/cloudfront.tf`
  **or** the travelling `<meta>` baseline in `ui/index.html`), or widening
  `script-src` / `connect-src` to a new third-party origin.
- `eval`, `new Function(...)`, or building a `<script>`/URL from user-supplied
  strings.

The safe default is React's text interpolation (which escapes) plus the CSP
above; keep rendering user/content strings as **text**, never HTML. If a real
need for one of the above arises, raise it explicitly for security review — do
not slip it in under an unrelated task.
