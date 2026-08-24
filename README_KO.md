# My Chat

> **언어 / Language:** [English](README.md) | 한국어

고정 샘플 계정 3개(`user1`, `user2`, `user3`)를 사용하는 ChatGPT 스타일의 FastAPI 웹앱입니다. 실제 AI 추론과 모델 목록은 **GitHub Copilot SDK/CLI**에서 가져오며, Microsoft Foundry는 커스텀 Python 코드를 실행하는 **Hosted Agent 런타임**으로만 사용합니다.

> Foundry의 GPT 모델 배포는 만들지 않습니다. `azure.yaml`의 Foundry 프로젝트에는 모델 배포가 없으며, 모든 답변은 `github-copilot-sdk`를 통해 GitHub Copilot 모델이 생성합니다.

## 주요 기능

- 고정 사용자 3명과 사용자별 데이터 완전 분리
- 공용 임시 비밀번호로 최초 로그인 후 개인 비밀번호 강제 변경
- Argon2 비밀번호 해시, 로그인 잠금, 서명 세션 쿠키, CSRF 방어
- GitHub Copilot 계정에서 사용 가능한 모델을 런타임에 동적으로 조회
- GPT-5.6 Sol/Terra/Luna, Claude Opus 5/Sonnet 5/Haiku 4.5, 최신 Gemini/MAI 모델 선택
- 모델별 지원 범위에 맞춘 추론 강도 선택
- 웹 검색을 `자동`, `항상 사용`, `사용 안 함`으로 직접 선택
- 답변 토큰 실시간 스트리밍과 응답 소요시간 표시
- 사용자별 채팅 기록, 제목 자동 생성, 개별/전체 삭제
- 사용자별 개인 메모리 저장·수정·삭제 및 답변 문맥 반영
- 이미지, PDF, Office 문서, TXT/Markdown/CSV/JSON 파일 첨부
- 웹 검색과 첨부 자료를 바탕으로 `.pptx` 슬라이드 생성 및 다운로드
- 랩탑 우선 ChatGPT 스타일 UI와 모바일 대응 레이아웃
- Copilot 연결/로딩/오류 상태, 모델 재시도, 마지막 대화 자동 복원
- 로컬은 SQLite WAL, App Service는 네트워크 스토리지에 안전한 rollback journal 기반 영속 저장

## 구조

```text
Browser
  -> FastAPI web app (login, history, memory, SQLite)
  -> Microsoft Foundry Hosted Agent (Responses protocol)
  -> GitHub Copilot Python SDK (model discovery)
  -> GitHub Copilot CLI runtime (selective web_search + attachments)
  -> Copilot-provided GPT / Claude / Gemini / MAI models
```

| 경로 | 역할 |
|---|---|
| `src/my_copilot_agent/` | Foundry Hosted Agent와 Copilot SDK 어댑터 |
| `src/my_chat_web/` | FastAPI 웹앱, 인증, SQLite, UI |
| `tests/` | 인증·격리·CRUD·프로토콜 테스트 |
| `azure.yaml` | 모델 배포 없는 Foundry Hosted Agent 정의 |

## GitHub Copilot 인증

로컬에서는 기존 Copilot CLI 로그인을 사용할 수 있습니다. 배포 환경은 헤드리스이므로 `COPILOT_GITHUB_TOKEN`이 필요합니다. GitHub CLI OAuth 토큰, Copilot Requests 권한이 있는 fine-grained PAT, 또는 Copilot CLI OAuth 토큰을 사용할 수 있습니다. Classic `ghp_` PAT는 지원되지 않습니다.

현재 프로젝트는 다음처럼 특정 GitHub 계정의 OAuth 토큰을 azd 환경에 설정합니다.

```bash
azd env set COPILOT_GITHUB_TOKEN "$(gh auth token --user <github-user>)"
```

`azd provision`은 이 값을 Foundry의 `my-chat-runtime-secrets` CustomKeys 연결에 쓰기 전용 비밀로 저장합니다. Agent 버전에는 실제 값 대신 `${{connections.my-chat-runtime-secrets.credentials.github_token}}` 참조만 남습니다. 토큰을 소스, `.env.example`, 커밋 또는 로그에 넣지 마세요. `.azure/`와 `.env`는 Git에서 제외됩니다.

## 로컬 실행

### 1. Hosted Agent

```bash
cd src/my_copilot_agent
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cd ../..

AZURE_DEV_USER_AGENT=microsoft_foundry_skill \
  azd ai agent run my-chat --no-client
```

다른 터미널에서:

```bash
AZURE_DEV_USER_AGENT=microsoft_foundry_skill \
  azd ai agent invoke my-chat --local "안녕하세요"
```

### 2. 웹앱

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r src/my_chat_web/requirements-dev.txt

cd src/my_chat_web
export APP_SESSION_SECRET="<32자 이상의 임의 문자열>"
export MY_CHAT_BOOTSTRAP_PASSWORD="<최초 로그인용 임시 비밀번호>"
export FOUNDRY_AGENT_ENDPOINT="http://localhost:8088/responses"
../../.venv/bin/uvicorn main:app --reload
```

`http://localhost:8000`에서 로그인합니다. 최초 로그인 시 모든 사용자가 임시 비밀번호를 사용하고, 즉시 본인 비밀번호로 변경합니다.

## 테스트

```bash
.venv/bin/python -m pytest
.venv/bin/python -m compileall -q src tests
```

## Foundry 배포

최초 배포 시 Sweden Central 환경을 한 번 생성합니다.

```bash
AZURE_DEV_USER_AGENT=microsoft_foundry_skill \
  azd env new my-chat-dev --location swedencentral --no-prompt
```

이후 프로젝트 루트에서 배포 명령을 실행합니다.

```bash
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd provision --no-prompt
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd deploy --no-prompt
AZURE_DEV_USER_AGENT=microsoft_foundry_skill azd ai agent show --output json
AZURE_DEV_USER_AGENT=microsoft_foundry_skill \
  azd ai agent invoke my-chat "안녕하세요"
```

`azd deploy`는 `codeConfiguration`을 사용한 직접 코드 배포입니다. Docker/ACR이나 Foundry 모델 배포가 필요하지 않습니다.
새 환경과 재생성 워크플로의 기본 리전은 Sweden Central입니다. 이 기본값을 바꿔도 기존 Azure 리소스의 리전은 이동하지 않습니다.

## Azure 전체 재생성

Foundry, App Service, 관리 ID, RBAC과 앱 설정을 `my-chat` 이름으로 다시 만드는 절차는 [Azure 재생성 가이드](docs/azure-recreation_KO.md)를 따릅니다.

```bash
./scripts/recreate-azure.sh
```

재생성 스크립트는 로컬 `data/`, 업로드, SQLite 파일, `.env`, 로그를 배포 ZIP에서 제외합니다. Hosted Agent 호출과 웹앱 `/healthz` 확인까지 성공해야 배포를 완료한 것으로 처리합니다.

웹 UI는 별도의 Azure App Service에 배포하고 다음 앱 설정을 지정합니다.

| 설정 | 값 |
|---|---|
| `APP_ENV` | `production` |
| `APP_SESSION_SECRET` | 32자 이상의 랜덤 비밀 |
| `MY_CHAT_BOOTSTRAP_PASSWORD` | 최초 로그인용 임시 비밀번호 |
| `MY_CHAT_DATABASE_PATH` | `/home/data/my-chat.db` |
| `MY_CHAT_UPLOAD_DIR` | `/home/data/uploads` |
| `FOUNDRY_AGENT_ENDPOINT` | 배포된 Hosted Agent Responses 엔드포인트 |
| `FOUNDRY_TOKEN_SCOPE` | `https://ai.azure.com/.default` |
| `ALLOWED_HOSTS` | 웹앱 호스트 이름 |
| `WEBSITES_ENABLE_APP_SERVICE_STORAGE` | `true` |
| `FORWARDED_ALLOW_IPS` | `*` |

웹앱의 시스템 할당 관리 ID에 Foundry 프로젝트 범위의 `Foundry User` 역할을 부여합니다. 프로덕션에서는 `DefaultAzureCredential` 대신 `ManagedIdentityCredential`만 사용합니다.

FastAPI 시작 명령은 App Service의 HTTPS 프록시 헤더를 처리해야 합니다.

```bash
python -m uvicorn main:app --host 0.0.0.0 --proxy-headers
```

`FORWARDED_ALLOW_IPS=*` 앱 설정과 함께 사용합니다. 정적 자산은 같은 오리진의 상대 경로를 사용하므로 프록시 설정이 잘못돼도 CSP가 CSS/JavaScript를 차단하지 않습니다.

## 운영 참고

- 모델 가용성은 Copilot 요금제와 조직 정책에 따라 달라집니다. 정상 연결에서는 `CopilotClient.list_models()`의 실제 결과를 사용합니다. 조회 실패 시 UI는 구성된 fallback 목록과 경고를 표시하지만, 실제 사용 가능 여부는 보장하지 않습니다.
- `자동`은 모델이 웹 검색 필요성을 판단하고, `항상 사용`은 빠른 검색 모델로 웹 도구를 반드시 실행하며, `사용 안 함`은 웹 도구를 세션에서 제외합니다.
- 웹앱은 사용자별 Foundry 세션을 재사용합니다. 사용자 간 세션은 공유하지 않으며, 같은 사용자의 후속 요청은 기존 세션을 사용해 연결 초기화 지연을 반복하지 않습니다.
- 지원 첨부 형식은 TXT, Markdown, CSV, JSON, PDF, PNG/JPG/GIF/WebP, DOCX, XLSX, PPTX입니다. 파일당 8MB, 요청당 5개/총 16MB로 제한합니다.
- TXT/Markdown/CSV/JSON은 인용 컨텍스트로 전달하고, 이미지·PDF·Office 문서는 Copilot 네이티브 첨부로 전달합니다.
- 생성된 PPTX와 업로드 파일은 `/home/data/uploads` 아래 사용자·대화별 무작위 저장명으로 보관하며, 소유 사용자만 다운로드할 수 있습니다.
- 긴 답변 원문은 그대로 저장하며, 다음 요청의 대화 문맥으로 재사용할 때만 메시지당 20,000자로 축약해 프로토콜 제한과 응답시간을 지킵니다.
- SQLite 파일은 단일 저사용 웹 인스턴스를 전제로 합니다. 인스턴스를 여러 개로 확장할 경우 Azure SQL 또는 PostgreSQL로 이전하세요.
- 초기 비밀번호를 변경하면 기존 사용자 비밀번호는 재설정되지 않습니다.
- 사용자 메모리와 대화 기록은 해당 사용자 ID로 항상 필터링됩니다.
- Hosted Agent는 파일·셸 도구를 제공하지 않으며, 사용자가 선택한 경우에만 제한된 `web_search`/`web_fetch` 도구를 사용할 수 있습니다.
