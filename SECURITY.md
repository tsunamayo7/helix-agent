# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.15.x  | :white_check_mark: |
| < 0.15  | :x:                |

## Reporting a Vulnerability

If you discover a security vulnerability, please report it responsibly:

1. **Do NOT** open a public issue
2. Email: tsuna.konomiya@gmail.com
3. Include: description, steps to reproduce, potential impact
4. Expected response time: 48 hours

## Security Measures

- Default routine inference and compression run locally via Ollama
- Cloud AI providers (Claude, etc.) are used only when explicitly enabled by the user
- Sensitive inputs should be routed through local processing or redacted before external calls
- Retry loop detection prevents infinite API calls
- Input validation on all MCP tool parameters
- No secrets stored in code — environment variables only
