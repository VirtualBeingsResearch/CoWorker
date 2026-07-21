# Security Policy

[中文](SECURITY.zh-CN.md) · English

## Supported versions

Security fixes are made on the default branch and released in the newest version. Older releases
are not maintained unless a release note says otherwise.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's **Security → Report a vulnerability**
flow. Include the affected version, impact, reproduction steps, and any suggested mitigation.

If private vulnerability reporting is unavailable, open a public issue containing only a request
for a private contact channel. Do not include exploit details, credentials, personal data, or logs
that may contain secrets in a public issue.

Please allow the maintainers time to confirm and fix the issue before public disclosure. We will
credit reporters who want to be acknowledged.

## Security model

Coworker is an autonomous agent, not a security sandbox. Its tools can execute commands and read or
write files with the permissions of the operating-system user running it. Model output and content
from webpages, messages, attachments, skills, and memory must therefore be treated as untrusted.

See [Data and trust boundaries](docs/architecture/data-boundaries.en.md) for what is stored locally, what may leave
the machine, and what the cleanup script does and does not remove.

For the current v0.x releases:

- Run Coworker as a dedicated, least-privileged user or inside an isolated container or VM.
- Give it access only to disposable or backed-up workspaces.
- Do not provide production credentials unless the deployment is specifically isolated for them.
- The API binds to `127.0.0.1` and requires the Desktop communication Bearer token by default.
  If you expose it through a reverse proxy, terminate TLS there, set an explicit `API__HOST`,
  configure `API__CORS_ORIGINS` to trusted browser origins, and set a strong
  `API__COMMUNICATION_TOKEN`.
- `API__DEVELOPMENT_MODE=true` disables Desktop Bearer and HTTPS checks. Use it only for a
  deliberately local HTTP setup; never enable it on a shared or public listener.
- Do not expose port 8000 directly to the public internet or an untrusted network. The admin token
  protects the management API, but it is not a complete authorization boundary for every route.
- Keep `.env`, `providers.json`, runtime data, logs, exported configuration, and desktop credentials
  out of commits and vulnerability reports.

Reports about authentication bypasses, command or path traversal, secret disclosure, unsafe update
handling, and escapes from documented permission boundaries are especially welcome.
