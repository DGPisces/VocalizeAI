# Security Policy

## Supported versions

Only the most recent release tag receives security fixes. v1.x is
pre-release development — fixes are applied on the main branch and
shipped with the next release tag.

## Reporting a vulnerability

**Primary channel: GitHub Private Vulnerability Reporting.** Once enabled on
the public repository (post-Phase-9 setting), open a private security advisory
at Security → Advisories → New draft advisory. This is the preferred route — it
keeps the report private and tracked alongside the codebase.

**Backup channel: email.** Until Private Vulnerability Reporting is enabled, or
if you cannot use it, email
[40358663+DGPisces@users.noreply.github.com](mailto:40358663+DGPisces@users.noreply.github.com) with subject prefix
`[VocalizeAI Security]`.

Do **not** file a public GitHub issue for security topics. Disclosure
follows responsible-disclosure norms: a private 90-day window by default,
with earlier coordinated release if an active exploit is confirmed.

Please include:
- Description of the vulnerability and affected component
- Steps to reproduce
- Potential impact assessment

## Threat model summary

### Access posture

v1 uses a shared invite-token model. `POST /api/sessions` requires an
`X-Invite-Token` header whose value must match `VOCALIZE_INVITE_TOKEN`
configured server-side. This is an intentional design for v1 (requirement
`AUTH-DEFER-TO-v1.x`); per-user authentication is v1.x scope (requirement
`AUTH-01`).

### Network layer

- TLS termination at the Cloudflare edge via Universal SSL.
- A `cloudflared` tunnel connects the Pi orchestrator to the Cloudflare
  edge; all external traffic enters through Cloudflare before reaching
  the backend.

### Backend security controls

- **CORS**: single allowed origin (`https://vocalize.dgpisces.com`) in
  production; localhost origins preserved in dev mode via
  `VOCALIZE_HOST` env-conditional config. `allow_methods` restricted to
  `["GET", "POST", "DELETE"]`.
- **WS base URL enforcement**: server raises at startup if
  `VOCALIZE_HOST != "127.0.0.1"` and `VOCALIZE_WS_BASE_URL` is unset,
  preventing Host-header spoofing.
- **Task length bound**: `SetTaskRequest.task` has a `max_length=2000`
  field constraint to limit prompt-injection surface area.

## Known limitations

### Shared invite token in JS bundle

`NEXT_PUBLIC_VOCALIZE_INVITE_TOKEN` is embedded in the Next.js JS bundle
at build time by design — this is inherent to the shared-token model.
It is not a vulnerability; the token IS the gate. Anyone who can load the
page already has the token. Per-user secrets are v1.x scope (AUTH-01).

### No token rotation flow in v1

The invite token is a long-lived shared secret. Rotation requires a
deploy-time env-var update plus a frontend rebuild. There is no in-band
rotation API in v1.

### No prompt-injection mitigation

User-supplied task descriptions are passed to the LLM without sanitization.
This is a known gap flagged for v1.x scope.

## Emergency rollback for leaked secret in public mirror

If a secret (invite token, API key, tunnel ID) is accidentally committed
to the public repo:

1. **Rotate the leaked secret immediately.**
   - `VOCALIZE_INVITE_TOKEN`: update Pi `.env`, restart `vocalize` service,
     rebuild frontend with the new `NEXT_PUBLIC_VOCALIZE_INVITE_TOKEN`.
   - Other secrets: follow their respective rotation procedures.

2. **Force-push the public `main`** to a re-sanitized commit by re-running
   the `sync-private-to-public` skill against a clean private tree. This
   overwrites the public history and removes the leaked commit.

3. **Contact GitHub Support** to purge cached references:
   https://support.github.com/contact/github-private-information-removal

4. **Audit the blast radius**: determine whether the secret was used by
   any unauthorized party before rotation.
