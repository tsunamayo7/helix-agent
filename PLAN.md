# helix-agent 設計プラン

## 1. 競合分析サマリー

| プロジェクト | Stars | 強み | 弱み |
|------------|-------|------|------|
| **PAL MCP** | ~11,300 | マルチプロバイダー自動選択 | **Context 50%消費**、設定複雑 |
| **OllamaClaude** | 9 | トークン98.75%削減 | **応答20-180秒**、コード特化のみ |
| **Dxeo/ollama-mcp** | 小 | シンプル（3ツール） | **静的モデル選択**、ルーティングなし |
| **rawveg/ollama-mcp** | 中 | 安定、テスト96%、14ツール | **自動ルーティングなし**、手動指定 |
| **angrysky56** | 小 | router/chain/parallel対応 | **WIP、不安定** |
| **ollama-mcp-bridge** | 3 | Ollama APIプロキシ | Stars少、Dockerのみ |
| **ollama-docker-mcp** | 小 | - | **Phase 1未完了** |

## 2. 空白地帯（どの競合もカバーしていない）

1. **Claude Code Agent ワークフロー最適化** — Claude Code の Agent ツールと同じ感覚でOllamaを使える設計は皆無
2. **品質保証付き委譲** — 「ローカルLLMで十分な品質が出るタスクのみ委譲」する知的判断
3. **Vision + Text + Embedding の統一ルーティング** — 1つのMCPで全モダリティをカバー
4. **低コンテキストオーバーヘッド** — PALは50%消費。ツール定義を最小化して5%以下を目指す
5. **共有記憶（Qdrant）統合** — セッション間のコンテキスト保持
6. **インストール済みモデル自動検出 + 能力マッピング** — 設定不要で動く

## 3. helix-agent のコンセプト

### ミッション
**Claude Code の知能を損なわず、ローカル Ollama モデルで拡張する MCP サーバー**

### 設計原則

1. **Quality-First Delegation（品質優先委譲）**
   - Claudeの判断力を信頼し、Claudeが「このタスクはローカルLLMに任せたい」と思った時だけ使う
   - ローカルLLMの出力にはClaudeが品質チェックする前提で設計
   - 「精度を落とすトークン節約」ではなく「Claudeの手を増やす」

2. **Zero-Config Start（設定ゼロ起動）**
   - `ollama list` で自動検出、モデル能力を推定
   - YAML設定はオプショナル（カスタマイズしたい人向け）
   - `uv run helix-agent` で即動作

3. **Minimal Context Footprint（最小コンテキスト消費）**
   - ツール定義は簡潔に（PALの50%消費は論外）
   - 結果は要約モードをデフォルトに（長い出力をそのまま返さない）
   - 目標: コンテキスト消費5%以下

4. **Claude Code Native（Claude Codeネイティブ）**
   - Claude Code の Agent ツールのパターンに合わせた設計
   - Claudeが「エージェントに任せる」ように「Ollamaに任せる」

## 4. ツール設計

### コアツール（5個 — 少なく、強く）

```
┌─────────────────────────────────────────────────┐
│                 helix-agent                       │
│                                                   │
│  ┌──────────┐  ┌──────────┐  ┌──────────────┐   │
│  │  think   │  │  see     │  │  remember    │   │
│  │(推論/分析)│  │(Vision)  │  │(記憶/検索)    │   │
│  └──────────┘  └──────────┘  └──────────────┘   │
│  ┌──────────┐  ┌──────────┐                      │
│  │  models  │  │  config  │                      │
│  │(モデル管理)│  │(設定)    │                      │
│  └──────────┘  └──────────┘                      │
│                    ↓                              │
│            Ollama API (localhost:11434)            │
│            Qdrant API (localhost:6333)             │
└─────────────────────────────────────────────────┘
```

#### 1. `think` — 推論・分析・コード生成

```python
think(
    task: str,           # 「このログを要約して」「バグの原因を推測して」
    context: str = "",   # 追加コンテキスト（コード、ログ等）
    model: str = "auto", # auto = 最適モデル自動選択
    mode: str = "quality" # "quality" | "fast" | "creative"
)
```

- **auto モデル選択ロジック**:
  - コード関連 → coder系モデル優先 (qwen-coder, codestral等)
  - 推論・分析 → 大型モデル優先 (qwen3.5:122b, nemotron等)
  - 軽量タスク → 小型モデル (gemma3:4b等)
  - モデル能力はOllama API の modelfile情報 + 名前パターンマッチで推定

- **mode による挙動**:
  - `quality`: temperature低め、大型モデル優先、リトライあり
  - `fast`: 小型モデル、短い出力
  - `creative`: temperature高め、多様な出力

#### 2. `see` — Vision（画像分析）

```python
see(
    image_path: str,     # 画像ファイルパス or base64
    question: str = "",  # 「この画面で何が起きている？」
    model: str = "auto"  # auto = Vision対応モデルを自動選択
)
```

- Vision対応モデルを自動検出（mistral-small3.2, gemma3, moondream等）
- helix-pilotと異なり、**スクリーンショットは自分で撮らない**（画像を渡すだけ）
- OCR強化: 「画像内のテキストをすべて抽出して」に対応

#### 3. `remember` — 共有記憶（Qdrant統合）

```python
remember(
    action: str,    # "search" | "store" | "list"
    query: str = "",
    content: str = "",
    metadata: dict = {}
)
```

- Qdrant `mem0_shared` コレクションに直接アクセス
- 埋め込みモデル: qwen3-embedding:8b（既存インフラ活用）
- Claudeが「これ覚えておいて」→ helix-agentがQdrantに保存
- Claudeが「前回の決定は？」→ helix-agentがセマンティック検索

#### 4. `models` — モデル管理

```python
models(
    action: str = "list"  # "list" | "status" | "capabilities"
)
```

- `list`: インストール済みモデル一覧 + 推定能力
- `status`: Ollama稼働状態 + GPU使用率
- `capabilities`: 各モデルの得意分野マップ

#### 5. `config` — 設定管理

```python
config(
    action: str = "show"  # "show" | "set"
    key: str = "",
    value: str = ""
)
```

- モデル優先順位のカスタマイズ
- デフォルト temperature 等の設定

## 5. 自動モデル選択（Auto-Routing）の実装戦略

### Phase 1: 名前ベース推定（v0.1）
```python
MODEL_CAPABILITY_PATTERNS = {
    "code": ["coder", "codestral", "deepseek-coder", "starcoder"],
    "vision": ["mistral-small3.2", "gemma3", "moondream", "llava"],
    "reasoning": ["qwen3", "nemotron", "llama", "mistral"],
    "embedding": ["embedding", "nomic-embed", "bge"],
    "creative": ["gemma", "llama"],
}
```

### Phase 2: モデルメタデータ活用（v0.2）
- `ollama show <model>` の出力からパラメータ数、量子化、コンテキスト長を取得
- VRAM使用量を推定し、同時実行可能なモデル数を計算

### Phase 3: ベンチマーク自動実行（v0.3）
- 初回起動時に簡易ベンチマーク（推論速度、コード生成精度）
- 結果をキャッシュして次回以降の選択に活用

## 6. 品質保証メカニズム

### Claudeとの連携パターン

```
ユーザー: 「このログファイルを分析して」

Claude（オーケストレーター）:
  1. helix-agent.think(task="ログ分析", context=<log>, mode="quality")
  2. ローカルLLMの結果を受け取る
  3. Claude自身が結果を検証・補完
  4. ユーザーに最終回答を返す
```

**重要**: helix-agentの出力は「ドラフト」であり、Claudeが最終品質を保証する。
これにより:
- ローカルLLMの精度不足をClaudeが補完
- Claudeのトークン消費は「検証」分だけ（全推論より大幅に少ない）
- ユーザーには常にClaude品質の回答が届く

### 品質モード設定

```yaml
# config.yaml（オプション）
quality:
  mode: "enhance"  # "enhance" | "delegate" | "verify"
  # enhance: Claudeが結果を強化（デフォルト、品質最優先）
  # delegate: ローカルLLMの結果をそのまま返す（トークン最節約）
  # verify: Claudeが正誤のみチェック（中間）
```

## 7. 技術スタック

| コンポーネント | 技術 |
|-------------|------|
| MCP フレームワーク | FastMCP (Python) |
| HTTP クライアント | httpx (async) |
| Ollama API | REST API (localhost:11434) |
| Qdrant | REST API (localhost:6333) |
| 埋め込み | qwen3-embedding:8b (Ollama) |
| パッケージ管理 | uv |
| テスト | pytest + pytest-asyncio |

### 依存関係（最小）

```toml
[project]
dependencies = [
    "fastmcp>=2.0",
    "httpx>=0.27",
]

[project.optional-dependencies]
memory = ["qdrant-client>=1.12"]
```

## 8. ディレクトリ構造

```
C:\Development\tools\helix-agent\
├── server.py              # MCPサーバーエントリポイント
├── pyproject.toml
├── config.yaml            # オプション設定
├── src/
│   ├── __init__.py
│   ├── agent.py           # HelixAgent メインクラス
│   ├── router.py          # 自動モデル選択ロジック
│   ├── ollama_client.py   # Ollama API クライアント
│   ├── memory.py          # Qdrant 共有記憶
│   └── models.py          # モデル能力マッピング
├── tests/
│   ├── test_agent.py
│   ├── test_router.py
│   └── test_ollama_client.py
├── docs/
│   └── articles/          # 宣伝記事
└── README.md
```

## 9. 競合との差別化マトリクス

| 特徴 | helix-agent | PAL MCP | OllamaClaude | rawveg/ollama-mcp |
|------|:-----------:|:-------:|:------------:|:-----------------:|
| Claude Code最適化 | **★★★★★** | ★★★ | ★★★★ | ★★ |
| 設定ゼロ起動 | **★★★★★** | ★ | ★★★ | ★★★ |
| コンテキスト消費 | **<5%** | ~50% | ~2% | ~10% |
| 品質保証 | **enhance/verify** | なし | なし | なし |
| Vision対応 | **Yes** | モデル依存 | No | No |
| 共有記憶 | **Qdrant** | No | No | No |
| モデル自動選択 | **能力推定** | プロバイダー選択 | 固定+fallback | 手動指定 |
| OS非依存 | **Yes** | Yes | Yes | Yes |
| Ollama特化 | **Yes** | No (全プロバイダー) | Yes | Yes |

## 10. 開発ロードマップ

### v0.1.0 — MVP ✅ 完了（2026-03-29）
- [x] `think` ツール（テキスト推論、auto-routing基本版）
- [x] `see` ツール（Vision分析、OCR）
- [x] `models` ツール（一覧、能力推定）
- [x] `config` ツール（基本設定）
- [x] Ollama API クライアント
- [x] 名前ベース自動モデル選択
- [x] Claude Code settings.json 登録
- [x] README.md（英語）+ README_ja.md（日本語）
- [x] テスト 49個パス
- [x] GitHub公開: https://github.com/tsunamayo7/helix-agent

### v0.2.0 — Memory + メタデータルーティング
- [ ] `remember` ツール（Qdrant mem0_shared統合）
- [ ] モデルメタデータ活用ルーティング（ollama show → パラメータ数、量子化、コンテキスト長）
- [ ] config.yaml によるカスタマイズ
- [ ] VRAM使用量推定 → 同時実行可能モデル数計算

### v0.3.0 — OTel実測ベースルーティング
- [ ] OpenTelemetryスパン追加（think/see呼び出しにトレース埋め込み）
- [ ] 初回起動時ベンチマーク自動実行（全モデル × 基本タスク）
- [ ] 実績データのローカルDB保存（SQLite）
- [ ] 実測ベースモデル選択（推測→実測への進化）
  - モデル × タスク種別 × 精度 × 応答速度の実績マトリクス
  - quality=最高精度モデル、fast=実測最速モデル
  - 使うほどルーティング精度が向上
- [ ] 並列推論（複数モデルで同時推論→最良選択）
- [ ] ストリーミング応答対応
- [ ] 品質モード（enhance/delegate/verify）
- [ ] otel-agent-trace互換のエクスポート（可視化ダッシュボード連携）

参考:
- OTelマルチセッション分析: https://zenn.dev/seeda_yuto/articles/otel-multi-session-analysis
- OTelツール: https://github.com/mii012345/otel-agent-trace

### v0.10.0 — Token Drain Crisis Response ✅ 完了（2026-04-05）

2026-03-31 の Anthropic 公式謝罪「users hitting usage limits way faster than expected」を受けて、
ユーザー調査で特定された Computer Use の痛点を直撃する機能を追加。

- [x] `vision_compress` — スクショ→構造化JSON要約（15K→400 tokens、~94%削減）
- [x] `dom_compress` — HTML→構造化JSON要約（114K→500 tokens、~99%削減）
- [x] `retry_guard_check` / `_status` / `_reset` — 同一ツール呼び出し反復ループ検知
- [x] README を Token Drain Crisis 文脈で再ポジショニング
- [x] テスト 28個追加（token_saver.py 全機能カバー）

### v1.0.0 — 正式公開（Token Drain Crisis 特化ローンチ）

**ポジショニング**: "Stop the Token Drain. Survive Claude Code's Computer Use costs."

#### 技術ロードマップ
- [ ] ベンチマーク記事（英日）: Before/After 数値で証明
- [ ] デモGIF作成: "Gmail 50件処理 token 比較"
- [ ] PathGuard 強化（CVE-2025-59536 文脈で差別化）
- [ ] Prompt Injection Scanner（取得コンテンツの事前サニタイズ）
- [ ] Token Budget Dashboard（tracing.py 拡張、実時間節約額表示）
- [ ] `git_ops` tool（PR自動作成エージェント）
- [ ] CI/CD（GitHub Actions）

#### ローンチ戦略
- [ ] Reddit r/ClaudeAI の Token drain crisis スレッドに reply
- [ ] Reddit r/LocalLLaMA へ投稿
- [ ] HN "Show HN: Cut Claude Code Computer Use costs by 94% with local vision models"
- [ ] Zenn/Qiita（日本語）「Claude Code トークン節約術」
- [ ] awesome-claude-code に PR 登録
- [ ] mcpservers.org 登録

#### マーケティング訴求
- "Stop the Token Drain" をキャッチコピー主軸に
- Anthropic 公式謝罪を引用して文脈共有
- 15,000 tokens per screenshot を定量説明
- BYOM できない Claude Code に外付け BYOM を

### v1.1.0 — ComputerUse特化モデル学習（将来）

- [ ] v4学習データセット構築（Computer Use 特化ペア 1,000件）
- [ ] git操作対話ペア 500件
- [ ] ReAct思考トレース 500件
- [ ] gemma4-agent-coder-v4 リリース
