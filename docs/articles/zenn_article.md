---
title: "helix-agent を helix-agents に進化させた: Ollama / Codex / OpenAI-compatible を 1 つの MCP で切り替える"
emoji: "🤖"
type: "tech"
topics: ["mcp", "claudecode", "ollama", "codex", "python"]
published: true
---

`helix-agent` はもともと、Claude Code からローカル Ollama モデルへタスクを委譲するための MCP サーバーでした。

今回それを **helix-agents** として進化させ、1 つの MCP サーバーから次を切り替えられるようにしました。

- `ollama`
- `codex`
- `openai-compatible`

## 何を変えたか

単に provider を増やしただけではありません。

Claude Code で自然に扱えるように、background agent の lifecycle も追加しました。

- `spawn_agent`
- `send_agent_input`
- `wait_agent`
- `list_agents`
- `close_agent`

これで、単発のツール呼び出しではなく、継続する作業者として扱いやすくなります。

## 使い分け

### Ollama

- ローカル推論
- 低コストな下書き
- Vision

### Codex

- repo をまたぐ実装
- コードレビュー
- 変更と検証を伴うタスク

### OpenAI-compatible

- API ベースの chat model
- 標準的な chat completions 互換サーバー

## 使い方の例

### Codex でレビュー

```text
think(
  task="この差分の回帰リスクを見て",
  provider="codex",
  cwd="/repo"
)
```

### Ollama でローカル要約

```text
think(
  task="このログを要約して",
  provider="ollama"
)
```

### 調査用 background agent

```text
spawn_agent(
  description="flaky test 調査",
  provider="codex",
  agent_type="explorer"
)
```

その後に:

```text
send_agent_input(...)
wait_agent(...)
close_agent(...)
```

## セットアップ

```bash
git clone https://github.com/tsunamayo7/helix-agent.git
cd helix-agent
uv sync
uv run python server.py
```

Claude Code 側は次のように設定します。

```json
{
  "mcpServers": {
    "helix-agents": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/helix-agent", "python", "server.py"]
    }
  }
}
```

## 補足

- Codex を使うには `codex` CLI が `PATH` に必要です
- OpenAI-compatible は API キーが必要です
- Vision は現状 Ollama 経路中心です

GitHub:
https://github.com/tsunamayo7/helix-agent
