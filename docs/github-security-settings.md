# GitHub Security Settings (手動設定手順)

## Settings → Code security and analysis で以下を有効化

1. **Dependency graph** — Enable
2. **Dependabot alerts** — Enable
3. **Dependabot security updates** — Enable
4. **Secret scanning** — Enable (推奨)

## Dependency Review の実効性
Dependency Review Actionは high severity 以上でCIを失敗させる設定済み。
ただし、**マージブロックとして機能させるには branch protection / rulesets で required checks 化が必要**。

## 現時点の状態
- Dependency Review Action: 設定済み (.github/workflows/dependency-review.yml)
- Branch protection: 未設定 (CI green化後に設定予定)
