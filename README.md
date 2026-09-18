# 게임 DA 실전 문제 풀이

뉴스레터 자료의 실제 사례를 문제로 풀고, 답변을 확정한 뒤 실제 결과와 비교하여 개인 SQLite DB와 기존 Notion 자료 페이지에 보존하는 개인용 웹 앱입니다.

## 주요 특성

- 실제 결과는 정적 파일이나 브라우저 JavaScript에 포함되지 않음
- 제출 시 `attempt_id`를 발급하고 SQLite에 답변 스냅샷을 불변 기록으로 저장
- 제출·Notion 저장의 중복 클릭을 idempotency key로 방지
- 비교 생성이나 Notion 저장이 실패해도 확정된 답변은 보존
- Notion 잠시 오류를 위한 지수 백오프 재시도
- Notion은 읽기 용도의 복제본이며 SQLite가 원본 저장소

## 실행

Python 3.11 이상이면 외부 패키지 설치 없이 실행할 수 있습니다.

```powershell
Copy-Item .env.example .env
# .env의 NOTION_TOKEN과 NOTION_PAGE_ID를 입력합니다.
python -m app
```

브라우저에서 <http://127.0.0.1:8000>을 엽니다. 서버는 기본으로 로컬호스트에만 바인딩됩니다.

`.env`는 편의상 앱이 직접 읽으며 Git에서 제외됩니다. 설정 값은 프로세스 환경 변수가 `.env`보다 우선합니다.

## Notion 연결

1. Notion integration을 만들고 대상 자료 페이지에 연결합니다.
2. `.env`에 `NOTION_TOKEN`과 페이지 ID인 `NOTION_PAGE_ID`를 설정합니다.
3. 기존 DB의 체크박스 속성명이 `읽음`과 다르면 `NOTION_READ_PROPERTY`를 변경합니다.

문제별 다른 Notion 페이지를 쓰려면 `data/questions.json`의 `source.notion_page_id` 값을 지정하세요. 빈 값인 경우 `NOTION_PAGE_ID`를 사용합니다.

## 문제 추가

`data/questions.json`에 문제를 추가합니다. `reveal`과 `evaluation`은 서버에서만 읽고 문제 조회 API에서는 제외됩니다. Notion 풀이 기록의 추천 판단과 관점 전환에는 `reveal.recommended_option`, `recommendation_reason`, `perspective_before`, `perspective_after`를 사용하고, 참조에는 `source.published_at`, `evidence_level`을 사용합니다. 이미 풀이한 문제를 수정할 때는 기존 버전을 바꾸지 말고 `version`을 증가시키세요. 각 시도에는 제출 당시 문제·결과·출처 스냅샷이 남습니다.

## 공통 계약

후속 UI·평가·Notion 기능은 `app/contracts.py`의 타입과 상태값을 공통 경계로 사용합니다.

- `contract_version`: HTTP 응답 형식의 버전입니다. 필드를 제거하거나 의미를 바꾸는 호환성 파괴 변경에서만 증가시킵니다.
- `question.version`: 문제 내용의 버전입니다. 시나리오, 선택지, 실제 결과 또는 평가 기준을 바꾸면 증가시킵니다.
- `attempt_id`: 한 번 확정된 풀이를 식별하며 기존 풀이를 덮어쓰는 데 사용하지 않습니다.
- `idempotency_key`: 동일한 제출이나 외부 저장 요청의 중복 실행을 막습니다.
- `AttemptStatus`와 `PersistenceStatus`: 브라우저·평가 서비스·Notion 저장기가 공유하는 상태값입니다.

공개 질문 응답에는 `reveal`과 `evaluation`이 없어야 하며, 확정된 답변 원문은 비교 결과나 외부 저장 실패와 관계없이 SQLite에 유지되어야 합니다.

## 테스트

```powershell
python -m unittest discover -s tests -v
```

## 주요 설정

| 환경 변수 | 기본값 | 설명 |
|---|---:|---|
| `APP_HOST` | `127.0.0.1` | HTTP 바인드 주소 |
| `APP_PORT` | `8000` | HTTP 포트 |
| `APP_ACCESS_TOKEN` | 빈 값 | 설정 시 API Bearer 인증 사용 |
| `APP_DB_PATH` | `data/app.db` | SQLite DB 경로 |
| `QUESTION_DATA_PATH` | `data/questions.json` | 서버 전용 문제 파일 |
| `NOTION_TOKEN` | 빈 값 | Notion integration token |
| `NOTION_PAGE_ID` | 빈 값 | 기본 저장 대상 자료 페이지 ID |
| `NOTION_READ_PROPERTY` | `읽음` | 저장 검증 후 선택할 checkbox 속성 |
| `NOTION_API_VERSION` | `2026-03-11` | 고정할 Notion API 버전 |

`APP_HOST=0.0.0.0`으로 공개할 때는 반드시 `APP_ACCESS_TOKEN`을 설정하고 HTTPS 역방향 프록시 뒤에서 실행하세요.
