"""Gemini CDP Worker — gemini.google.comでの事実検証テンプレート生成.

Sonnet Agent に渡すCDP操作プロンプトを生成する。
CDP操作自体はこのスクリプトでは行わない（chrome-devtools MCPが担当）。

制約 (CLAUDE.md):
  - Gemini 2垢並行: ページ2(tsuna.konomiya) + ページ9(tomotomlo777)
  - Geminiハルシネーション防止: 「Google検索実行+URL必須」テンプレート厳守
  - URL無しデータは不採用
  - CDPスナップショット(take_snapshot)は初回1回のみ
  - CDP並行操作禁止: 1サービス=1エージェントで直列実行
"""

from __future__ import annotations


# デフォルトアカウント設定
DEFAULT_ACCOUNT = "tsuna.konomiya"  # ページ2
SECONDARY_ACCOUNT = "tomotomlo777"  # ページ9

# ハルシネーション防止プレフィックス
GROUNDING_PREFIX = (
    "以下の質問についてGoogle検索を実行して最新情報を確認してください。"
    "回答には参照URLを必ず含めてください。URL無しの情報は不採用とします。\n\n"
)


def generate_agent_prompt(
    task: dict,
    account: str = DEFAULT_ACCOUNT,
    use_grounding: bool = True,
) -> str:
    """Sonnet Agentに渡すCDP操作プロンプトを生成.

    Args:
        task: orchestratorから取得したタスクdict
              必須キー: id, target, task
              任意キー: context, priority
        account: 使用するGeminiアカウント
        use_grounding: Google検索テンプレートを適用するか

    Returns:
        Sonnet Agentに渡すプロンプト文字列
    """
    task_text = task.get("task", "")
    context = task.get("context", "")
    task_id = task.get("id", "unknown")

    # ハルシネーション防止テンプレート適用
    if use_grounding:
        input_text = GROUNDING_PREFIX + task_text
    else:
        input_text = task_text

    context_section = ""
    if context:
        context_section = f"""
## 追加コンテキスト
{context}
"""

    account_info = (
        f"ページ2 ({DEFAULT_ACCOUNT})"
        if account == DEFAULT_ACCOUNT
        else f"ページ9 ({SECONDARY_ACCOUNT})"
    )

    return f"""gemini.google.com で事実検証を実行してください。

## タスクID
{task_id}

## 検証内容
{task_text}
{context_section}
## 使用アカウント
{account_info}

## CDP操作手順

### Geminiページを探す
1. chrome-devtools list_pages を実行
2. URLに "gemini.google.com" を含むページを探す
3. 複数ある場合は {account_info} のページを選択

### ページがある場合
1. chrome-devtools select_page でそのページを選択
2. アカウント確認:
   chrome-devtools evaluate_script:
   `(() => {{ const avatar = document.querySelector('[data-ogsr-up] img, [aria-label*="Google "]'); return avatar ? avatar.getAttribute('aria-label') || avatar.alt || 'unknown' : 'not found'; }})()`

### ページがない場合
1. chrome-devtools navigate_page で https://gemini.google.com を開く
2. 10秒待機 (chrome-devtools wait_for)
3. アカウントを確認し、{account} でなければアカウント切替

### メッセージ送信 (ハルシネーション防止テンプレート適用済み)
1. テキストエリアを探す:
   chrome-devtools evaluate_script:
   `document.querySelector('.ql-editor, [contenteditable="true"], textarea, [aria-label*="prompt"], [aria-label*="Enter"]')`
2. テキストエリアに以下を入力 (chrome-devtools type_text):

{input_text}

3. 送信ボタンをクリック:
   chrome-devtools evaluate_script:
   `document.querySelector('button[aria-label*="Send"], button[aria-label*="送信"], button[class*="send"]')`
   見つかったら chrome-devtools click でクリック

### 応答待ち
1. 5秒間隔で最大60秒ポーリング
2. chrome-devtools evaluate_script で応答要素を監視:
   `(() => {{ const msgs = document.querySelectorAll('[class*="response"], [class*="model-response"], .markdown'); if (msgs.length === 0) return null; return msgs[msgs.length - 1].innerText; }})()`
3. 2回連続で応答テキストが同じ長さなら完了と判断

### 応答取得
chrome-devtools evaluate_script:
`(() => {{ const msgs = document.querySelectorAll('[class*="response"], [class*="model-response"], .markdown'); if (msgs.length === 0) return '応答取得失敗'; return msgs[msgs.length - 1].innerText; }})()`

### URL検証 (ハルシネーション防止)
応答テキストを取得したら、以下を確認:
chrome-devtools evaluate_script:
`(() => {{ const resp = document.querySelectorAll('[class*="response"], [class*="model-response"], .markdown'); if (resp.length === 0) return false; const links = resp[resp.length - 1].querySelectorAll('a[href]'); return links.length > 0; }})()`

- URLが含まれていない場合: 結果に「**警告: 参照URLなし — ハルシネーションの可能性あり**」を付記

## 重要ルール
- take_snapshot は使用しない（初回以外）
- evaluate_script でDOM直接操作を優先
- **Google検索実行+URL必須テンプレートを厳守**
- URL無し回答は不採用とし、警告を付記
- セレクタが見つからない場合は代替セレクタを試す
- 60秒以内に応答が得られない場合はタイムアウトとして報告

## 結果報告
取得した応答テキストをそのまま報告してください。
URL検証結果（URLの有無）も必ず付記してください。
"""


def generate_completion_command(task_id: str, status: str = "completed") -> str:
    """タスク完了コマンドを生成.

    Args:
        task_id: タスクのUUID
        status: completed | failed

    Returns:
        実行すべきbashコマンド文字列（{{result}}プレースホルダー含む）
    """
    script = "~/Development/tools/helix-agent/scripts/browser_ai_orchestrator.py"
    return (
        f'python3 {script} complete --id {task_id} '
        f'--result "{{{{result}}}}" --status {status}'
    )


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) < 2:
        print("使い方: python3 gemini_worker.py '<task_json>'", file=sys.stderr)
        sys.exit(1)

    task = json.loads(sys.argv[1])
    print(generate_agent_prompt(task))
