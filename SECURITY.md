# Security Policy

## Supported versions

Only the most recent release tag receives security fixes. v1.x is
pre-release development — fixes are applied on the main branch and
shipped with the next release tag.

## Reporting a vulnerability

**Channel: GitHub Private Vulnerability Reporting.** Open a private security
advisory at the repository's Security → Advisories → New draft advisory. This
is the only private reporting channel and keeps the report tracked alongside
the codebase.

If GitHub Private Vulnerability Reporting is not yet enabled on the public
repository, open a public GitHub issue titled exactly
**"Private security contact request"** with no technical details; a
maintainer will respond there with a private channel for follow-up. Do
**not** include vulnerability details, repro steps, or impact information
in that initial public issue.

Do **not** file a public GitHub issue with the vulnerability details for any
security topic. Disclosure follows responsible-disclosure norms: a private
90-day window by default, with earlier coordinated release if an active
exploit is confirmed.

Please include:
- Description of the vulnerability and affected component
- Steps to reproduce
- Potential impact assessment

## Threat model summary

### Access posture

VocalizeAI is **self-deploy**: each operator runs their own backend on
their own infrastructure and is responsible for restricting access to it
(reverse-proxy auth, VPN, Cloudflare Access policy, etc.). The codebase
does not ship a built-in authentication gate. Per-user authentication is
v1.x scope (requirement `AUTH-01`).

### Network layer

- TLS termination at the Cloudflare edge via Universal SSL.
- A `cloudflared` tunnel connects the Pi orchestrator to the Cloudflare
  edge; all external traffic enters through Cloudflare before reaching
  the backend.

### Backend security controls

- **CORS**: single allowed origin (set via `VOCALIZE_CORS_ORIGINS`, e.g.
  `https://vocalize.example.com`) in production; localhost origins preserved in dev mode via
  `VOCALIZE_HOST` env-conditional config. `allow_methods` restricted to
  `["GET", "POST", "DELETE"]`.
- **WS base URL enforcement**: server raises at startup if
  `VOCALIZE_HOST != "127.0.0.1"` and `VOCALIZE_WS_BASE_URL` is unset,
  preventing Host-header spoofing.
- **Task length bound**: `SetTaskRequest.task` has a `max_length=2000`
  field constraint to limit prompt-injection surface area.

## Known limitations

### No built-in authentication in v1

VocalizeAI v1 ships no request-level auth on `POST /api/sessions` or
the WebSocket endpoint. Self-deploy operators MUST restrict reachability
at the network or proxy layer until per-user authentication lands (v1.x
scope; requirement `AUTH-01`).

### No prompt-injection mitigation

User-supplied task descriptions are passed to the LLM without sanitization.
This is a known gap flagged for v1.x scope.

## Emergency rollback for leaked secret in public mirror

If a secret (API key, tunnel ID, third-party token) is accidentally
committed to the public repo:

1. **Rotate the leaked secret immediately** per the secret's own rotation
   procedure (provider dashboard for API keys, `cloudflared` rotate for
   tunnel credentials, etc.).

2. **Force-push the public `main`** to a re-sanitized commit by re-running
   the `sync-private-to-public` skill against a clean private tree. This
   overwrites the public history and removes the leaked commit.

3. **Contact GitHub Support** to purge cached references:
   https://support.github.com/contact/github-private-information-removal

4. **Audit the blast radius**: determine whether the secret was used by
   any unauthorized party before rotation.
