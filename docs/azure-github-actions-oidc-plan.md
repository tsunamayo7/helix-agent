# Azure + GitHub Actions OIDC Deployment Plan

## Overview

GitHub ActionsからAzureへ、長期シークレットを保存せずにデプロイするための構成案。

## Architecture

```
GitHub Actions (CI/CD)
    │
    │ OIDC Token (id-token: write)
    ▼
Azure AD (Federated Credentials)
    │
    │ Short-lived Access Token
    ▼
Azure Container Apps / App Service
    ├── Azure Container Registry (ACR)
    └── Azure Key Vault (secrets)
```

## Security Principles

1. **No long-lived secrets in GitHub**: OIDC federation eliminates PATs and service principal secrets
2. **Least privilege**: `id-token: write` is scoped to deploy jobs only
3. **Environment protection**: Only `main` branch can deploy to production
4. **Audit trail**: All deployments tracked via GitHub Actions run logs + Azure Activity Log

## Proposed Workflow

```yaml
deploy:
  runs-on: ubuntu-latest
  permissions:
    id-token: write
    contents: read
  environment: production
  steps:
    - uses: actions/checkout@v4
    - uses: azure/login@v2
      with:
        client-id: ${{ secrets.AZURE_CLIENT_ID }}
        tenant-id: ${{ secrets.AZURE_TENANT_ID }}
        subscription-id: ${{ secrets.AZURE_SUBSCRIPTION_ID }}
    - run: az acr login --name ${{ secrets.ACR_NAME }}
    - run: |
        docker build -t ${{ secrets.ACR_LOGIN_SERVER }}/helix-agent:$GITHUB_SHA .
        docker push ${{ secrets.ACR_LOGIN_SERVER }}/helix-agent:$GITHUB_SHA
    - uses: azure/container-apps-deploy-action@v2
      with:
        imageToDeploy: ${{ secrets.ACR_LOGIN_SERVER }}/helix-agent:${{ github.sha }}
```

## Required Azure RBAC

| 操作 | 必要なロール |
|------|------------|
| ACR push | AcrPush |
| Container Apps deploy | Contributor or Container Apps Contributor |
| Key Vault secrets read | Key Vault Secrets User |

## Prerequisites (for future implementation)

- Azure subscription with Container Apps enabled
- Azure AD app registration with federated credentials
- ACR (Azure Container Registry) instance
- GitHub environment "production" with protection rules

## Alignment with APC DevOps Practice

This plan follows the GitHub + Azure integration pattern that APC implements for clients:
- Keyless authentication (OIDC) eliminates secret rotation burden
- GitHub Environments provide deployment approval gates
- Container Apps offers serverless scaling without K8s management overhead
- Full audit trail across GitHub + Azure for compliance

## Status

**構成案・今後検証予定** — 未実装。Azure環境が利用可能になった段階で検証を行う予定。
