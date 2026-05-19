# helix-agent Demo: Cut Claude Code tokens 97%
Output demo.gif
Set FontSize 16
Set Width 1000
Set Height 500
Set Theme "Catppuccin Mocha"
Set Padding 20

Type "# helix-agent: Cut Claude Code token usage 82-97%"
Enter
Sleep 1.5s

Type "# Screenshot: 15,000 → 400 tokens (97% saved)"
Enter
Sleep 1s

Type "# DOM snapshot: 114,000 → 500 tokens (99% saved)"
Enter
Sleep 1.5s

Type "git clone https://github.com/tsunamayo7/helix-agent && cd helix-agent"
Enter
Sleep 0.5s

Type "uv sync && ollama pull gemma4:e2b"
Enter
Sleep 0.5s

Type "uv run python server.py  # MCP server ready"
Enter
Sleep 2s

Type "# 27 tools | 367 tests | $0 cost | MIT license"
Enter
Sleep 2s

Type "# github.com/tsunamayo7/helix-agent"
Enter
Sleep 3s
