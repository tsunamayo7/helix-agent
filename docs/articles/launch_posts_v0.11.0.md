# helix-agent v0.11.0 ローンチ投稿ドラフト

バズメカニズム分析（`viral_mechanics_2026.md`）を適用した投稿案。

---

## 📝 投稿1: X 日本語 単発（保存版リスト型）

```
【Claude Codeのリトライ地獄、MCPで止められます】

Opusが同じツールを延々と呼んで Max quota が19分で消える問題
(anthropics/claude-code#41659)、公式にループ検知は未実装。

v0.11.0 でこう解決しました:

✅ retry_guard_check — 同一呼び出しを検知して警告
✅ vision_compress — スクショ→400トークンJSONへ圧縮
✅ dom_compress — HTML→500トークンへ圧縮

おまけ: Windowsの日本語入力問題(React Ink+IME)も
helix-agent-ja-input で一緒に解決しました。

MIT・308テスト通過
https://github.com/tsunamayo7/helix-agent

#ClaudeCode #MCP
```

**字数**: 約280字（Xは280字制限適合）
**狙い**: 日本人開発者、IMEで困っている層、Max plan枯渇経験者

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

## 📋 投稿順序と間隔

| Day | 時刻 | チャネル |
|---|---|---|
| Day 0 | JST 21:00 | X 日本語単発 |
| Day 0 | JST 22:00 | Zenn記事公開 |
| Day 0 | JST 23:00 | HN Show HN |
| Day 1 | JST 09:00 | Qiita記事公開 |
| Day 1 | JST 14:00 | X 英語スレッド（7-tweet） |
| Day 1 | JST 23:00 | Reddit r/LocalLLaMA |
| Day 3 | JST 任意 | X で反応まとめ・Star数報告 |

---

## 📊 成果測定シート

| 日付 | X JP RT | X JP いいね | X EN thread eng. | Zenn いいね | Qiita いいね | HN points | Reddit upvotes | GH Stars増 |
|---|---|---|---|---|---|---|---|---|
| Day 1 | | | | | | | | |
| Day 3 | | | | | | | | |
| Day 7 | | | | | | | | |

投稿後にこの表を埋め、`posts_helix_agent.md` へ転記。
