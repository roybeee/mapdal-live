# admin_v2.py 분리 계획

`admin_v2.py`는 이번 작업 전 20,523줄이었고, 전체 커밋 307개 중 216개가 이 파일을 건드렸다.
관리자 화면, 회원, CRM, CS, 상품, 재고, 드롭, SEO, 알림, 아티스트가 모두 한 파일에 들어 있어
충돌·회귀·리뷰 비용이 가장 큰 곳이다.

## 이번에 분리한 것 (2026-11-10)

| 대상 | 새 위치 | 줄 수 | 방식 |
|---|---|---|---|
| 관리자 대시보드 HTML (`ADMIN_HTML`) | `templates/admin_dashboard.html` | 2,219 | 바이트 동일 이전, `_template()` 로드 |
| 상품 등록·수정 폼 (`_PRODUCT_FORM_HTML`) | `templates/admin_product_form.html` | 480 | 동일 |
| 마이페이지 (`_MYPAGE_HTML`) | `templates/mypage.html` | 351 | 동일 |
| 독립 상품 상세 (`_PDP_HTML`) | `templates/pdp.html` | 228 | 동일 |
| 이니시스 MID/TID 판별·카드/은행 코드표·결제상세 정규화 | `inicis_meta.py` | 약 190 | 순수 함수, admin_v2 에서 같은 이름으로 re-export |
| 기동 마이그레이션 실행 | `migrations.py` | 신규 | `schema_migrations` 기록, 실패 노출 |

결과: `admin_v2.py` 20,523줄에서 약 17,100줄로 줄었다. 템플릿은 AST로 원본 문자열과 바이트 단위
동일함을 확인했다. 호출부는 바뀌지 않았다(같은 이름의 모듈 속성).

## 원칙

1. **이름을 유지한다.** 테스트와 다른 모듈이 `admin_v2.X`를 monkeypatch 하거나 AST로 함수를 뽑아
   실행한다. 옮긴 함수는 `from 새모듈 import X`로 admin_v2 에 다시 노출한다.
   단, 다른 함수가 내부에서 부르는 함수를 옮기면 `admin_v2.X` monkeypatch 가 효과를 잃는다.
   그래서 **호출되는 쪽이 아니라 호출 관계의 잎(leaf)부터** 옮긴다.
2. **한 PR 에 한 도메인만** 옮긴다. 이동과 동작 변경을 섞지 않는다.
3. AST 추출 테스트(`tests/*.cjs`, `test_inventory_admin.py`, `test_stock_baseline.py`)가 보는
   함수는 옮길 때 테스트의 소스 경로도 함께 고친다.
4. CI(pytest + node 브라우저 테스트)가 녹색일 때만 머지한다.

## 다음 후보 (우선순위 순)

라우트 분포(현재 줄 번호 기준)와 결합도를 보고 정한 순서다.

1. **나머지 대형 문자열 상수**: `DROPINPUT_SNIPPET`(231줄), `MOBNAV_SNIPPET`(228), `_FB_TOOLS_JS`(164),
   `_MP_ECOM_JS`(123). 이번과 같은 방식이라 위험이 가장 낮다. `CARD_CSS_SNIPPET`처럼 테스트가
   정규식으로 읽는 상수는 테스트 경로를 같이 바꾼다.
2. **회원 API `/api/member/*`** (48개 라우트): `member_router` 로 분리. 의존은 `member_of`·`one/run`·`audit`
   정도로 좁다.
3. **OAuth `/auth/*`** (Google·Kakao·Naver·Apple): `auth_oauth.py`. `oauth_member_finish`·`member_upsert` 를 같이 옮긴다.
4. **SEO·사이트맵·robots·OG 이미지**: 다른 기능과 거의 독립적이다.
5. **아티스트 `/api/artists`·`/artist/*`·`/admin/api/artists`** 와 `_artists_*` 마이그레이션.
6. **CS(`/admin/api/cs`)·리뷰·Q&A·1:1 문의**.
7. **주문 관리 `/admin/api/orders*`·취소 코어(`_order_cancel_core`)·가상계좌 스윕** — 결제와 엮여 있어
   가장 위험하다. 1~6 이 끝나고 테스트가 충분히 쌓인 뒤에 한다.
8. **DB 계층 통합**: `app.py` 의 `db()/Cx` 와 admin_v2 의 `rows/one/run` 이 따로 있다. 마지막에 `db.py` 하나로 합친다.

## 마이그레이션 실행기 (`migrations.py`)

- `run_once(fn)`: 일회성 정리. `(이름, 함수 소스 지문)`이 성공으로 기록돼 있으면 건너뛴다.
  함수 본문을 고치면 지문이 바뀌어 한 번 더 실행된다.
- `run_each_boot(fn)`: 배포 정적본 동기화처럼 매번 필요한 작업. 매번 실행하되 결과를 기록한다.
- 실패는 삼키지 않는다. 기동은 계속되고, 로그·`schema_migrations.status='failed'`·
  `GET /admin/api/migrations`(OWNER)로 드러난다. 실패한 일회성 작업은 다음 기동 때 다시 시도된다.
- `MIGRATIONS_FORCE=1`이면 일회성 작업도 이번 기동에 한해 모두 다시 실행한다.
- 새 데이터 마이그레이션은 `ensure_ready()`에 `try/except: pass`로 추가하지 말고 `_mr.run_once(...)`로 등록한다.
