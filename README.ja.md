# helix-agent（日本語ドキュメント）

**Claude Code の日本語ユーザーのためのサバイバルキット** — リトライループ検知 + スクリーンショット/DOM 圧縮 + Windows 日本語入力ヘルパーを1つの MCP サーバーで提供します。

English README: **[README.md](README.md)**

## なぜ helix-agent か

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

### 4. Windows の日本語入力問題を解決（`helix-agent-ja-input`）

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
- vision/DOM 圧縮を使う場合: [Ollama](https://ollama.ai/) + gemma4:31b（VRAM 20GB 以上推奨）
- `helix-agent-ja-input` のみ: 追加不要（tkinter は標準ライブラリ）

オプション:
- Qdrant（共有記憶用）
- Playwright（ブラウザ自動化）
- PaddleOCR: `pip install helix-agent[ja]`（今後の `ja_screen_read` 用）

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
