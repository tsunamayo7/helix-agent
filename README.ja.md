# helix-agent（日本語ドキュメント）

**Claude Code のトークン消費を 82〜97% 自動削減。** リトライループ検知 + スクショ/DOM のローカルLLM圧縮 + GPU自動検出によるモデル最適選択 — すべて1つの MCP サーバーで。

English README: **[README.md](README.md)**

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-347%20passing-brightgreen.svg)](#)
[![v0.15.1](https://img.shields.io/badge/version-0.15.1-7c3aed.svg)](#)
[![8GB VRAM OK](https://img.shields.io/badge/GPU-8GB%20VRAM%20OK-green.svg)](#gpu-自動検出とモデルティア)

## トークン削減 — 実測データ

> *「Maxプランの5時間クォータが19分で消えた」* — [Claude Code ユーザー](https://github.com/anthropics/claude-code/issues/16157)（👍 666+）

| 機能 | 削減前 | 削減後 | 削減率 |
|---|---|---|---|
| **スクショ解析** (vision_compress) | ~15,000 トークン | ~400 トークン | **97%** |
| **DOM/HTML処理** (dom_compress) | ~114,000 トークン | ~500 トークン | **99%** |
| **ブラウザ操作** (agent-browser) | ~15,000/アクション | ~1,000〜2,700 | **82〜93%** |
| **リトライループ** (retry_guard) | ∞（クォータ消滅まで） | 3回目で停止 | **100%** |
| **ルーチンタスク** (think/agent_task) | Opusトークン（$$$） | ローカルLLM（$0） | **100%** |

すべての圧縮は **ローカルGPU上のOllama** で実行 — クラウドAPIコスト ゼロ。

### 数字で見る問題の深刻さ

Claude Code の一般的なセッションでは、見えないところでトークンが大量消費されています（[926セッション分析](https://x.com/Nossa_ym/status/2041127311735402802) より）:

| トークンの行き先 | 1ターンあたり | 割合 |
|---|---|---|
| システムプロンプト + MCPツールスキーマ | 45,000 | ~60% |
| Playwright MCP のスクショ/DOM | 15,000〜114,000 | 可変 |
| 会話履歴の再構築 | 10,000+ | ターンごとに増加 |
| **あなたの実際のプロンプト** | **~500** | **1%未満** |

平均22ターンのセッションで **100万トークン以上** — そのほとんどがオーバーヘッドです。

**helix-agent は各レイヤーを攻撃します:**
- ツールスキーマ → `defer_loading: true` の使い方をドキュメント化
- スクショ/DOM → `vision_compress` / `dom_compress`（97-99%削減）
- ブラウザ操作 → `agent-browser` バックエンド（82-93%削減）
- リトライループ → `retry_guard`（∞ → 0）
- ルーチン委譲 → ローカルLLM経由 `think`（$0 vs Opus ~$0.04/回）

## GPU 自動検出とモデルティア

helix-agent は **起動時にGPUを自動検出** し、各タスクに最適なモデルを選択します。8GB〜96GB+ のあらゆるGPUで動作します。

| GPU | VRAM | 選択モデル | DOM圧縮速度 | メモリレビュー速度 |
|---|---|---|---|---|
| RTX 4060 等 | 8GB | gemma4:e2b | **10.2秒** | **9.4秒** |
| RTX 4070 Ti / 5070 Ti 等 | 16GB | gemma4:e4b | **11.8秒** | **12.3秒** |
| RTX 4090 / 3090 等 | 24GB | gemma4:26b (MoE) | **14.7秒** | **14.4秒** |
| RTX PRO 6000 / A6000 等 | 48GB+ | gemma4:31b | 27.5秒 | 18.7秒 |

> **発見**: 8GB VRAMのgemma4:e2bは31bの **2.7倍速** で、圧縮タスクでは同等品質の出力を生成。高価なGPUは不要です。

```bash
# GPUに合ったモデルをインストールするだけ — 設定不要で自動選択:
ollama pull gemma4:e2b   # 8GB GPU
ollama pull gemma4:e4b   # 16GB GPU
ollama pull gemma4:26b   # 24GB GPU
ollama pull gemma4:31b   # 48GB+ GPU
```

## こんな人に最適

| あなたの悩み | helix-agent の解決策 |
|---|---|
| Maxプランなのに1〜2時間でレート制限に達する | スクショ/DOMを **97〜99%圧縮** してClaudeに渡す |
| Claudeが同じ失敗コマンドを10回以上繰り返す | `retry_guard` が3回目でループを自動停止 |
| Opusトークンを単純な読み取りタスクに消費している | ローカルLLMに自動委譲（**$0**） |
| 8GB GPUしかなくローカルLLMは無理だと思っている | **gemma4:e2b** を自動選択 — 2.7倍速で実用十分 |
| セッションをまたいでパターンを覚えてほしい | **自己進化記憶** がスキルと好みをローカル保存 |

## 他のツールにできなくて helix-agent にできること

| 機能 | helix-agent | 他のツール |
|---|---|---|
| スクショ→テキスト（97%トークン削減） | ✅ `vision_compress`（ローカルLLM） | ❌ これをやるMCPサーバーは他にない |
| DOM→テキスト（99%トークン削減） | ✅ `dom_compress`（ローカルLLM） | ❌ Playwright MCPは生DOMを送る |
| リトライループ検出 | ✅ `retry_guard`（サブms、LLM不要） | ❌ Claude Code本体に検出機能なし |
| GPU自動検出→モデル選択 | ✅ 8GB〜96GB+の5ティア | ❌ 他ツールは手動設定が必要 |
| 自己進化記憶 | ✅ hermes方式SKILL.md + Qdrant | ❌ helix-agent独自機能 |
| ブラウザ82〜93%トークン削減 | ✅ agent-browser + フォールバック | △ agent-browser単体（フォールバックなし） |
| MCP 3プリミティブ完全対応 | ✅ 27 Tools + 3 Resources + 3 Prompts | △ ほとんどのMCPはToolsのみ |

## 主要機能

### 1. Claude Code の「無限リトライ問題」を解決（`retry_guard`）

Opus は同じツールを同じ引数で何度も呼び続けることがあります（[anthropics/claude-code#41659](https://github.com/anthropics/claude-code/issues/41659)）。Max プランの 5 時間クォータが [19 分で消える事例](https://www.macrumors.com/2026/03/26/claude-code-users-rapid-rate-limit-drain-bug/) も報告されています。

公式にはループ検出機能がなく、コミュニティは「各自で hook を書く」状態でした。`retry_guard` はその hook を使い回せる MCP ツールとして提供します。

```python
retry_guard_check(tool_name="navigate", args={"url": "..."})
# → {"loop_detected": true, "repeat_count": 3,
#    "recommendation": "同じ引数で navigate を3回呼んでいます。ループの可能性があります。"}
```

| ツール | 役割 |
|---|---|
| `retry_guard_check` | ツール呼び出し前にループ検知 |
| `retry_guard_status` | セッション統計（呼び出し総数・ユニーク数・最大反復数） |
| `retry_guard_reset` | ループ解消後の履歴クリア |

### 2. スクリーンショット→テキスト自動変換（`vision_compress` / `dom_compress`）

核心のアイデア: **生画像やHTMLをClaudeに送らない。ローカルで先に圧縮する。**

```
┌──────────────┐     ┌─────────────────┐     ┌──────────────┐
│ スクリーンショット │────→│ vision_compress  │────→│ ~400 トークン │
│ (15,000 トークン) │     │ (ローカル gemma4) │     │ (テキストのみ) │
└──────────────┘     └─────────────────┘     └──────────────┘

┌──────────────┐     ┌─────────────────┐     ┌──────────────┐
│ DOM/HTML     │────→│ dom_compress     │────→│ ~500 トークン │
│ (114,000 トークン)│     │ (ローカル gemma4) │     │ (テキストのみ) │
└──────────────┘     └─────────────────┘     └──────────────┘
```

`computer_use(action="screenshot", analyze=True)` を呼ぶと、生画像は**レスポンスから自動削除**され、Claudeはテキスト要約のみ受け取ります。追加設定不要で透過的に動作します。

- **`vision_compress`** — スクショ → ローカルVision LLM → JSON（97%削減）
- **`dom_compress`** — HTML → ローカルLLM → JSON（99%削減）

実測例（RTX PRO 6000 で検証）:
```
入力:  1920×1048 の X.com スクリーンショット（通常 ~15,000 トークン）
出力:  "Xホームフィード、日本語UI、おすすめタブ、@Suryansh777 の
        Claude Code Resource Bible 投稿が表示" (~400 トークン)
節約:  1回の呼び出しで 7,362 トークン節約
```

### 3. ブラウザ自動化のトークンを 82〜93% 削減（v0.12.0）

`computer_use` は [Vercel agent-browser](https://github.com/vercel-labs/agent-browser)（Rust/CDP）を優先的に使い、利用不可なら helix-pilot → Playwright の順でフォールバックします。

50 件の同一フローで測定した結果：

| バックエンド | 1アクションあたりのトークン | React controlled component |
|---|---|---|
| Playwright (screenshot+DOM) | ~15,000 | ⚠️ setValue が無効化されることがある |
| agent-browser (アクセシビリティツリー) | ~1,000〜2,700 | ✅ ネイティブキー入力で通る |

`fill` がネイティブキーボードイベントを発火するため、Wantedly / LinkedIn など React SPA のフォームを追加ハックなしで埋められるようになりました。

### 4. 自律的画面確認（v0.14.0 NEW）

Claude Codeの `computer_use` は通常、スクリーンショットをそのまま返します（1枚 ~15,000トークン）。helix-agentはこれを自動的にインターセプトします：

```
操作: computer_use(action="click", target="#submit")
  ↓
確認: computer_use(action="screenshot", analyze=True)
  ↓ （生画像は自動削除、ローカルgemma4が解析）
結果: "フォーム送信完了、成功トーストが表示" (~400トークン)
```

**MCPサーバーの `instructions` フィールドがClaude Codeに自動的に指示:**
1. 生スクリーンショットではなく常に `vision_compress` を使用
2. 操作後は必ず `analyze=True` で画面確認
3. ツール呼び出し前に `retry_guard_check` でループ検出
4. ルーチンタスクは `think` でローカルLLMに委譲（$0）

**MCPサーバーを接続するだけで、Claude Codeが自律的にトークンを節約します** — ユーザーの介入不要。

### 5. 使うほど賢くなる自己進化記憶（v0.14.0 NEW）

[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) に着想を得た機能。会話を N ターンごとにローカル LLM がバックグラウンドでレビューし、**再利用可能なスキルとインサイトを自動保存** します — $0 で。

- **メモリナッジ**: 5ターンごとに gemma4 が会話をレビュー、保存すべき好み・修正を検出
- **スキル自動生成**: タスク完了パターン → SKILL.md ファイル（hermes互換フォーマット）
- **使うほど精度が上がる** — すべてローカル実行

```python
# 自動レビュー（MCPツールとしても手動呼出可能）
evolving_memory_review(user_message="...", assistant_response="...")
# → {"memory_reviewed": true, "skill_action": {"action": "create", "name": "..."}}

# 学習済みスキル一覧
list_learned_skills()
# → {"skills": [...], "stats": {"turns": 50, "skills_count": 3}}
```

### 6. Windows の日本語入力問題を解決（`helix-agent-ja-input`）

Claude Code は React Ink で TUI を構築しているため、IME との相性が悪く、**文字重複・カーソルずれ・変換中 Enter で暴発** といった問題が頻発します（[Zenn で完全解説](https://zenn.dev/atu4403/articles/claudecode-japanese-input-solution)）。

`helix-agent-ja-input` は OS ネイティブ IME で動く **フローティング入力ウィンドウ** を開きます:

```bash
uv run helix-agent-ja-input
```

1. 日本語で普通に入力（変換も正常動作）
2. `Ctrl+Enter` でクリップボードへコピー
3. Claude Code ターミナルで `Ctrl+V`

tkinter（標準ライブラリ）のみで実装されており、追加依存ゼロ・軽量です。macOS の [Prompt Line](https://qiita.com/nkmr_jp/items/c0dd480d320fc333e60a) の Windows 向け代替として作りました。

## 4層コードレビューパイプライン（v0.15.0, NEW）

複数LLMを連携させ、約¥30でコードレビュー網羅率ほぼ100%を目指す実装です:

```
Layer 2: gemma4 ReActレビュー（$0, web_search + RAG 付き）
  ↓ findings + context
Layer 3: Sonnet 4.6 検証 + クロスファイル解析（~¥10）
  ↓ マージされた findings
Layer 4: Opus 4.6 メタレビュー（~¥5, サマリのみ読む — 原文は読まない）
  ↓ 最終裁定
Codex:   コンサルタント（P1問題のみ、オンデマンド）
```

**実測比較（同一コードベース 5モデル比較）**:

| レビュアー | 検出件数 | 独自検出 | コスト |
|---|:---:|:---:|:---:|
| gemma4+RAG（ローカル） | 7 | 1 | **$0** |
| Codex GPT-5.3 | 5 | 0 | ~¥50 |
| Sonnet 4.6 | 14 | 1 | ~¥20 |
| Opus 4.6 | 16 | 4 | ~¥100 |
| **4層統合** | **16+** | **全部** | **~¥30** |

> **ポイント**: gemma4 + RAG（$0）が Codex GPT-5.3（~¥50）をコードレビュー品質で上回ります。

```python
# 日常レビュー（gemma4のみ、$0）
code_review(target="src/", skip_sonnet=True)

# リリース前（gemma4 + Sonnet、~¥10）
code_review(target="src/", context="決済モジュール")

# P1緊急（+ Codexコンサル）
code_review(target="src/", codex_consult=True)

# Codex reasoning effort を明示（none/minimal/low/medium/high/xhigh）
code_review(target="src/", codex_consult=True, codex_effort="xhigh")
```

**codex_effort 仕様**:

- 省略時は `high` がデフォルト
- **自動昇格**: P1 問題が 3 件以上検出されると Codex は自動的に `xhigh` で起動されます（手動指定不要)

## 自律運用・成長ループ（v0.15.0, NEW）

helix-agent は `scripts/` 配下に自己保守用ハーネスを同梱しています。**監査 → 派遣 → 自動修復** の連鎖を Windows タスクスケジューラで定期実行することで、Claude Code は自己修復基盤の上で動きます。

| スクリプト | 役割 |
|---|---|
| `scripts/system_auditor.py` | 記憶・フック・サービス・整合性の定期監査 |
| `scripts/anomaly_dispatcher.py` | 検知した異常を適切な部門/エージェントへ派遣 |
| `scripts/env_self_heal.py` | 環境リグレッション（サービス/依存/パス）の自動修復 |
| `scripts/critical_files_guard.py` | `CLAUDE.md`/`settings.json` 等の重要ファイル保護（SHA-256スナップショット、30世代） |
| `scripts/helix_overview.py` | Claude 自身が 1 コマンドで 9 領域（会社/記憶/RAG/異常/設定/起動/生成物/セキュリティ/保守）を俯瞰 |
| `scripts/dept_feed_bridge.py` | 部門別 Qdrant RAG（dept_hr/research/design/build/qa）へのライブ給餌 |
| `scripts/dept_dataset_builder.py` | 部門 RAG からファインチューニング用データセットを生成 |
| `scripts/dept_ft_advisor.py` | 部門の FT 準備度を評価・推奨 |
| `scripts/supervisor.py` | 常駐デーモン 9 本の監視・再起動 |

## 並列タスク実行 (v0.15.1, NEW)

複数タスクを同時実行し、タスク種別×入力サイズで最適モデルを自動選択:

```python
parallel_tasks(tasks='[
    {"task": "このコードを要約", "type": "summarize", "context": "..."},
    {"task": "英語に翻訳: ...", "type": "translate"},
    {"task": "分類して", "type": "classify"},
    {"task": "ベストプラクティスを検索", "type": "search"},
    {"task": "セキュリティレビュー", "type": "review", "context": "..."}
]')
```

**2軸自動モデル選択** — タスク種別 × 入力複雑度:

| 入力サイズ | 要約/翻訳/分類 | 検索/コード生成 | レビュー |
|---|---|---|---|
| 短い (<3K字) | gemma4:e2b (**3-6秒**) | gemma4:e4b (26秒) | gemma4:31b |
| 中程度 (3-8K字) | gemma4:e4b (12秒) | gemma4:31b | gemma4:31b |
| 長い (>8K字) | gemma4:31b (21秒) | gemma4:31b | gemma4:31b |

ベンチマーク (5タスク同時実行, clip-bridge 501行):

| 構成 | 時間 | VRAM | 品質 |
|---|---|---|---|
| **e2b+e4b混合並列** | **51秒** | **10GB** | 5タスク全成功 |
| e4b×3専門家並列 | 85秒 | 6GB | P1=2件検出 |
| 31b単独 | 130秒 | 20GB | P1=2, P2=1, P3=2 |

軽量タスク(e2b/e4b)は`asyncio.gather`で並列実行。重いタスク(31b)はGPU競合防止のため順次実行。

## MCPツール一覧

合計 **27 MCPツール** + 3 Resources + 3 Prompts（v0.15.0時点、`server.py`）。主要カテゴリ別:

- **委譲**: `think` / `agent_task` / `parallel_tasks` / `fork_task`
- **ビジョン/ブラウザ**: `see` / `browse` / `computer_use` / `vision_compress` / `dom_compress`
- **トークン防衛**: `retry_guard_check` / `retry_guard_status` / `retry_guard_reset`
- **記憶**: `dept_search` / `dept_store`（部門別 Qdrant: dept_hr/research/design/build/qa、mem0_shared）/ `evolving_memory_review` / `list_learned_skills` / `get_skill`
- **バックグラウンド**: `spawn_agent` / `send_agent_input` / `wait_agent` / `list_agents` / `close_agent`
- **品質**: `code_review`（4層レビューパイプライン、`codex_effort` 指定可）
- **メタ**: `providers` / `models` / `config` / `agent_types`

## クイックスタート

```bash
git clone https://github.com/tsunamayo7/helix-agent.git
cd helix-agent
uv sync
```

**MCP サーバーとして Claude Code に登録する場合** (`~/.claude/settings.json`):

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

**日本語入力ヘルパーだけ使う場合**:

```bash
uv run helix-agent-ja-input
```

## 必要環境

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- [Ollama](https://ollama.ai/) + Gemma 4 モデル（GPUに応じて自動選択）:
  - 8GB VRAM: `ollama pull gemma4:e2b`（実効2.3B、4GB）
  - 16GB VRAM: `ollama pull gemma4:e4b`（実効4.5B、6GB）
  - 24GB VRAM: `ollama pull gemma4:26b`（MoE 実効3.8B、12GB）
  - 48GB+ VRAM: `ollama pull gemma4:31b`（dense 30.7B、20GB）

オプション:
- Qdrant（共有記憶 + 自己進化記憶の永続化）
- [agent-browser](https://github.com/vercel-labs/agent-browser)（ブラウザ操作の82-93%トークン削減に推奨）
- Playwright（ブラウザ自動化のフォールバック）

## 今後の日本語機能（v1.2 以降予定）

- **`ja_screen_read`** — PaddleOCR + gemma4:31b で日本語 UI スクショを解析、日本語 JSON を返す MCP ツール
- **`ja_term_expand`** — 日本語技術用語 → 英語変換（エラーメッセージ翻訳検索支援）

## セキュリティ

Claude Code のプロンプトインジェクション脆弱性（[CVE-2025-59536](https://research.checkpoint.com/2026/rce-and-api-token-exfiltration-through-claude-code-project-files-cve-2025-59536/)）への対策として **PathGuard**（パス許可リスト+サニタイズ）を同梱しています。詳細は [SECURITY.md](SECURITY.md) を参照してください。

## ライセンス

MIT

## 関連プロジェクト

- [helix-ai-studio](https://github.com/tsunamayo7/helix-ai-studio) — 7プロバイダー対応の統合 AI チャットスタジオ
- [helix-pilot](https://github.com/tsunamayo7/helix-pilot) — ローカル Vision LLM で Windows デスクトップを操作する MCP
- [claude-code-codex-agents](https://github.com/tsunamayo7/claude-code-codex-agents) — Codex CLI ブリッジ MCP
- [helix-sandbox](https://github.com/tsunamayo7/helix-sandbox) — Docker + Windows Sandbox 連携 MCP
