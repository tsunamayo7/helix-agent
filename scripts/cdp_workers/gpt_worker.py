"""GPT CDP Worker — chatgpt.comでの分析実行テンプレート生成.

Sonnet Agent に渡すCDP操作プロンプトを生成する。
CDP操作自体はこのスクリプトでは行わない（chrome-devtools MCPが担当）。

制約 (CLAUDE.md):
  - GPT: 「環境構築」プロジェクト内チャットで実行
  - CDPスナップショット(take_snapshot)は初回1回のみ
  - CDP並行操作禁止: 1サービス=1エージェントで直列実行
"""

from __future__ import annotations


# ChatGPT「環境構築」プロジェクトのURLパス
# Corp全体構造ファイルを情報源に登録済みのプロジェクト
GPT_PROJECT_NAME = "環境構築"


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

    return f"""chatgpt.com の「{GPT_PROJECT_NAME}」プロジェクトで分析を依頼してください。

## タスクID
{task_id}

## 分析内容
{task_text}
{context_section}
## CDP操作手順

### ChatGPTタブを探す
1. chrome-devtools list_pages を実行
2. URLに "chatgpt.com" を含むページを探す

### ページがある場合
1. chrome-devtools select_page でそのページを選択
2. 現在のURLを確認 (chrome-devtools evaluate_script: `window.location.href`)
3. 「{GPT_PROJECT_NAME}」プロジェクトのチャットでなければ:
   - サイドバーからプロジェクトを探す
   - chrome-devtools evaluate_script:
     `(() => {{ const items = document.querySelectorAll('nav a, [class*="conversation"] a'); for (const a of items) {{ if (a.textContent.includes('{GPT_PROJECT_NAME}')) return a.href; }} return null; }})()`
   - 見つかったURLに chrome-devtools navigate_page で移動

### ページがない場合
1. chrome-devtools navigate_page で https://chatgpt.com を開く
2. 10秒待機 (chrome-devtools wait_for)
3. 上記と同じ手順で「{GPT_PROJECT_NAME}」プロジェクトに移動

### メッセージ送信
1. テキストエリアを探す:
   chrome-devtools evaluate_script:
   `document.querySelector('#prompt-textarea, textarea, [contenteditable="true"]')`
2. テキストエリアに以下を入力 (chrome-devtools type_text):

{task_text}

3. 送信ボタンをクリック:
   chrome-devtools evaluate_script:
   `document.querySelector('button[data-testid="send-button"], button[aria-label*="Send"], button[aria-label*="送信"]')`
   見つかったら chrome-devtools click でクリック

### 応答待ち
1. 5秒間隔で最大60秒ポーリング
2. chrome-devtools evaluate_script で応答要素を監視:
   `(() => {{ const msgs = document.querySelectorAll('[data-message-author-role="assistant"]'); if (msgs.length === 0) return null; return msgs[msgs.length - 1].innerText; }})()`
3. ストリーミング完了判定:
   - 送信ボタンが再度表示される（停止ボタンが消える）
   - chrome-devtools evaluate_script:
     `!!document.querySelector('button[data-testid="send-button"]') && !document.querySelector('button[aria-label*="Stop"]')`
4. 2回連続で応答テキストが同じなら完了と判断

### 応答取得
chrome-devtools evaluate_script:
`(() => {{ const msgs = document.querySelectorAll('[data-message-author-role="assistant"]'); if (msgs.length === 0) return '応答取得失敗'; return msgs[msgs.length - 1].innerText; }})()`

## 重要ルール
- take_snapshot は使用しない（初回以外）
- evaluate_script でDOM直接操作を優先
- 「{GPT_PROJECT_NAME}」プロジェクト内で実行すること（Corp全体構造が情報源として登録済み）
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
        print("使い方: python3 gpt_worker.py '<task_json>'", file=sys.stderr)
        sys.exit(1)

    task = json.loads(sys.argv[1])
    print(generate_agent_prompt(task))
