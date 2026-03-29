# helix-agent エージェント化 技術調査レポート

> 調査日: 2026-03-29
> 対象: helix-agent v0.3.0 のエージェント化に向けた深掘り調査

---

## 1. FastMCP 2.x / 3.x 最新API詳細

### 1.1 Context オブジェクト 全メソッド

FastMCP の `Context` はツール/リソース/プロンプト実行内で自動注入され、以下のAPIを提供する。

```python
from fastmcp import Context

@mcp.tool()
async def my_tool(task: str, ctx: Context) -> str:
    # === ログ ===
    await ctx.log("info", "処理開始")        # debug/info/warning/error

    # === プログレス通知 ===
    await ctx.report_progress(24, 100)        # progress, total

    # === リソース読み取り ===
    content = await ctx.read_resource("file://path/to/resource")

    # === セッション状態（セッションスコープで永続） ===
    await ctx.set_state("key", {"data": 123})           # JSON-serializable
    value = await ctx.get_state("key")
    await ctx.set_state("client", obj, serializable=False)  # 非直列化オブジェクト

    # === コンポーネント可視性（セッション単位） ===
    ctx.enable_components(["tool_a", "tool_b"])   # このセッションのみ有効化
    ctx.disable_components(["tool_c"])
    ctx.reset_visibility()

    # === セッション情報 ===
    session_id = ctx.session_id
    request_id = ctx.request_id

    # === 低レベルセッションアクセス ===
    await ctx.session.send_progress_notification(token, progress, total, message)
```

**helix-agent への適用:**
- `report_progress` はエージェントループの各ステップ進捗通知に使える
- `set_state` でエージェントの会話履歴をセッション内で保持可能
- `read_resource` で MCP resources 経由のファイルアクセスを実装可能

### 1.2 エラーハンドリング・リトライ機構

FastMCP はミドルウェアとして提供:

```python
from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import ErrorHandlingMiddleware, RetryMiddleware

mcp = FastMCP("helix-agent")

# エラーハンドリング: 例外→MCP エラーレスポンスに変換
mcp.add_middleware(ErrorHandlingMiddleware(
    include_traceback=True,      # デバッグ用
    transform_errors=True,       # MCP準拠に変換
))

# リトライ: 指数バックオフ付き自動リトライ
mcp.add_middleware(RetryMiddleware(
    max_retries=3,
    retry_exceptions=(ConnectionError, TimeoutError),
))
```

**注意:** ミドルウェアは FastMCP 独自機能で、MCP仕様外。

### 1.3 長時間実行ツールの問題

**既知の問題:** FastMCP は5秒以上のツール実行で結果を返さないバグが報告されている ([Issue #2845](https://github.com/PrefectHQ/fastmcp/issues/2845))。

**回避策:**
- クライアント/サーバーの read timeout を 60-180秒に引き上げ
- `report_progress` でハートビートを送信し、接続維持
- Ollama の推論は 20-120秒かかるため、helix-agent では特に重要

### 1.4 MCP Resources によるファイルアクセス

```python
@mcp.resource("file://{path}")
async def read_file(path: str) -> str:
    """エージェントがファイルを読む"""
    safe_path = Path(path).resolve()
    # セキュリティ: allowlist ディレクトリ内のみ許可
    if not str(safe_path).startswith(str(ALLOWED_ROOT)):
        raise ValueError(f"Access denied: {path}")
    return safe_path.read_text(encoding="utf-8")
```

**参考実装:** [fastmcp-file-server](https://github.com/Luxshan2000/fastmcp-file-server) — 多層認証・stdio/HTTP/公開モード対応

### Sources
- [FastMCP Context 公式ドキュメント](https://gofastmcp.com/servers/context)
- [FastMCP Middleware](https://gofastmcp.com/servers/middleware)
- [FastMCP Error Handling Middleware](https://gofastmcp.com/python-sdk/fastmcp-server-middleware-error_handling)
- [FastMCP Resources](https://gofastmcp.com/servers/resources)
- [context.py ソースコード](https://github.com/jlowin/fastmcp/blob/main/src/fastmcp/server/context.py)
- [長時間実行ツールの問題 Issue #2845](https://github.com/PrefectHQ/fastmcp/issues/2845)

---

## 2. Ollama の最新 tools/function calling 情報

### 2.1 Ollama Tools API 対応状況 (2026年3月)

Ollama は v0.3.0 から tools API をサポート。使用方法:

```python
# Ollama Python SDK
import ollama

response = ollama.chat(
    model='qwen3',
    messages=[{'role': 'user', 'content': '東京の天気は？'}],
    tools=[{
        'type': 'function',
        'function': {
            'name': 'get_weather',
            'description': 'Get weather for a city',
            'parameters': {
                'type': 'object',
                'properties': {
                    'city': {'type': 'string', 'description': 'City name'}
                },
                'required': ['city']
            }
        }
    }]
)
# response.message.tool_calls にツール呼び出しが返る
```

### 2.2 モデル別 Function Calling 信頼性

**2026年3月の実測データ** ([I Tested 13 Local LLMs on Tool Calling](https://www.jdhodges.com/blog/local-llms-on-tool-calling-2026-pt1-local-lm/)):

| モデル | サイズ | Tool Calling 精度 | 備考 |
|--------|--------|-------------------|------|
| **Qwen3.5 4B** | 3.4GB | **97.5%** | 最高スコア、小型で驚異的 |
| Nemotron Nano 4B | ~3GB | 95.0% | 高い信頼性 |
| GLM-4.7-Flash | ~4GB | 95.0% | 安定 |
| Mistral Nemo 12B | ~8GB | 92.5% | マルチターンに強い |
| **Mistral Small 3.2** | 15GB | **42.5%** | **Vision は優秀だが tool calling は低い** |

### 2.3 Qwen3.5 の重大なバグ (Ollama)

**問題:** Ollama が Qwen3.5 に間違った tool calling パイプラインを適用している。

- Qwen3.5 は **Qwen3-Coder XML フォーマット** (`<function=name>`) で訓練された
- Ollama は **Qwen3 Hermes-style JSON** パイプライン（Qwen3VLRenderer）を使用
- `</think>` タグが閉じられず、以降のターンが壊れる

**ワークアラウンド:**
1. Ollama v0.17.5 で一部修正
2. 正しいパイプライン（Qwen3CoderRenderer + Qwen3CoderParser）は存在するが未適用
3. ツール数5個以上で XML にフォールバックする問題あり

**helix-agent への影響:**
- Qwen3.5 の native tool calling は現時点で不安定
- **プロンプトベース ReAct パターンの方が安全**
- ネイティブ tool calling はモデル/バージョン依存のリスクが高い

### 2.4 Structured Output (format: "json") の信頼性

```python
# Ollama の structured output
response = await client.chat(
    model="qwen3",
    messages=[...],
    format={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["search", "calculate", "finish"]},
            "input": {"type": "string"}
        },
        "required": ["action", "input"]
    }
)
```

- JSON スキーマを `format` に渡すと、そのスキーマに準拠した JSON のみ生成される
- 小型モデル (4B-9B) でも JSON 構造の準拠率は高い
- **ReAct のアクション出力に最適**: `{"thought": "...", "action": "...", "input": "..."}` の形式を強制可能

### 2.5 プロンプトベース ReAct の推奨実装パターン

```python
REACT_SYSTEM_PROMPT = """You are an AI assistant that solves tasks step by step.

Available tools:
{tool_descriptions}

For each step, respond in this exact JSON format:
{
  "thought": "your reasoning about what to do next",
  "action": "tool_name",       // or "finish" when done
  "action_input": "input to the tool"
}

When you have the final answer:
{
  "thought": "I now have the answer",
  "action": "finish",
  "action_input": "the final answer"
}
"""

# format パラメータでJSON準拠を強制
result = await client.chat(
    model=selected_model,
    messages=messages,
    format_json=True,  # or schema-based format
    temperature=0.1,   # 低温で確実性向上
)
```

### Sources
- [Ollama Tool Calling ドキュメント](https://docs.ollama.com/capabilities/tool-calling)
- [I Tested 13 Local LLMs on Tool Calling](https://www.jdhodges.com/blog/local-llms-on-tool-calling-2026-pt1-local-lm/)
- [Qwen3.5 Tool Calling Issue #14493](https://github.com/ollama/ollama/issues/14493)
- [Qwen3.5 XML Issue #14745](https://github.com/ollama/ollama/issues/14745)
- [Structured Output ガイド](https://python.useinstructor.com/integrations/ollama/)
- [Ollama + Qwen3 Structured Output](https://medium.com/@rosgluk/constraining-llms-with-structured-output-ollama-qwen3-python-or-go-2f56ff41d720)

---

## 3. 成功しているOSSエージェントの実装パターン

### 3.1 smolagents (HuggingFace)

**特徴:** エージェントロジックが ~1,000行に収まる極めてシンプルな設計

**エージェントループ:**
```
1. ユーザータスク受信
2. while step < max_steps:
   a. LLM にメッセージ履歴を送信
   b. LLM がPythonコードスニペットを生成（ReAct形式）
   c. コードをサンドボックスで実行
   d. 実行結果を履歴に追加
   e. "final_answer" が呼ばれたら終了
3. 最終回答を返す
```

**学ぶべき点:**
- CodeAgent は JSON/XML ではなく **Python コード** でツールを呼ぶ → 30%少ないステップで高精度
- `max_steps` (デフォルト6) でループ制限
- サンドボックス実行: Modal, Docker, E2B 対応
- ローカルLLM: Ollama 対応だが、小型モデルでは品質課題あり

**ソース:** [smolagents/src/smolagents/agents.py](https://github.com/huggingface/smolagents/blob/main/src/smolagents/agents.py)

### 3.2 pydantic-ai (Pydantic)

**特徴:** pydantic-graph ベースの型安全なエージェントグラフ

**エージェントループ:**
```python
# pydantic-ai のグラフ実行ループ
async with agent.run(prompt) as run:
    node = start_node
    while True:
        result = await run.next(node)
        if isinstance(result, End):
            return result.output
        node = result  # 次のノードへ
```

**学ぶべき点:**
- 型ヒントベースのグラフ定義 → エッジは戻り値の型で自動推定
- Durable Execution: 一時的なAPIエラーを跨いで進捗を保持
- ストリーミング出力 + 即座のバリデーション
- Ollama サポートあり（LiteLLM 経由）

**ソース:** [pydantic/pydantic-ai](https://github.com/pydantic/pydantic-ai)

### 3.3 browser-use

**特徴:** イベント駆動アーキテクチャのブラウザ自動化エージェント

**エージェントループ:**
```
1. タスク受信 → Agent (service.py) がオーケストレーション
2. while not done:
   a. DOM 状態を取得 (DomService)
   b. LLM にアクションを決定させる
   c. max_actions_per_step (デフォルト3) まで連続実行
   d. ページ変化検出 or max_failures (デフォルト3) で制御
3. flash_mode: 評価スキップ、メモリのみで高速実行
```

**学ぶべき点:**
- `max_actions_per_step` で1ステップ内の並列アクション数を制限
- `max_failures` でエラーリカバリ回数を制御
- flash_mode: 軽量モードの切り替えパターン
- マルチプロバイダー LLM 抽象化レイヤー

**ソース:** [browser-use/browser-use](https://github.com/browser-use/browser-use)

### 3.4 CrewAI

**特徴:** ロール定義型マルチエージェント、Ollama + LiteLLM 統合

**アーキテクチャ:**
```
Crew (オーケストレーター)
  ├── Agent (Researcher, role="調査員")
  │     └── LLM: ollama/qwen3:14b
  ├── Agent (Writer, role="ライター")
  │     └── LLM: ollama/gemma3:12b
  └── Task → Agent 割り当て → 順次/並行実行
```

**学ぶべき点:**
- ロール・ゴール・バックストーリーによるエージェント定義
- メモリ管理: 短期/長期/エンティティメモリ
- 最低32GB RAM推奨（マルチエージェント時）
- LiteLLM 経由で `ollama/model_name` の形式で統合

**ソース:** [crewAIInc/crewAI](https://github.com/crewAIInc/crewAI)

### 3.5 パターン比較まとめ

| 項目 | smolagents | pydantic-ai | browser-use | CrewAI |
|------|-----------|-------------|-------------|--------|
| ループ行数 | ~1,000行 | Graph基盤 | ~500行 | Framework大 |
| ツール形式 | Python code | Pydantic schema | DOM actions | 関数デコレータ |
| エラーリカバリ | max_steps | Durable Exec | max_failures | リトライ |
| ローカルLLM | Ollama対応 | LiteLLM対応 | 限定的 | LiteLLM対応 |
| 採用推奨度 | **高** | 中 | 低 | 低 |

**結論:** smolagents の ReAct ループ + JSON structured output が helix-agent に最もフィットする。

### Sources
- [smolagents GitHub](https://github.com/huggingface/smolagents)
- [pydantic-ai GitHub](https://github.com/pydantic/pydantic-ai)
- [browser-use GitHub](https://github.com/browser-use/browser-use)
- [crewAI GitHub](https://github.com/crewAIInc/crewAI)
- [smolagents 公式ドキュメント](https://huggingface.co/docs/smolagents/en/index)
- [CrewAI LLM Connections](https://docs.crewai.com/en/learn/llm-connections)
- [CrewAI + Ollama + Arize Phoenix](https://cambazm.medium.com/building-a-simple-local-agent-stack-crewai-ollama-and-arize-phoenix-554aa822a6c5)

---

## 4. MCP エコシステムの最新動向

### 4.1 MCP 2026 ロードマップ

**主要な4つの柱:**

1. **Transport Scalability** — Streamable HTTP の水平スケーリング
   - ステートフルセッションとロードバランサーの衝突を解消
   - `.well-known` メタデータで接続なしにサーバー能力を発見

2. **Agent Communication** — サーバーサイドエージェントループの拡張
   - Sampling（サーバーがクライアント経由でLLM呼び出し）の高度化
   - 並列ツール呼び出しの正式サポート

3. **Elicitation** — サーバーが実行を一時停止してユーザー入力を要求
   - URL-mode: OAuth/決済など外部URLに誘導して再開
   - 2026 Q1 に SEP（仕様拡張提案）を確定、6月に仕様リリース予定

4. **Governance & Enterprise** — 認証・認可の標準化

### 4.2 Streamable HTTP Transport

stdio の代替として、MCP サーバーをリモートサービスとして実行可能にする。

**現状の課題:**
- ステートフルセッションとロードバランサーの衝突
- 水平スケーリングにワークアラウンドが必要
- レジストリ/クローラーが接続なしにサーバー情報を取得できない

**helix-agent への影響:** 現時点では stdio で十分。将来的にリモートデプロイ（複数ユーザー共有）する場合に必要。

### 4.3 「MCP + ローカルLLM + エージェント」成功プロジェクト

| プロジェクト | 概要 | Stars/評価 |
|------------|------|-----------|
| [MCP Client for Ollama](https://github.com/jonigl/mcp-client-for-ollama) | TUI クライアント、agent mode、human-in-the-loop | 注目度高 |
| [Ollama MCP Bridge](https://github.com/jonigl/ollama-mcp-bridge) | Ollama API を MCP ツールで拡張 | 実用的 |
| [MCP-Ollama Server](https://github.com/Sethuram2003/MCP-ollama_server) | ファイル/Web/Email/GitHub操作をローカルLLMで | プライバシー重視 |
| [Ollama MCP Agent](https://github.com/godstale/ollama-mcp-agent) | PC上で無料で使えるエージェント | 初心者向け |

### 4.4 mcpservers.org / PulseMCP のトレンド

- [PulseMCP で "ollama" 検索](https://www.pulsemcp.com/servers?q=ollama) → 53件のOllama関連MCPサーバー
- ナレッジベース + Ollama embeddings + Qdrant の組み合わせが人気
- GitHub リポジトリのインデックス化 + セマンティック検索が主流ユースケース

### Sources
- [2026 MCP Roadmap](http://blog.modelcontextprotocol.io/posts/2026-mcp-roadmap/)
- [MCP 公式 Roadmap](https://modelcontextprotocol.io/development/roadmap)
- [MCP Transport Future](https://blog.modelcontextprotocol.io/posts/2025-12-19-mcp-transport-future/)
- [MCP vs A2A 比較](https://dev.to/pockit_tools/mcp-vs-a2a-the-complete-guide-to-ai-agent-protocols-in-2026-30li)
- [MCP Ecosystem v1.27 分析](https://www.contextstudios.ai/blog/mcp-ecosystem-in-2026-what-the-v127-release-actually-tells-us)
- [PulseMCP Ollama Servers](https://www.pulsemcp.com/servers?q=ollama)

---

## 5. セキュリティ・サンドボックス

### 5.1 MCP の Sampling / Human-in-the-Loop

**Sampling:** サーバーがクライアント経由で LLM 呼び出しを要求する仕組み。
- エージェントループの中核: サーバー側でマルチステップ推論を実行可能
- Claude Code ではまだ sampling の完全サポートは限定的

**Human-in-the-Loop:**
- MCP 仕様は write_file / execute_command で手動承認を推奨
- **helix-agent では**: エージェントが実行するアクションをClaude Code（ホスト側）が検証する構造が自然

### 5.2 ファイルアクセスの制限

**MCP Roots メカニズム:**
```json
{
  "roots": [
    {"uri": "file:///path/to/allowed/dir"}
  ]
}
```

- Roots はクライアントがサーバーに「この範囲内で操作してください」と伝える仕組み
- **重要:** Roots は意図の伝達であり、セキュリティ制約ではない
- 実際のセキュリティは OS レベル（ファイルパーミッション、サンドボックス）で担保する必要あり

**推奨実装:**
```python
ALLOWED_ROOTS = [Path("C:/Development"), Path("C:/Users/tomot/Documents")]

def validate_path(path: str) -> Path:
    resolved = Path(path).resolve()
    if not any(str(resolved).startswith(str(root)) for root in ALLOWED_ROOTS):
        raise PermissionError(f"Access denied: {path}")
    # 機密ファイルパターンのブロック
    BLOCKED = [".env", "credentials", ".ssh", ".gnupg"]
    if any(b in resolved.name.lower() for b in BLOCKED):
        raise PermissionError(f"Sensitive file blocked: {path}")
    return resolved
```

### 5.3 コマンド実行のサンドボックス化

**選択肢:**

| 方法 | セキュリティ | 速度 | 複雑度 |
|------|------------|------|--------|
| subprocess + allowlist | 低 | 最速 | 低 |
| Docker コンテナ | 高 | 中 | 中 |
| Windows Sandbox | 最高 | 遅 | 高 |
| helix-sandbox 連携 | 最高 | 中 | 低（既存） |

### 5.4 helix-sandbox との連携

helix-sandbox（Windows Sandbox MCP）は既に以下をサポート:
- サンドボックスライフサイクル管理（起動・停止）
- サンドボックス内でのコマンド実行
- 共有フォルダによるファイル共有
- ネットワーク情報取得

**連携パターン:**
```
helix-agent (エージェント)
  │
  ├── 安全な操作: 直接実行（ファイル読み取り、Ollama 呼び出し）
  │
  └── 危険な操作: helix-sandbox 経由
      ├── コマンド実行 → sandbox 内で実行
      ├── ファイル書き込み → sandbox 内で実行後、結果のみ取得
      └── ネットワークアクセス → sandbox 内で隔離実行
```

**MCP サーバー間連携:** FastMCP の `Client` を使って helix-agent → helix-sandbox を呼び出し可能:

```python
from fastmcp import Client

async def execute_in_sandbox(command: str) -> str:
    async with Client("helix-sandbox") as sandbox:
        result = await sandbox.call_tool("execute_command", {
            "command": command,
            "timeout": 30
        })
        return result
```

### Sources
- [MCP Client Concepts](https://modelcontextprotocol.io/docs/learn/client-concepts)
- [MCP セキュリティガイド](https://christian-schneider.net/blog/securing-mcp-defense-first-architecture/)
- [MCP サンドボックス化方法](https://mcpmanager.ai/blog/sandbox-mcp-servers/)
- [Windows Sandbox MCP](https://github.com/yourtablecloth/WindowsSandboxMcp)
- [Code Sandbox MCP](https://github.com/Automata-Labs-team/code-sandbox-mcp)
- [Claude Code Sandboxing](https://code.claude.com/docs/en/sandboxing)

---

## 6. 推奨アーキテクチャ

### 6.1 全体設計図

```
┌─────────────────────────────────────────────────────────────┐
│                    Claude Code (Host/Client)                  │
│  「エージェントに任せる」感覚で helix-agent を呼び出す        │
└──────────────┬──────────────────────────────────────────────┘
               │ MCP (stdio)
               ▼
┌─────────────────────────────────────────────────────────────┐
│                    helix-agent MCP Server                     │
│                                                               │
│  ┌──────────────────────────────────────────────────────┐    │
│  │              Agent Loop (ReAct)                        │    │
│  │                                                        │    │
│  │  1. タスク受信                                         │    │
│  │  2. while step < max_steps:                           │    │
│  │     a. ctx.report_progress(step, max_steps)           │    │
│  │     b. LLM に送信 (format: JSON schema)               │    │
│  │     c. JSON パース: {thought, action, action_input}    │    │
│  │     d. action == "finish" → 結果返却                   │    │
│  │     e. ツール実行 → 結果を履歴に追加                   │    │
│  │  3. max_steps 到達 → 途中結果を返却                    │    │
│  └──────────────────────────────────────────────────────┘    │
│                                                               │
│  ┌────────────┐  ┌────────────┐  ┌────────────────────┐     │
│  │ ToolRegistry│  │ ModelRouter │  │ SessionMemory      │     │
│  │ - search    │  │ - auto     │  │ - ctx.set_state()  │     │
│  │ - read_file │  │ - benchmark│  │ - 会話履歴保持     │     │
│  │ - write_file│  │ - fallback │  │                    │     │
│  │ - calculate │  │            │  │                    │     │
│  │ - qdrant    │  │            │  │                    │     │
│  └──────┬─────┘  └──────┬─────┘  └────────────────────┘     │
│         │               │                                     │
│         ▼               ▼                                     │
│  ┌────────────┐  ┌────────────┐  ┌────────────────────┐     │
│  │ PathGuard   │  │ OllamaAPI  │  │ helix-sandbox      │     │
│  │ (allowlist) │  │ (httpx)    │  │ (MCP client)       │     │
│  └────────────┘  └────────────┘  └────────────────────┘     │
└─────────────────────────────────────────────────────────────┘
         │                │                    │
         ▼                ▼                    ▼
    File System     Ollama (11434)      Windows Sandbox
                    Qdrant (6333)
```

### 6.2 エージェントツール設計

MCPツール（Claude Code から呼ばれる）:

```
think(task, context, model, mode)        # 既存: 単発推論（変更なし）
see(image_path, question, model)         # 既存: Vision（変更なし）
agent(task, tools, max_steps, mode)      # 新規: エージェントループ
models(action, model_name)               # 既存: モデル管理
config(action, key, value)               # 既存: 設定管理
```

エージェント内部ツール（LLMが JSON で呼ぶ）:

```
search_memory(query)          # Qdrant 検索
store_memory(content, meta)   # Qdrant 保存
read_file(path)               # ファイル読み取り（PathGuard付き）
write_file(path, content)     # ファイル書き込み（PathGuard付き）
run_command(command)           # コマンド実行（sandbox or allowlist）
calculate(expression)          # 計算（eval sandboxed）
web_search(query)              # Web検索（将来）
```

---

## 7. 実装の難易度・リスク評価

| コンポーネント | 難易度 | リスク | 備考 |
|--------------|--------|--------|------|
| ReAct ループ本体 | **低** | 低 | smolagents 参考で ~200行 |
| JSON structured output | **低** | 低 | Ollama format パラメータで強制可能 |
| 内部ツール定義 | **低** | 低 | Python 関数 + レジストリ |
| ctx.report_progress | **低** | 中 | FastMCP の5秒問題に注意 |
| ファイルアクセス + PathGuard | **中** | 中 | セキュリティ設計が重要 |
| Qdrant 統合 | **中** | 低 | 既存インフラ流用 |
| helix-sandbox 連携 | **中** | 中 | MCP client-to-server 呼び出し |
| ネイティブ tool calling | **高** | **高** | Qwen3.5 バグ、モデル依存 |
| マルチステップ会話管理 | **中** | 中 | トークン上限・コンテキスト管理 |
| コマンド実行サンドボックス | **高** | **高** | セキュリティ検証が必要 |

---

## 8. 具体的な実装ステップ

### Phase 1: ReAct エージェントループ MVP (推定工数: 4-6時間)

**ファイル変更:**

1. `src/tools.py` (新規 ~100行)
   - `ToolRegistry` クラス: 内部ツールの登録・実行
   - `Tool` dataclass: name, description, parameters, handler
   - `format_tools_for_prompt()`: LLM に渡すツール説明テキスト生成

2. `src/react_loop.py` (新規 ~200行)
   - `ReactLoop` クラス: エージェントループ本体
   - `run(task, tools, max_steps=10)`: メインループ
   - `_parse_action(response)`: JSON パース + バリデーション
   - `_execute_action(action, tools)`: ツール実行
   - `_build_messages(history)`: メッセージ履歴構築

3. `src/agent.py` (修正)
   - `HelixAgent.agent()` メソッド追加
   - ReactLoop + ToolRegistry の統合

4. `server.py` (修正)
   - `@mcp.tool() async def agent(...)` 追加
   - Context 注入 (`ctx: Context`) で progress 報告

5. `tests/test_react_loop.py` (新規 ~150行)
   - モックLLM でのループテスト
   - max_steps 到達テスト
   - JSON パースエラーのリカバリテスト

### Phase 2: 内部ツール実装 (推定工数: 3-4時間)

6. `src/builtin_tools.py` (新規 ~200行)
   - `search_memory()`: Qdrant セマンティック検索
   - `store_memory()`: Qdrant 保存
   - `read_file()`: PathGuard 付きファイル読み取り
   - `write_file()`: PathGuard 付きファイル書き込み
   - `calculate()`: 安全な数式評価

7. `src/pathguard.py` (新規 ~60行)
   - allowlist ディレクトリ管理
   - 機密ファイルパターンブロック
   - パストラバーサル防止

### Phase 3: セッション管理・品質向上 (推定工数: 2-3時間)

8. `src/react_loop.py` (修正)
   - `ctx.set_state()` でエージェント会話履歴をセッション保持
   - `ctx.report_progress()` で各ステップの進捗通知
   - エラーリカバリ: JSON パース失敗時のリプロンプト (最大2回)

9. `src/agent.py` (修正)
   - モード別動作: `mode="agent"` 追加
   - 結果サマリー: 長い出力の自動要約

### Phase 4: helix-sandbox 連携 (推定工数: 3-4時間, オプション)

10. `src/sandbox_client.py` (新規 ~80行)
    - FastMCP Client で helix-sandbox に接続
    - `run_in_sandbox(command)`: サンドボックス内コマンド実行
    - `run_command` 内部ツールと統合

---

## 9. 最重要な判断ポイント

### ネイティブ tool calling vs プロンプトベース ReAct

**結論: プロンプトベース ReAct + JSON structured output を推奨**

理由:
1. Qwen3.5 の Ollama ネイティブ tool calling に重大なバグがある
2. JSON structured output (`format` パラメータ) は安定して動作する
3. モデル非依存: どのモデルでも同じプロンプトで動く
4. デバッグが容易: thought/action が可視化される
5. smolagents の実績: ネイティブ tool calling より 30% 少ないステップ

### Claude Code との役割分担

```
Claude Code (ホスト):
  - タスクの分解・判断
  - helix-agent の結果の品質検証
  - ユーザーとの対話
  - セキュリティの最終承認

helix-agent (エージェント):
  - ローカルLLM による推論・分析
  - ファイル操作の実行
  - Qdrant 検索・保存
  - 反復的な作業の自動化
```

この構造により、Claude Code のトークン消費を最小化しつつ、ローカルLLM のパワーを最大限に活用できる。
