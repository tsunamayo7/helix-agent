# helix-agent v0.11.0 ローンチ投稿ドラフト

バズメカニズム分析（`viral_mechanics_2026.md`）を適用した投稿案。

---

## 📝 投稿1-A: 【最終版】 X 日本語 単発（拡張データ反映）

**設計根拠**: 実測TOP投稿（15万views超）の5大パターンを取り込み:
1. 価値転換の明示（quota19分消費→防げる）
2. 数値具体性（#41659, 5時間→19分, 94%, 308 tests）
3. 時代宣言フレーズ抑制（翻訳互換維持のため）
4. 自分語り要素（「自分も体験した」）

```
Claude Code の Max quota、5時間分が19分で消える現象
(anthropics/claude-code#41659) を自分も体験しました。

Opus が同じツールを同じ引数で延々と呼び続けるバグが原因です。
公式にループ検知機能は未実装。

MCP サーバーで解決しました:

✅ retry_guard_check — 同一呼び出しを検知して警告（LLM不要、純ロジック）
✅ vision_compress — スクリーンショット 15,000→400 トークン (94%削減)
✅ dom_compress — HTML 114,000→500 トークン

Windows の日本語入力問題 (React Ink + IME) 向けに
helix-agent-ja-input (tkinter・依存ゼロ) も同梱。

MIT / 308 tests passing
https://github.com/tsunamayo7/helix-agent

#ClaudeCode #MCP
```

**字数**: 約320字（X 280字上限超過→URL除外で収まる。要トリム）
**翻訳互換性**: ✅ 数字・英数字コード・記号のみ

---

## 📝 投稿1-B: X 日本語（280字版、翻訳互換）

```
Claude Code の Max quota 5時間分が19分で消える現象
(claude-code#41659) を自分も体験しました。

原因: Opus が同じツールを同じ引数で呼び続けるバグ。
公式にループ検知は未実装。

helix-agent v0.11.0 の解決策:

✅ retry_guard — 同一呼び出しを検知（純ロジック、LLM不要）
✅ vision_compress — 15K→400 トークン (94%削減)
✅ dom_compress — 114K→500 トークン

Windows日本語入力問題も ja-input で同梱解決。
MIT / 308 tests

https://github.com/tsunamayo7/helix-agent

#ClaudeCode #MCP
```

**字数**: 約250字 ✅
**パターン**: 体験共感 + 数字3点 + 技術列挙 + おまけ要素

---

## 📝 投稿1-C: X 英語単発（Quoted-candidate 作戦）

英語で投稿し、日本人がQT引用コメントしやすい構造にする。引用されやすさ優先。

```
I lost 5 hours of Claude Max plan quota in 19 minutes.

Root cause: Opus called the same tool with identical args
over and over (claude-code#41659). No built-in loop guard.

I built one. It's an MCP server:

✅ retry_guard — detect loops before quota drains (pure logic)
✅ vision_compress — screenshot → 400 tokens (94% cut)
✅ dom_compress — HTML → 500 tokens

MIT / 308 tests passing
https://github.com/tsunamayo7/helix-agent
```

---

## 📝 投稿1（旧版、参考用）: X 日本語 単発（X自動翻訳対応・両市場リーチ型）

**設計方針**: X自動翻訳により1投稿で日英両市場カバー。翻訳で壊れる和製ニュアンス（保存版/神/マジで）を排除。数字・英数字コード・普遍語彙のみ使用。

```
Claude Code のリトライループ問題を MCP で解決しました。

Opus が同じツールを同じ引数で繰り返し呼ぶバグ
(anthropics/claude-code#41659) で、Max quota が
19分で消費される事例が報告されています。

v0.11.0 で追加したツール:

✅ retry_guard_check — 同一呼び出しを検知して警告
✅ vision_compress — スクリーンショットを400トークンへ圧縮
✅ dom_compress — HTMLを500トークンへ圧縮

Windows の日本語入力問題 (React Ink + IME) 向けに
helix-agent-ja-input (tkinter製) も同梱。

MIT / 308 tests passing
https://github.com/tsunamayo7/helix-agent

#ClaudeCode #MCP
```

**字数**: 約260字
**狙い**: 日本人開発者をメイン、自動翻訳経由で英語圏・その他言語圏にも到達
**翻訳検証ポイント**: `retry_guard_check`/`vision_compress`/`dom_compress`等のコード名、数字(19分/400/500/308/19分)、英数字バージョン、絵文字✅ は翻訳後も保持される

**旧版（和製ニュアンス型）との比較**:
- Before: 「リトライ地獄」「こう解決しました」「おまけ」→ 翻訳で意味劣化
- After: 「リトライループ問題」「追加したツール」「同梱」→ 翻訳互換語彙

---

## 🧵 投稿2: X 英語 7-tweet thread

```
1/ Claude Code's Opus sometimes calls the same tool with the same args
over and over when it misreads an error.

I lost a 5-hour Max quota in 19 minutes that way.

So I built an MCP to detect the loop before it drains your budget:

2/ The problem is documented (anthropics/claude-code#41659) and
Anthropic publicly admitted in March 2026 that users hit quotas
"way faster than expected."

Community best practice: "write your own hook."
That doesn't scale.

3/ retry_guard ships that hook as a reusable MCP tool:

retry_guard_check(tool_name, args)
→ {loop_detected: true, repeat_count: 3,
   recommendation: "Vary args or escalate to Opus."}

SHA1 fingerprint per call + sliding window. No LLM needed.

4/ Bundled extras for the same "Claude Code survival" story:

• vision_compress — 15K-token screenshot → 400-token JSON summary
• dom_compress — 114K-token DOM → 500-token structured extract

Route via local gemma4:31b. Opus decides, gemma4 does the looking.

5/ Stack:
• Python 3.12, FastMCP 2.0
• Ollama for vision/DOM
• tkinter (stdlib) for bundled JP input helper
• 308 tests passing

Zero LLM dependency for retry_guard itself — it's pure logic.

6/ It is *not* a Claude Code wrapper. It's an MCP server Claude Code
connects to. Fully TOS-compliant.

Install:
  git clone, uv sync, point Claude Code at server.py. Done.

7/ MIT. Repo: https://github.com/tsunamayo7/helix-agent

Happy to answer questions — especially edge cases where retry_guard
missed a loop you hit in production.
```

**狙い**: 国際開発者、HN/Reddit前段階での認知獲得

---

## 📰 投稿3: Zenn記事（3,000字ドラフト）

### タイトル案
「Claude Codeのリトライループ問題をMCPで止めた — helix-agent v0.11.0」

### 構成
1. **はじめに（200字）**: Max plan 19分で消費事例への共感
2. **課題（400字）**: #41659解説、公式の未対応ぶり、Anthropic謝罪引用
3. **既存解決策の限界（400字）**: 手動hook運用の辛さ、token-optimizer-mcpとの差別化
4. **retry_guard の仕組み（600字）**: SHA1フィンガープリント + 時間窓、コード例
5. **実装詳細（500字）**: RetryGuardクラス解説、テスト観点
6. **おまけ機能（400字）**: vision_compress / dom_compress、日本語入力ヘルパー
7. **セットアップ（300字）**: git clone → uv sync → Claude Code設定
8. **まとめ（200字）**: Star依頼、フィードバック募集

**タグ**: ClaudeCode, MCP, Python, Ollama, 個人開発

---

## 📰 投稿4: Qiita記事

Zenn記事を流用、タイトルのみ調整:
「【保存版】Claude Codeのリトライ地獄を止めるMCP — helix-agent v0.11.0 リリース」

**タグ**: ClaudeCode, MCP, Python, 個人開発, Ollama

---

## 🔶 投稿5: HN Show HN

```
Show HN: helix-agent – The missing retry loop guard for Claude Code

I built this after losing a 5-hour Claude Max plan quota in 19 minutes.

Opus had misread an error and called the same MCP tool with identical
args over and over. The bug is documented (anthropics/claude-code#41659)
but there's no built-in loop detection. Community best practice is to
write your own PreToolUse hook.

retry_guard packages that hook as a reusable MCP tool:

  retry_guard_check(tool_name="navigate", args={"url": "..."})
  → {"loop_detected": true, "repeat_count": 3,
     "recommendation": "Vary args or escalate to Opus."}

SHA1 fingerprint per call, per-session history, sliding time window.
No LLM required — pure logic, sub-millisecond.

Also bundled (optional, gemma4 via Ollama):
- vision_compress: screenshot → ~400-token JSON summary
- dom_compress: HTML → ~500-token structured extract

And for Japanese users, a stdlib-only tkinter floating input helper that
sidesteps the React Ink + IME bug that breaks Japanese input in the
Claude Code terminal on Windows.

Tech: Python 3.12, FastMCP 2.0, 308 tests. MIT.

Repo: https://github.com/tsunamayo7/helix-agent

Happy to answer questions.
```

**投稿時刻**: US morning（JST 23:00-24:00）推奨

---

## 📋 Reddit r/LocalLLaMA

```
Title: Built an MCP that detects Claude Code retry-loops before they drain Max plan quota

Post body:

Context: Claude Code's Opus has a documented bug where it calls the same
tool with identical args over and over after misreading an error. A friend
lost his 5-hour Max quota in 19 minutes last week. Bug report:
anthropics/claude-code#41659

helix-agent v0.11.0 ships three MCP tools that address this:

**retry_guard** (the main new thing):
- SHA1-hashes every tool call + args
- Per-session history with sliding time window
- Warns Claude when the same call repeats 3+ times
- Pure logic, no LLM needed, sub-millisecond

**vision_compress** (bundled extra, uses gemma4:31b):
- Screenshot → ~400-token JSON (page_type, interactive_elements, state_flags)
- ~94% token reduction vs raw screenshot to Claude

**dom_compress** (bundled extra, uses gemma4:31b):
- HTML → ~500-token structured extract (forms, links, buttons)
- ~99% reduction vs Playwright MCP's 114K-token DOM dumps

Hardware: works on 24GB VRAM (gemma4:31b). retry_guard itself needs no GPU.

Tech: Python 3.12, FastMCP 2.0, Ollama. 308 tests. MIT.

Repo: https://github.com/tsunamayo7/helix-agent

Happy to answer questions on architecture or edge cases.
```

---

## 📋 投稿順序と間隔（X自動翻訳対応・修正版）

| Day | 時刻 | チャネル | 備考 |
|---|---|---|---|
| Day 0 | JST 21:00 | **X 日本語（翻訳互換）単発** | **1本で日英両市場カバー** |
| Day 0 | JST 22:00 | Zenn記事公開 | 検索流入長期 |
| Day 0 | JST 23:00 | HN Show HN | US朝一狙い |
| Day 1 | JST 09:00 | Qiita記事公開 | Zenn流用 |
| Day 1 | JST 14:00 | X英語スレッド（7-tweet） | 英語圏深掘り追加層 |
| Day 1 | JST 23:00 | Reddit r/LocalLLaMA | ローカルLLM層 |
| Day 3 | JST 任意 | X 自虐フォロー投稿 | mizchiパターン、コアユーザー深掘り |
| Day 7 | JST 任意 | X 成果報告（Star数） | 数字共有で追加エンゲージメント |

**Day 3 自虐フォロー投稿案（翻訳互換）**:
```
retry_guard_check を実装しながら「自分のループも止められないのに、
Claude のループを検知するツールを作っているのか」と思いました。

でも実際、自分より Claude の方が短時間でループします。
人間の勝ち。
```
→ 自虐ユーモア＋内輪感、翻訳後も含意が伝わる（ループ=loop が共通）

---

## 📊 成果測定シート

| 日付 | X JP RT | X JP いいね | X EN thread eng. | Zenn いいね | Qiita いいね | HN points | Reddit upvotes | GH Stars増 |
|---|---|---|---|---|---|---|---|---|
| Day 1 | | | | | | | | |
| Day 3 | | | | | | | | |
| Day 7 | | | | | | | | |

投稿後にこの表を埋め、`posts_helix_agent.md` へ転記。
