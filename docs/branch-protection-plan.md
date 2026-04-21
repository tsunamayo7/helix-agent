# Branch Protection 設定計画

## 推奨設定 (CI green確認後に適用)

### master branch
- Require a pull request before merging: **Yes**
  - Required approving reviews: 0 (1人リポジトリのため)
- Require status checks to pass before merging: **Yes**
  - Required checks:
    - CI / test (3.12)
    - CI / test (3.13)
    - CodeQL / analyze
- Require branches to be up to date before merging: Yes
- Do not allow bypassing: No (管理者は直接push可)

### 設定コマンド (gh CLI)
```bash
gh api repos/tsunamayo7/helix-agent/branches/master/protection -X PUT \
  --input - <<'EOF'
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["test (3.12)", "test (3.13)", "analyze"]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": null,
  "restrictions": null
}
EOF
```

### 注意
- CI / CodeQL が安定して green になってから適用すること
- 1人リポジトリのためレビュー必須は運用負荷を考慮し省略
- 実際に有効化する前にリポジトリオーナーに確認
