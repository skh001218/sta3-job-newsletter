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

`data/questions.json`에 문제를 추가합니다. `reveal`과 `evaluation`은 서버에서만 읽고 문제 조회 API에서는 제외됩니다. 이미 풀이한 문제를 수정할 때는 기존 버전을 바꾸지 말고 `version`을 증가시키세요. 각 시도에는 제출 당시 문제·결과·출처 스냅샷이 남습니다.

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
