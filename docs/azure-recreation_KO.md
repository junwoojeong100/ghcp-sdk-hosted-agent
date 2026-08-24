# Azure에서 My Chat 재생성

> **언어 / Language:** [English](azure-recreation.md) | 한국어

이 저장소에는 Azure 리소스를 다시 만들고 애플리케이션 코드를 배포하는 데 필요한 모든
항목이 포함되어 있습니다. 환경 바인딩과 비밀 값은 의도적으로 제외합니다.

## 생성되는 리소스

Foundry 리소스는 Microsoft Foundry azd provider를 통해 `azure.yaml`에서 생성합니다.

- 리소스 그룹: `rg-my-chat-foundry-<env>-swc`
- Foundry 프로젝트: `my-chat-<env>`
- Hosted Agent: `my-chat`
- GitHub 비밀 연결: `my-chat-runtime-secrets`

웹 리소스는 `infra/web/main.bicep`에서 생성합니다.

- 리소스 그룹: `rg-my-chat-web-<env>-swc`
- Linux App Service 플랜: `asp-my-chat-web-<env>-swc`
- Python 웹앱: `my-chat-web-<env>-<stable-hash>`
- 시스템 할당 관리 ID
- 프로젝트 범위의 `Foundry User` 역할 할당

기본 플랜은 기존 배포와 비용 수준을 맞추기 위해 F1을 사용합니다. Always On과 전용
컴퓨팅을 사용하려면 `APP_SERVICE_SKU_NAME=B1`과 `APP_SERVICE_SKU_TIER=Basic`을
설정합니다.

## 사전 요구 사항

- 대상 tenant에 인증된 Azure CLI와 Azure Developer CLI
- `azure.ai.agents` azd extension
- `curl`과 `jq`
- 리소스 그룹과 역할 할당을 생성할 권한
- GitHub Copilot을 사용할 수 있는 GitHub OAuth 토큰

## 필수 환경 변수

```bash
export AZURE_SUBSCRIPTION_ID="<subscription-guid>"
export COPILOT_GITHUB_TOKEN="<copilot-enabled-oauth-token>"
export APP_SESSION_SECRET="<at-least-32-random-characters>"
export MY_CHAT_BOOTSTRAP_PASSWORD="<temporary-password-at-least-10-characters>"
```

`MY_CHAT_BOOTSTRAP_PASSWORD`는 `user1`, `user2`, `user3`이 함께 사용하는
최초 로그인용 비밀번호입니다. 모든 계정은 로그인 직후 새 비밀번호로
변경해야 합니다. 실습용 값은 배포 셸에서 설정해 참가자에게 별도로
전달하고, 공개 저장소에는 실제 사용 중인 값을 커밋하지 않습니다.

선택적으로 다음 값을 재정의할 수 있습니다.

```bash
export MY_CHAT_ENVIRONMENT="dev"
export FOUNDRY_LOCATION="swedencentral"
export WEB_LOCATION="swedencentral"
export APP_SERVICE_SKU_NAME="F1"
export APP_SERVICE_SKU_TIER="Free"
```

Foundry와 App Service의 기본 리전은 모두 Sweden Central입니다. 이 값은 리소스를 새로 만들 때만 적용되며 기존 배포의 리전을 이동하지 않습니다.

## 재생성 및 배포

```bash
./scripts/recreate-azure.sh
```

스크립트는 다음 작업을 수행합니다.

1. `my-chat-<env>` azd 환경을 생성하거나 선택합니다.
2. Foundry account, project와 비밀 연결을 프로비저닝합니다.
3. Hosted Agent를 배포합니다.
4. Hosted Agent를 호출해 smoke test를 수행합니다.
5. App Service Bicep template과 RBAC 역할 할당을 배포합니다.
6. 로컬 database, upload, secret, cache와 log를 제외한 정리된 ZIP을 만듭니다.
7. FastAPI 웹앱을 ZIP 배포하고 `/healthz`가 성공할 때까지 기다립니다.

## 사용자 데이터 복원

인프라를 재생성하면 비어 있는 새 `/home/data` volume이 만들어집니다. 웹앱이 생성된
후 운영 backup에서 다음 항목을 복원합니다.

- `/home/data/my-chat.db`
- `/home/data/uploads/`

SQLite를 복원하는 동안 웹앱을 중지하고 `PRAGMA integrity_check`를 확인한 다음 웹앱을
다시 시작합니다.

## 커밋하면 안 되는 항목

- `.azure/` 환경 값
- GitHub 토큰
- App session secret
- Bootstrap password
- SQLite database와 upload
