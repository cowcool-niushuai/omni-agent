# Security Policy

## Supported versions

Only the latest `main` branch and the latest tagged release receive security
fixes.

## Reporting a vulnerability

Please do **not** open a public GitHub issue for security problems.

Use GitHub's **Security → Advisories → Report a vulnerability** flow for this
repository. Include:

- affected version or commit
- reproduction steps
- expected and actual behavior
- impact assessment
- whether secrets, local files, or host commands are involved

Please avoid attaching real API keys, tokens, passwords, databases, or private
documents. Redact secrets in logs and screenshots.

## Security model

OmniAgent is a local-first agent platform. The default profile is intentionally
conservative:

- commands run in a bubblewrap sandbox when available, without a shell
- network access is disabled for command execution by default
- high-risk tools require explicit approval
- tool calls are written to an audit trail
- project folders and Git repositories are contained by policy
- API tokens and model provider keys are stored outside the repository

The optional **Full Access** mode disables the sandbox and approval prompts for
the active project. It is intended for trusted development machines only. Do
not enable it on a shared or Internet-exposed deployment.

## Secrets

These files must never be committed:

```text
.env
model_config.json
mcp_config.json
omni_agent.db
data/
workspace/
desktop/release/
desktop/resources/backend/
```

If a secret is committed, rotate it first, then remove it from history. A normal
`git rm` is not sufficient once the commit is public.
