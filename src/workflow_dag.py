"""Helix Corp — WorkflowDAG エンジン.

部門タスクの依存関係を解決し、並行実行可能なwave（バッチ）に分割する。
Skills (/corp-implement, /corp-review等) から呼び出される。

使い方:
    from src.workflow_dag import WorkflowDAG, DeptTask

    dag = WorkflowDAG()
    dag.add("research", "技術調査", depends_on=[])
    dag.add("design", "設計案作成", depends_on=["research"])
    dag.add("build", "実装", depends_on=["design"])
    dag.add("qa", "品質レビュー", depends_on=["design"])  # buildと並行可能

    for wave in dag.execution_waves():
        # wave内のタスクは並行実行可能
        for task in wave:
            launch_dept_agent(task)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class DeptModel(str, Enum):
    OPUS = "opus"
    SONNET = "sonnet"
    CODEX = "codex"
    HELIX = "helix"  # gemma4 via helix-agent


# 部門→デフォルトモデルのマッピング
DEPT_DEFAULT_MODEL = {
    "management": DeptModel.OPUS,
    "hr": DeptModel.SONNET,
    "research": DeptModel.SONNET,
    "design": DeptModel.SONNET,
    "build": DeptModel.CODEX,
    "qa": DeptModel.SONNET,
}

# 部門→Qdrantコレクション
DEPT_COLLECTION = {
    "management": "mem0_shared",
    "hr": "dept_hr",
    "research": "dept_research",
    "design": "dept_design",
    "build": "dept_build",
    "qa": "dept_qa",
}

# 部門→思考バイアス
DEPT_BIAS = {
    "hr": "市場価値・適合性・キャリアパスの最適化を重視。候補者/求人のマッチング精度を最大化。",
    "research": "網羅性・最新性・代替案の提示を重視。複数ソースからの裏取りを必ず行う。",
    "design": "拡張性・保守性・SOLID原則・長期的整合性を重視。過剰設計と過少設計のバランスを取る。",
    "build": "実装品質・テスト通過・DRY原則を重視。動くコードを最優先し、設計に忠実に実装する。",
    "qa": "防御的思考・最悪ケース想定を重視。OWASP Top 10、エッジケース、競合状態を常にチェック。",
}


@dataclass
class DeptTask:
    """部門タスク定義."""
    id: str                          # ユニークID (例: "research_1")
    department: str                  # 部門名 (例: "research")
    task: str                        # タスク内容
    depends_on: list[str] = field(default_factory=list)  # 依存するタスクID
    model: DeptModel | None = None   # モデル（Noneなら部門デフォルト）
    use_worktree: bool = False       # git worktree分離
    status: TaskStatus = TaskStatus.PENDING
    result: str = ""                 # 実行結果
    started_at: str = ""
    completed_at: str = ""
    duration_sec: float = 0.0

    @property
    def effective_model(self) -> DeptModel:
        return self.model or DEPT_DEFAULT_MODEL.get(self.department, DeptModel.SONNET)

    @property
    def collection(self) -> str:
        return DEPT_COLLECTION.get(self.department, "mem0_shared")

    @property
    def bias(self) -> str:
        return DEPT_BIAS.get(self.department, "")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "department": self.department,
            "task": self.task,
            "depends_on": self.depends_on,
            "model": self.effective_model.value,
            "collection": self.collection,
            "status": self.status.value,
            "result": self.result[:500],
            "duration_sec": self.duration_sec,
        }


class WorkflowDAG:
    """部門タスクDAGの構築と実行wave分割."""

    def __init__(self, name: str = ""):
        self.name = name
        self.tasks: dict[str, DeptTask] = {}
        self._counter: dict[str, int] = {}

    def add(
        self,
        department: str,
        task: str,
        depends_on: list[str] | None = None,
        model: DeptModel | None = None,
        use_worktree: bool = False,
    ) -> str:
        """タスクを追加し、IDを返す."""
        self._counter[department] = self._counter.get(department, 0) + 1
        task_id = f"{department}_{self._counter[department]}"

        dept_task = DeptTask(
            id=task_id,
            department=department,
            task=task,
            depends_on=depends_on or [],
            model=model,
            use_worktree=use_worktree,
        )
        self.tasks[task_id] = dept_task
        return task_id

    def execution_waves(self) -> list[list[DeptTask]]:
        """依存関係を解決し、並行実行可能なグループ(wave)に分割.

        Returns:
            list of waves。各waveはAgent toolで同時起動可能なタスクリスト。
        """
        remaining = set(self.tasks.keys())
        completed = set()
        waves = []

        while remaining:
            # 全依存が完了しているタスクを抽出
            ready = []
            for task_id in remaining:
                task = self.tasks[task_id]
                if all(dep in completed for dep in task.depends_on):
                    ready.append(task)

            if not ready:
                # デッドロック検出
                raise RuntimeError(
                    f"DAGデッドロック: {remaining} が解決不能。"
                    f"循環依存の可能性。"
                )

            # 同時実行上限（429エラー回避）
            MAX_PARALLEL = 3
            for i in range(0, len(ready), MAX_PARALLEL):
                batch = ready[i:i + MAX_PARALLEL]
                waves.append(batch)
                for task in batch:
                    remaining.discard(task.id)
                    completed.add(task.id)

        return waves

    def mark_completed(self, task_id: str, result: str = "") -> None:
        """タスクを完了マーク."""
        if task_id in self.tasks:
            task = self.tasks[task_id]
            task.status = TaskStatus.COMPLETED
            task.result = result
            task.completed_at = datetime.now(timezone.utc).isoformat()

    def mark_failed(self, task_id: str, error: str = "") -> None:
        """タスクを失敗マーク."""
        if task_id in self.tasks:
            task = self.tasks[task_id]
            task.status = TaskStatus.FAILED
            task.result = f"ERROR: {error}"

    def build_agent_prompt(self, task: DeptTask, context: str = "") -> str:
        """部門Agent用のプロンプトを構築."""
        dept_name = {
            "hr": "人事/採用", "research": "調査研究",
            "design": "設計", "build": "構築", "qa": "品質管理",
        }.get(task.department, task.department)

        prompt = f"""あなたはHelix Corp {dept_name}部門長です。

## 思考原則
{task.bias}

## 部門記憶
helix-agent MCP の dept_search(department="{task.collection}") で過去の知見を参照してください。
重要な発見は dept_store(department="{task.collection}") で保存してください。

## タスク
{task.task}
"""
        if context:
            prompt += f"\n## 前段タスクの結果\n{context}\n"

        prompt += """
## 出力形式
- P1(Critical): 即座に対処が必要な重大事項
- P2(Warning): 対処を推奨する事項
- P3(Info): 参考情報

簡潔に、要点のみ報告してください。"""

        return prompt

    def summary(self) -> dict:
        """DAGのサマリーを返す."""
        waves = self.execution_waves()
        return {
            "name": self.name,
            "total_tasks": len(self.tasks),
            "waves": len(waves),
            "wave_detail": [
                [{"id": t.id, "dept": t.department, "model": t.effective_model.value}
                 for t in wave]
                for wave in waves
            ],
            "tasks": {tid: t.to_dict() for tid, t in self.tasks.items()},
        }

    def to_json(self) -> str:
        return json.dumps(self.summary(), ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# プリセットDAG（Skillsから使用）
# ---------------------------------------------------------------------------

def create_review_dag(target: str) -> WorkflowDAG:
    """コードレビュー用DAG（3部門並行）."""
    dag = WorkflowDAG(name="corp-review")
    dag.add("design", f"アーキテクチャ視点でレビュー: {target}")
    dag.add("build", f"実装品質+テスト視点でレビュー: {target}", model=DeptModel.CODEX)
    dag.add("qa", f"セキュリティ+品質視点でレビュー: {target}")
    return dag


def create_implement_dag(feature: str) -> WorkflowDAG:
    """新機能実装用DAG（調査→設計→構築+品質並行）."""
    dag = WorkflowDAG(name="corp-implement")
    r = dag.add("research", f"既存実装・最新技術の調査: {feature}")
    d = dag.add("design", f"設計案作成: {feature}", depends_on=[r])
    dag.add("build", f"実装+テスト: {feature}", depends_on=[d], use_worktree=True)
    dag.add("qa", f"セキュリティ+品質レビュー: {feature}", depends_on=[d])
    return dag


def create_investigate_dag(topic: str) -> WorkflowDAG:
    """技術調査用DAG（3ソース並行）."""
    dag = WorkflowDAG(name="corp-investigate")
    dag.add("research", f"Web検索+RAGで調査: {topic}")
    dag.add("design", f"既存アーキテクチャとの関連分析: {topic}")
    dag.add("build", f"セカンドオピニオン: {topic}", model=DeptModel.CODEX)
    return dag
