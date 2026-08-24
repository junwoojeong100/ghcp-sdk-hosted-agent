#!/usr/bin/env bash

set -euo pipefail

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Required command not found: $1" >&2
    exit 1
  fi
}

require_value() {
  if [[ -z "${!1:-}" ]]; then
    echo "Required environment variable is not set: $1" >&2
    exit 1
  fi
}

azd_value() {
  AZURE_DEV_USER_AGENT=microsoft_foundry_skill \
    azd env get-value "$1" | tr -d '"'
}

require_command az
require_command azd
require_command curl
require_command jq
require_command python3

require_value COPILOT_GITHUB_TOKEN
require_value APP_SESSION_SECRET
require_value MY_CHAT_BOOTSTRAP_PASSWORD

if (( ${#APP_SESSION_SECRET} < 32 )); then
  echo "APP_SESSION_SECRET must be at least 32 characters." >&2
  exit 1
fi
if (( ${#MY_CHAT_BOOTSTRAP_PASSWORD} < 10 )); then
  echo "MY_CHAT_BOOTSTRAP_PASSWORD must be at least 10 characters." >&2
  exit 1
fi

AZURE_SUBSCRIPTION_ID="${AZURE_SUBSCRIPTION_ID:-$(az account show --query id -o tsv)}"
MY_CHAT_ENVIRONMENT="${MY_CHAT_ENVIRONMENT:-dev}"
AZD_ENV_NAME="${AZD_ENV_NAME:-my-chat-${MY_CHAT_ENVIRONMENT}}"
FOUNDRY_LOCATION="${FOUNDRY_LOCATION:-swedencentral}"
WEB_LOCATION="${WEB_LOCATION:-swedencentral}"
FOUNDRY_RESOURCE_GROUP="${FOUNDRY_RESOURCE_GROUP:-rg-my-chat-foundry-${MY_CHAT_ENVIRONMENT}-swc}"
FOUNDRY_PROJECT_NAME="${FOUNDRY_PROJECT_NAME:-my-chat-${MY_CHAT_ENVIRONMENT}}"
APP_SERVICE_SKU_NAME="${APP_SERVICE_SKU_NAME:-F1}"
APP_SERVICE_SKU_TIER="${APP_SERVICE_SKU_TIER:-Free}"

az account set --subscription "$AZURE_SUBSCRIPTION_ID"

if ! AZURE_DEV_USER_AGENT=microsoft_foundry_skill \
  azd env select "$AZD_ENV_NAME" >/dev/null 2>&1; then
  AZURE_DEV_USER_AGENT=microsoft_foundry_skill \
    azd env new "$AZD_ENV_NAME" \
      --subscription "$AZURE_SUBSCRIPTION_ID" \
      --location "$FOUNDRY_LOCATION" \
      --no-prompt
fi

AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env set \
  AZURE_SUBSCRIPTION_ID "$AZURE_SUBSCRIPTION_ID"
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env set \
  AZURE_LOCATION "$FOUNDRY_LOCATION"
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env set \
  AZURE_RESOURCE_GROUP "$FOUNDRY_RESOURCE_GROUP"
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env set \
  AZURE_FOUNDRY_RESOURCE_GROUP "$FOUNDRY_RESOURCE_GROUP"
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env set \
  AZURE_AI_PROJECT_NAME "$FOUNDRY_PROJECT_NAME"
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd env set \
  COPILOT_GITHUB_TOKEN "$COPILOT_GITHUB_TOKEN"

AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd provision --no-prompt
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd deploy my-chat --no-prompt

agent_smoke_succeeded=false
for attempt in 1 2 3; do
  if AZURE_DEV_USER_AGENT=microsoft_foundry_skill \
    azd ai agent invoke my-chat \
      "Reply with exactly: OK" >/dev/null; then
    agent_smoke_succeeded=true
    break
  fi
  sleep $((attempt * 10))
done
if [[ "$agent_smoke_succeeded" != "true" ]]; then
  echo "Hosted Agent smoke test failed." >&2
  exit 1
fi

FOUNDRY_ACCOUNT_NAME="$(azd_value AZURE_AI_ACCOUNT_NAME)"
FOUNDRY_PROJECT_NAME="$(azd_value AZURE_AI_PROJECT_NAME)"
FOUNDRY_RESOURCE_GROUP="$(azd_value AZURE_RESOURCE_GROUP)"
AGENT_ENDPOINT="$(azd_value AGENT_MY_CHAT_RESPONSES_ENDPOINT)"

DEPLOYMENT_NAME="my-chat-web-${MY_CHAT_ENVIRONMENT}"
TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/my-chat-recreate.XXXXXX")"
DEPLOYMENT_RESULT="$TEMP_DIR/deployment.json"
PARAMETERS_PATH="$TEMP_DIR/main.parameters.json"
PACKAGE_PATH="$TEMP_DIR/my-chat-web.zip"

cleanup() {
  rm -f "$DEPLOYMENT_RESULT" "$PARAMETERS_PATH" "$PACKAGE_PATH"
  rmdir "$TEMP_DIR" 2>/dev/null || true
}
trap cleanup EXIT

export MY_CHAT_BICEP_ENVIRONMENT="$MY_CHAT_ENVIRONMENT"
export MY_CHAT_BICEP_WEB_LOCATION="$WEB_LOCATION"
export MY_CHAT_BICEP_FOUNDRY_RESOURCE_GROUP="$FOUNDRY_RESOURCE_GROUP"
export MY_CHAT_BICEP_FOUNDRY_ACCOUNT="$FOUNDRY_ACCOUNT_NAME"
export MY_CHAT_BICEP_FOUNDRY_PROJECT="$FOUNDRY_PROJECT_NAME"
export MY_CHAT_BICEP_AGENT_ENDPOINT="$AGENT_ENDPOINT"
export MY_CHAT_BICEP_SKU_NAME="$APP_SERVICE_SKU_NAME"
export MY_CHAT_BICEP_SKU_TIER="$APP_SERVICE_SKU_TIER"

python3 - "$PARAMETERS_PATH" <<'PY'
import json
import os
import sys

output_path = sys.argv[1]
values = {
    "environmentName": os.environ["MY_CHAT_BICEP_ENVIRONMENT"],
    "webLocation": os.environ["MY_CHAT_BICEP_WEB_LOCATION"],
    "foundryResourceGroupName": os.environ[
        "MY_CHAT_BICEP_FOUNDRY_RESOURCE_GROUP"
    ],
    "foundryAccountName": os.environ["MY_CHAT_BICEP_FOUNDRY_ACCOUNT"],
    "foundryProjectName": os.environ["MY_CHAT_BICEP_FOUNDRY_PROJECT"],
    "foundryAgentEndpoint": os.environ["MY_CHAT_BICEP_AGENT_ENDPOINT"],
    "appSessionSecret": os.environ["APP_SESSION_SECRET"],
    "bootstrapPassword": os.environ["MY_CHAT_BOOTSTRAP_PASSWORD"],
    "appServiceSkuName": os.environ["MY_CHAT_BICEP_SKU_NAME"],
    "appServiceSkuTier": os.environ["MY_CHAT_BICEP_SKU_TIER"],
}
payload = {
    "$schema": (
        "https://schema.management.azure.com/schemas/"
        "2019-04-01/deploymentParameters.json#"
    ),
    "contentVersion": "1.0.0.0",
    "parameters": {name: {"value": value} for name, value in values.items()},
}
with open(output_path, "w", encoding="utf-8") as output:
    json.dump(payload, output)
PY
chmod 600 "$PARAMETERS_PATH"

az deployment sub create \
  --name "$DEPLOYMENT_NAME" \
  --location "$WEB_LOCATION" \
  --template-file infra/web/main.bicep \
  --parameters "@$PARAMETERS_PATH" \
  --only-show-errors \
  --output json > "$DEPLOYMENT_RESULT"

WEB_RESOURCE_GROUP="$(jq -r '.properties.outputs.webResourceGroupName.value' "$DEPLOYMENT_RESULT")"
WEB_APP_NAME="$(jq -r '.properties.outputs.webAppName.value' "$DEPLOYMENT_RESULT")"
WEB_APP_URL="$(jq -r '.properties.outputs.webAppUrl.value' "$DEPLOYMENT_RESULT")"

python3 scripts/package_web.py src/my_chat_web "$PACKAGE_PATH"

az webapp deploy \
  --name "$WEB_APP_NAME" \
  --resource-group "$WEB_RESOURCE_GROUP" \
  --subscription "$AZURE_SUBSCRIPTION_ID" \
  --src-path "$PACKAGE_PATH" \
  --type zip \
  --clean true \
  --restart true \
  --track-status true \
  --only-show-errors \
  --output none

web_health_succeeded=false
for attempt in {1..12}; do
  if curl --fail --silent --show-error \
    --max-time 15 "$WEB_APP_URL/healthz" >/dev/null; then
    web_health_succeeded=true
    break
  fi
  sleep 10
done
if [[ "$web_health_succeeded" != "true" ]]; then
  echo "Web app health check failed: $WEB_APP_URL/healthz" >&2
  exit 1
fi

echo "Foundry project: $(azd_value FOUNDRY_PROJECT_ENDPOINT)"
echo "Hosted Agent: $AGENT_ENDPOINT"
echo "Web app: $WEB_APP_URL"
