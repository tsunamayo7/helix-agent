# DevSecOps Readiness Report — helix-agent

## このリポジトリで実装したGitHub / DevSecOps要素

| 要素 | 状態 | 詳細 |
|------|:---:|------|
| GitHub Actions CI | ✅ | Python 3.12/3.13, ruff, mypy advisory（現時点ではadvisoryとして導入。警告を確認しながら段階的にstrict化予定）, pytest+coverage |
| CodeQL | ✅ | security-extended, 週次+push/PR, v4 |
| Dependabot | ✅ | pip + GitHub Actions, 週次, labels付き |
| Dependency Review | ✅ | PR時 high severity fail。マージブロックとして機能させるにはbranch protection/rulesetsでrequired checks化が必要 |
| SECURITY.md | ✅ | 脆弱性報告プロセス, 48h応答 |
| CONTRIBUTING.md | ✅ | 開発ワークフロー, TDD推奨 |
| CODEOWNERS | ✅ | @tsunamayo7 |
| Issue Templates | ✅ | Bug Report / Feature Request |
| PR Template | ✅ | Checklist + Risk + Security Impact |
| Coverage計測 | ✅ | pytest-cov, Codecov連携 |

## 実務ではさらに必要になる要素

| 要素 | 状態 | 備考 |
|------|:---:|------|
| Branch protection / rulesets | 計画済 | CI green後に適用予定 |
| Required checks | 計画済 | CI + CodeQL + Dependency Review |
| Organization-level policy | 未経験 | Enterprise環境で学習予定 |
| GitHub Advanced Security | 部分的 | CodeQL適用済み, secret scanning未設定 |
| Secret scanning | 未設定 | GitHub UI有効化で対応可 |
| Audit log | 未経験 | Enterprise環境で学習予定 |
| Azure OIDC deployment | 構成案済 | docs/azure-github-actions-oidc-plan.md |

## 未経験領域

- GitHub Enterprise 組織管理 (seats, policies, EMUs)
- Azure 実運用 (subscriptions, RBAC, networking)
- Kubernetes 実運用 (AKS, Helm, service mesh)

## キャッチアップ計画

1. GitHub Actions OIDC + Azure Container Apps 検証 (構成案作成済み)
2. Branch protection / rulesets 検証 (計画書作成済み)
3. CodeQL / Dependabot / Dependency Review の継続運用と改善
4. AZ-900 → AZ-104 相当の学習
5. GitHub Certified (Admin or Advanced Security) 取得検討

## 面接での推奨表現

### 言ってよい
- 「GitHub Enterprise領域で重要になるDevSecOps要素を、自分の公開リポジトリに適用して学習・実践しています」
- 「GitHub Actions CI、CodeQL、Dependabot、Dependency Reviewを実際に運用し、DevSecOpsの入口を実践しています」
- 「企業環境でのGitHub Enterprise管理運用は未経験ですが、主要なDevSecOps要素を自分のプロジェクトで実装しています」
- 「Azure実務は未経験ですが、GitHub Actions OIDCによるAzure接続構成案を整理し、入社後に検証したいと考えています」

### 避けるべき
- 「GitHub Enterpriseの運用経験があります」
- 「DevSecOpsを完全に実践しています」
- 「Azureでのデプロイ経験があります」
- 「カバレッジ80%以上を維持しています」(未測定)
- 「機密情報を絶対に外部送信しません」(クラウドAI利用あり)
- 「mypyも完全に通っています」(現時点はadvisory、continue-on-error: true)

### テスト数について
- 一部資料に旧表記(347)が残っている可能性がありますが、現時点の実測ではhelix-agentは367 testsです
