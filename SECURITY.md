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

- All LLM inference runs locally (Ollama) — no data sent to external services
- Retry loop detection prevents infinite API calls
- Input validation on all MCP tool parameters
- No secrets stored in code — environment variables only
