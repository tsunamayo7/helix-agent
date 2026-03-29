# helix-agent

**Claude Code をローカル Ollama モデルで拡張する MCP サーバー — インテリジェント自動ルーティング、設定不要**

[English](README.md) | **日本語**

---

helix-agent は、**Claude Code のタスクをローカルの Ollama モデルに委譲する** MCP サーバーです。推論、コードレビュー、画像分析などを、インストール済みの Ollama モデルから最適なものを自動選択して実行します。

**API キー不要。クラウド不要。設定ファイル不要。すぐ動きます。**

## なぜ helix-agent？

| 課題 | helix-agent の解決策 |
|------|---------------------|
| Claude Code は全てに API トークンを消費する | ルーティンタスクを無料のローカルモデルに委譲 |
| PAL MCP はコンテキストの 50% を消費 | **コンテキスト消費 5% 以下** |
| 既存の Ollama MCP は手動モデル選択が必要 | **自動ルーティング** — インストール済みモデルを検出し最適を選択 |
| ローカルモデルの品質保証がない | **品質優先** — Claude が出力を検証・補完 |
| 複数の設定ファイルが必要で複雑 | **設定ゼロ** — `uv run` で即起動 |

## クイックスタート

```bash
# 1. Ollama でモデルを用意
ollama pull gemma3

# 2. クローンとインストール
git clone https://github.com/tsunamayo7/helix-agent.git
cd helix-agent && uv sync
```

`~/.claude/settings.json` に追加:

```json
{
  "mcpServers": {
    "helix-agent": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/helix-agent", "python", "server.py"]
    }
  }
}
```

これだけで、Claude Code からローカル Ollama モデルが使えるようになります。

## ツール

### `think` — 推論・分析・コード生成

テキストタスクをローカル LLM に委譲。最適なモデルを自動選択します。

```
Claude Code: 「helix-agent でこの 500 行のログを要約して」
→ helix-agent が qwen3.5:122b（推論モデル）にルーティング
→ 要約を返却
→ Claude が検証・強化
```

**モード:**
- `quality` — 大型モデル、低 temperature、徹底的（デフォルト）
- `fast` — 小型モデル、簡潔な出力
- `creative` — 高 temperature、探索的

### `agent_task` — 自律型 ReAct エージェント

ローカル LLM がツールを使いながら自律的にタスクを解決します。

```
Claude Code: 「helix-agent の agent で pyproject.toml を読んでプロジェクトを要約して」
→ Step 1: LLM「ファイルを読む必要がある」→ read_file 実行
→ Step 2: LLM が内容を分析 → 要約を返却
→ 推論トレース付きの構造化結果を返す
```

**内蔵ツール:**
- `read_file` / `write_file` / `list_files` / `search_in_file` — ファイル操作（PathGuard 保護）
- `run_command` — シェル実行（許可リスト: git, python, uv, ollama）
- `calculate` — 安全な数式評価
- `search_memory` — Qdrant セマンティック検索

**セキュリティ:** PathGuard がディレクトリ許可リスト、機密ファイルブロック（.env, credentials, SSH鍵）、パストラバーサル攻撃を防止。

### `see` — Vision & OCR

ローカル Vision モデルで画像を分析します。

```
Claude Code: 「helix-agent でこのスクリーンショットを OCR して」
→ mistral-small3.2（Vision モデル）にルーティング
→ 画像内のテキストを抽出
```

### `models` — モデル情報・ベンチマーク・オーバーライド

モデルの確認、ハードウェア上でのベンチマーク、特定モデルへの固定ができます。

```
> models(action="capabilities")
{
  "vision": ["mistral-small3.2:latest", "gemma3:27b"],
  "code": ["qwen-coder:7b"],
  "reasoning": ["qwen3.5:122b", "nemotron-cascade-2:latest"],
  "embedding": ["qwen3-embedding:8b"]
}
```

**ベンチマーク** — ユーザーの実機でモデルを評価:

```
> models(action="benchmark")                         # 未評価モデルを一括テスト
> models(action="benchmark", model_name="gemma3:4b")  # 特定モデルをテスト
> models(action="benchmark_status")                   # ランキング確認
```

テスト項目: コード生成（FizzBuzz、文字列操作）、推論（論理、数学）、指示追従（JSON出力、リスト形式）、日本語（翻訳、要約）、速度（tokens/sec）。結果は `~/.helix-agent/benchmarks.json` にキャッシュされ、ルーティング優先度に自動反映されます。

**モデルオーバーライド** — 特定モデルに固定:

```
> models(action="use", model_name="qwen3.5:122b")    # 全タスクをこのモデルに固定
> models(action="use_auto")                            # 自動選択に戻す
```

### `config` — 設定管理

再起動なしで設定を変更できます。

## 自動ルーティングの仕組み

```
タスク: 「この Python 関数のバグを探して」
  ↓
キーワード検出: "関数", "バグ" → CODE 能力
  ↓
モデル絞り込み: CODE 能力を持つモデルを抽出
  ↓
優先度ソート: qwen-coder > deepseek-coder > 汎用
  ↓
選択: qwen-coder:7b
```

ルーターのロジック:
1. **ローカルベンチマークスコア** — ユーザーの実機での性能データ（v0.3.0）
2. **名前パターンマッチ** — モデル名から能力を推定
3. **サイズ優先** — quality モードでは大型モデルを優先
4. **既知モデルブースト** — 実績あるモデルに加点

## 品質優先設計

helix-agent は **ドラフト生成器** として設計されています。Claude の代替ではありません:

```
ユーザー → Claude Code → helix-agent.think() → ローカル LLM（ドラフト）
                                                      ↓
                                                Claude が検証・強化
                                                      ↓
                                                高品質な最終回答
```

- ローカル LLM が重い処理を担当（トークン消費ゼロ）
- Claude が優れた推論力で補完（最小限のトークン）
- ユーザーは常に Claude 品質の回答を受け取る

## 競合との比較

| 特徴 | helix-agent | PAL MCP | OllamaClaude | ollama-mcp |
|------|:-----------:|:-------:|:------------:|:----------:|
| Claude Code 最適化 | **Yes** | 一部 | Yes | No |
| 設定ゼロ | **Yes** | No | 一部 | 一部 |
| コンテキスト消費 | **<5%** | ~50% | ~2% | ~10% |
| モデル自動選択 | **Yes** | Yes | フォールバックのみ | No |
| Vision 対応 | **Yes** | モデル依存 | No | No |
| 品質モード | **3 モード** | No | No | No |
| Ollama 特化 | **Yes** | No (全プロバイダー) | Yes | Yes |

## 対応モデル

Ollama の全モデルで動作します。自動ルーティングは以下に最適化:

| 能力 | 推奨モデル |
|------|-----------|
| 推論 | qwen3.5, nemotron-cascade-2, llama3.3, command-a |
| コード | qwen-coder, codestral, devstral, deepseek-coder |
| Vision | mistral-small3.2, gemma3, moondream |
| 埋め込み | qwen3-embedding, nomic-embed-text, bge |

### v0.2.0: メタデータ活用ルーティング

`ollama show` のメタデータを活用した精度向上:
- **コンテキスト長**認識（例: nemotron-cascade-2 は 262K）
- **パラメータ数**抽出による品質推定
- **スマート fast モード** — 50GB+モデルを回避、10GB未満を優先
- `models(action="detailed")` で詳細メタデータを確認可能

### v0.3.0: ローカルベンチマーク＋モデルオーバーライド

ユーザーの実機でベンチマークを実行し、ルーティングを最適化:
- **8 種の自動テスト** — コード、推論、指示追従、日本語、速度
- **自動採点** — 正規表現＋パターンマッチバリデータ
- **永続キャッシュ** — `~/.helix-agent/benchmarks.json` に保存
- **新モデル自動検出** — 未ベンチマークモデルを自動特定
- **モデルオーバーライド** — ユーザー指定のモデルにルーティングを固定
- **ベンチマーク連動ルーティング** — スコアがモデル選択優先度に直接影響

## 開発

```bash
# テスト実行（144テスト）
uv run pytest tests/ -v

# 構文チェック
uv run python -m py_compile server.py
```

## ロードマップ

- [x] v0.1.0 — コアツール (think, see, models, config) + 名前ベース自動ルーティング
- [x] v0.2.0 — メタデータ活用ルーティング（コンテキスト長、パラメータ数、スマート fast モード）
- [x] v0.3.0 — ローカルベンチマーク、モデルオーバーライド、ベンチマーク連動ルーティング
- [x] v0.4.0 — ReAct エージェントループ、ファイル操作ツール + PathGuard、progress 通知
- [ ] v0.5.0 — Qdrant メモリ統合、helix-sandbox コマンド実行
- [ ] v1.0.0 — 正式公開、mcpservers.org 登録

## 関連プロジェクト

- [helix-pilot](https://github.com/tsunamayo7/helix-pilot) — GUI 自動操作 MCP サーバー（Vision LLM で Windows デスクトップを制御）
- [helix-sandbox](https://github.com/tsunamayo7/helix-sandbox) — Windows Sandbox MCP サーバー

## ライセンス

MIT
