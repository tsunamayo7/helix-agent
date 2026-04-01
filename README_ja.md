# helix-agents

Claude Code 向けの、複数 LLM プロバイダにタスクを委譲できる MCP サーバーです。

元の `helix-agent` は Ollama 中心でしたが、`helix-agents` では次のプロバイダを切り替え可能にしています。

- `ollama`
- `codex`
- `openai-compatible`

## 主な変更

- `provider="auto" | "ollama" | "codex" | "openai-compatible"` で切り替え可能
- Claude Code 風の背景エージェント lifecycle を追加
- provider の状態確認と既定 provider の切り替えを追加
- Ollama 専用だった設計を維持互換しつつ Codex と API 系も追加

## ツール

### `think`

単発の推論、要約、レビュー、コード生成、調査に使います。

### `agent_task`

複数ステップの作業に使います。

- `ollama` と `openai-compatible` は内蔵 ReAct loop を使用
- `codex` は自律的な実装/レビュー agent として動作

### `see`

画像解析です。現状は `ollama` が最も自然です。

### `providers`

利用可能な provider を確認したり、既定 provider を切り替えます。

### `models`

provider ごとのモデル一覧確認やモデル固定に使います。

### 背景エージェント系ツール

- `spawn_agent`
- `send_agent_input`
- `wait_agent`
- `list_agents`
- `close_agent`

これにより、単発 bridge ではなく Claude Code の sub-agent に近い使い方ができます。

## セットアップ

```bash
git clone https://github.com/tsunamayo7/helix-agent.git
cd helix-agent
uv sync
uv run python server.py
```

Claude Code には次のように追加します。

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

## 設定

`config(action="show")` で確認できます。

主なキー:

- `default_provider`
- `ollama_host`
- `codex_model`
- `codex_sandbox`
- `openai_base_url`
- `openai_api_key_env`
- `openai_model`

例:

```text
providers(action="use", provider="codex")
models(action="list", provider="ollama")
config(action="set", key="openai_model", value="gpt-4.1")
think(task="この差分をレビューして", provider="codex", cwd="/repo")
spawn_agent(description="flaky test 調査", provider="codex", agent_type="explorer")
```

## 補足

- Codex 経路は `codex` CLI が `PATH` に必要です
- OpenAI-compatible 経路は API キーが必要です
- Vision は現状 Ollama 経路のみ実装しています
