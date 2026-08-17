# Recreate My Chat on Azure

The repository contains everything required to recreate the Azure resources and deploy the application code. Environment bindings and secrets are intentionally excluded.

## Resources created

Foundry resources are created by `azure.yaml` through the Microsoft Foundry azd provider:

- Resource group: `rg-my-chat-foundry-<env>-eus2`
- Foundry project: `my-chat-<env>`
- Hosted Agent: `my-chat`
- GitHub secret connection: `my-chat-runtime-secrets`

The web resources are created by `infra/web/main.bicep`:

- Resource group: `rg-my-chat-web-<env>-krc`
- Linux App Service plan: `asp-my-chat-web-<env>-krc`
- Python web app: `my-chat-web-<env>-<stable-hash>`
- System-assigned managed identity
- `Foundry User` role assignment on the project

The default plan is F1 for cost parity with the original deployment. Set `APP_SERVICE_SKU_NAME=B1` and `APP_SERVICE_SKU_TIER=Basic` for Always On and dedicated compute.

## Prerequisites

- Azure CLI and Azure Developer CLI authenticated to the target tenant
- `azure.ai.agents` azd extension
- `jq` and `zip`
- Permission to create resource groups and role assignments
- A GitHub OAuth token that can use GitHub Copilot

## Required environment variables

```bash
export AZURE_SUBSCRIPTION_ID="<subscription-guid>"
export COPILOT_GITHUB_TOKEN="<copilot-enabled-oauth-token>"
export APP_SESSION_SECRET="<at-least-32-random-characters>"
export MY_CHAT_BOOTSTRAP_PASSWORD="<temporary-password-at-least-10-characters>"
```

Optional overrides:

```bash
export MY_CHAT_ENVIRONMENT="dev"
export FOUNDRY_LOCATION="eastus2"
export WEB_LOCATION="koreacentral"
export APP_SERVICE_SKU_NAME="F1"
export APP_SERVICE_SKU_TIER="Free"
```

## Recreate and deploy

```bash
./scripts/recreate-azure.sh
```

The script:

1. Creates or selects the `my-chat-<env>` azd environment.
2. Provisions the Foundry account, project, and secret connection.
3. Deploys the Hosted Agent.
4. Deploys the App Service Bicep template and RBAC assignment.
5. Packages and ZIP-deploys the FastAPI web app.

## Restore user data

Infrastructure recreation creates a new empty `/home/data` volume. Restore these items from an operational backup after the web app exists:

- `/home/data/my-chat.db`
- `/home/data/uploads/`

Stop the web app during SQLite restore, verify `PRAGMA integrity_check`, then start it again.

## Never commit

- `.azure/` environment values
- GitHub tokens
- App session secrets
- Bootstrap passwords
- SQLite databases and uploads
