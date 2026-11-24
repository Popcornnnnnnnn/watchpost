# Security Policy

## Reporting a Vulnerability

If you discover a security issue in Watchpost (for example: approval-gate bypass, command/sandbox escape, webhook signature bypass, prompt injection breakout, or secret leakage), **please do not open a public issue with exploit details**.

Instead, open a private security advisory on GitHub:
https://github.com/Popcornnnnnnnn/watchpost/security/advisories/new

Please include:
- A concise summary of the affected component and Watchpost version.
- Steps or a minimal offline test reproducing the issue.
- Potential impact under `assistant_only`, `diagnose_only`, or `auto_fix` channel modes.

## Supported Versions

| Version | Supported |
| --- | --- |
| `0.4.x` (`watchpost-agent`) | Yes |
| `< 0.4.0` | Security fixes applied on `0.4.x` |

## Security Architecture Notes

- All `/api/*` control routes (except signed `/api/webhooks/*`) require the per-installation `0o600` Bearer token (`~/.local/share/watchpost/api.token`), loopback `Host` header, and reject browser `Origin` headers.
- Expose only `webhook_port` (`create_webhook_app`)—not `api_port`—when terminating public reverse tunnels for DingTalk or WeCom callbacks.
- Store credentials in `0o600` `~/.config/watchpost/.env` or environment variables via `${ENV_VAR}` placeholders rather than plaintext inside `config.json`.
