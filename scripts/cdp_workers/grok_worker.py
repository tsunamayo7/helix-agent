"""Grok CDP Worker — grok.comでの調査実行テンプレート生成.

Sonnet Agent に渡すCDP操作プロンプトを生成する。
CDP操作自体はこのスクリプトでは行わない（chrome-devtools MCPが担当）。

制約 (CLAUDE.md):
  - Grok操作時はChatGPTタブを先に閉じる（Service WorkerがCDPターゲットを奪う）
  - CDPスナップショット(take_snapshot)は初回1回のみ
  - CDP並行操作禁止: 1サービス=1エージェントで直列実行
"""

from __future__ import annotations


def generate_agent_prompt(task: dict) -> str:
    """Sonnet Agentに渡すCDP操作プロンプトを生成.

    Args:
        task: orchestratorから取得したタスクdict
              必須キー: id, target, task
              任意キー: context, priority

    Returns:
        Sonnet Agentに渡すプロンプト文字列
    """
    task_text = task.get("task", "")
    context = task.get("context", "")
    task_id = task.get("id", "unknown")

    context_section = ""
    if context:
        context_section = f"""
## 追加コンテキスト
{context}
"""

    return f"""grok.com でX/最新情報を調査してください。

## タスクID
{task_id}

## 調査内容
{task_text}
{context_section}
## CDP操作手順

### 事前準備: ChatGPTタブを閉じる
1. chrome-devtools list_pages を実行
2. URLに "chatgpt.com" を含むページがあれば chrome-devtools close_page で閉じる
   (Service WorkerがCDPターゲットを奪うため必須)
3. 2秒待機

### Grok操作
1. chrome-devtools list_pages で grok.com のページを探す
2. あれば chrome-devtools select_page でそのページを選択
3. なければ chrome-devtools navigate_page で https://grok.com を開く
4. 10秒待機 (chrome-devtools wait_for)
5. テキストエリアを探す:
   chrome-devtools evaluate_script:
   `document.querySelector('textarea, [contenteditable="true"]')`
6. テキストエリアに以下を入力 (chrome-devtools type_text):

{task_text}

7. 送信ボタンを探してクリック:
   chrome-devtools evaluate_script:
   `document.querySelector('button[aria-label*="Send"], button[aria-label*="送信"], form button[type="submit"]')`
   見つかったら chrome-devtools click でクリック
8. 応答完了を待つ:
   - 5秒間隔で最大60秒ポーリング
   - chrome-devtools evaluate_script で応答テキストの長さを確認
   - 2回連続で同じ長さなら完了と判断
9. 応答テキストを取得:
   chrome-devtools evaluate_script:
   `(() => {{ const msgs = document.querySelectorAll('[class*="message"], [class*="response"], [data-testid*="message"]'); return msgs.length > 0 ? msgs[msgs.length - 1].innerText : '応答取得失敗'; }})()`

## 重要ルール
- take_snapshot は使用しない（初回以外）
- evaluate_script でDOM直接操作を優先
- セレクタが見つからない場合は代替セレクタを試す
- 60秒以内に応答が得られない場合はタイムアウトとして報告

## 結果報告
取得した応答テキストをそのまま報告してください。
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
        print("使い方: python3 grok_worker.py '<task_json>'", file=sys.stderr)
        sys.exit(1)

    task = json.loads(sys.argv[1])
    print(generate_agent_prompt(task))
