# helix-agent（日本語ドキュメント）

**Claude Code のトークン消費を 82〜97% 自動削減。** リトライループ検知 + スクショ/DOM のローカルLLM圧縮 + GPU自動検出によるモデル最適選択 — すべて1つの MCP サーバーで。

English README: **[README.md](README.md)**

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-330%20passing-brightgreen.svg)](#)
[![v0.14.0](https://img.shields.io/badge/version-0.14.0-7c3aed.svg)](#)
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
| MCP 3プリミティブ完全対応 | ✅ 23 Tools + 3 Resources + 3 Prompts | △ ほとんどのMCPはToolsのみ |

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

### 2. トークン消費を 94〜99% 削減（`vision_compress` / `dom_compress`）

スクリーンショット 1 枚で 15,000 トークン、DOM 1 ページで 114,000 トークンが消費されます。これらをローカル gemma4:31b で要約し、Claude には ~400 トークンの JSON だけ渡します。

- **`vision_compress`** — スクショ → `{page_type, interactive_elements, state_flags}` JSON
- **`dom_compress`** — HTML → `{forms, links, buttons, next_action_candidates}` JSON

### 3. ブラウザ自動化のトークンを 82〜93% 削減（v0.12.0）

`computer_use` は [Vercel agent-browser](https://github.com/vercel-labs/agent-browser)（Rust/CDP）を優先的に使い、利用不可なら helix-pilot → Playwright の順でフォールバックします。

50 件の同一フローで測定した結果：

| バックエンド | 1アクションあたりのトークン | React controlled component |
|---|---|---|
| Playwright (screenshot+DOM) | ~15,000 | ⚠️ setValue が無効化されることがある |
| agent-browser (アクセシビリティツリー) | ~1,000〜2,700 | ✅ ネイティブキー入力で通る |

`fill` がネイティブキーボードイベントを発火するため、Wantedly / LinkedIn など React SPA のフォームを追加ハックなしで埋められるようになりました。

### 4. 使うほど賢くなる自己進化記憶（v0.14.0 NEW）

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

### 5. Windows の日本語入力問題を解決（`helix-agent-ja-input`）

Claude Code は React Ink で TUI を構築しているため、IME との相性が悪く、**文字重複・カーソルずれ・変換中 Enter で暴発** といった問題が頻発します（[Zenn で完全解説](https://zenn.dev/atu4403/articles/claudecode-japanese-input-solution)）。

`helix-agent-ja-input` は OS ネイティブ IME で動く **フローティング入力ウィンドウ** を開きます:

```bash
uv run helix-agent-ja-input
```

1. 日本語で普通に入力（変換も正常動作）
2. `Ctrl+Enter` でクリップボードへコピー
3. Claude Code ターミナルで `Ctrl+V`

tkinter（標準ライブラリ）のみで実装されており、追加依存ゼロ・軽量です。macOS の [Prompt Line](https://qiita.com/nkmr_jp/items/c0dd480d320fc333e60a) の Windows 向け代替として作りました。

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
