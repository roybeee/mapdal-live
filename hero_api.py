# -*- coding: utf-8 -*-
"""
hero_api.py — 홈 히어로 슬라이드 API (mapdal-live)

붙이는 법 (main 앱 파일에 두 줄):
    from hero_api import router as hero_router
    app.include_router(hero_router)

엔드포인트:
    GET  /api/hero          공개. {"slides":[...], "interval_ms":3000}
    PUT  /api/hero          관리자. 헤더 X-Admin-Token: <ADMIN_TOKEN> 필요.

저장소:
    DATABASE_URL 이 있으면 Postgres 테이블 site_settings(key,value jsonb)에 저장
    (없거나 드라이버 미설치 시 data/hero.json 파일 폴백 — Render는 재배포 시
     파일이 초기화되므로 운영에서는 DB 저장이 기본).

인증:
    기존 관리자 세션 미들웨어가 있다면 require_admin 함수만 그걸로 교체하면 됨.
"""
import hmac
import json
import os
import time
from typing import List, Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
HERO_FILE = os.environ.get("HERO_FILE", os.path.join("data", "hero.json"))
SETTINGS_KEY = "hero_slides"

# ----------------------------------------------------------------------
# 기본 슬라이드 (관리자 저장 전 초기값) — 슬라이드 1은 기존 히어로 그대로
# ----------------------------------------------------------------------
DEFAULT_DATA = {
    "interval_ms": 3000,
    "slides": [
        {
            "img": "", "href": "",
            "eyebrow": "NOT A STORE, A STAGE · SEONGSU, SEOUL",
            "title": "SHOP SEONGSU,", "title2": "FROM ANYWHERE",
            "sub": "성수동 연무장길 825평 — 몰입부터 구매까지, 무대에서 벌어진 팬 경험이 매주 새로운 드롭으로 도착합니다. 현장의 열기를 그대로 배송합니다.",
            "cta_label": "SUMMER DROP 07 보기", "cta_href": "new-drops.html",
            "cta2_label": "공간 둘러보기", "cta2_href": "#space",
            "chip": "NEXT DROP|07.21 (TUE) 12:00 KST|응모 마감 07.29 (WED)",
            "active": True,
        },
        {
            "img": "/hero/hero-slide-2-vernon-the8.jpg",
            "href": "/kpop", "eyebrow": "", "title": "", "title2": "", "sub": "",
            "cta_label": "", "cta_href": "", "cta2_label": "", "cta2_href": "",
            "chip": "", "active": True,
        },
        {
            "img": "/hero/hero-slide-3-girlgroup.jpg",
            "href": "/kpop", "eyebrow": "", "title": "", "title2": "", "sub": "",
            "cta_label": "", "cta_href": "", "cta2_label": "", "cta2_href": "",
            "chip": "", "active": True,
        },
        {
            "img": "/hero/hero-slide-4-icecream.jpg",
            "href": "/kpop", "eyebrow": "", "title": "", "title2": "", "sub": "",
            "cta_label": "", "cta_href": "", "cta2_label": "", "cta2_href": "",
            "chip": "", "active": True,
        },
        {
            "img": "", "href": "new-drops.html",
            "eyebrow": "DROP 08 · TEASER",
            "title": "NEXT STAGE,", "title2": "COMING SOON",
            "sub": "다음 드롭 라인업이 곧 공개됩니다. 응모 일정은 드롭 캘린더에서 확인하세요.",
            "cta_label": "드롭 일정 보기", "cta_href": "new-drops.html",
            "cta2_label": "", "cta2_href": "", "chip": "", "active": True,
        },
    ],
}


# ----------------------------------------------------------------------
# 스키마 (검증)
# ----------------------------------------------------------------------
class Slide(BaseModel):
    img: str = Field("", max_length=600)
    href: str = Field("", max_length=600)
    eyebrow: str = Field("", max_length=120)
    title: str = Field("", max_length=120)
    title2: str = Field("", max_length=120)
    sub: str = Field("", max_length=400)
    cta_label: str = Field("", max_length=60)
    cta_href: str = Field("", max_length=600)
    cta2_label: str = Field("", max_length=60)
    cta2_href: str = Field("", max_length=600)
    chip: str = Field("", max_length=200)
    active: bool = True


class HeroData(BaseModel):
    interval_ms: int = Field(3000, ge=1500, le=15000)
    slides: List[Slide] = Field(..., min_length=1, max_length=10)


# ----------------------------------------------------------------------
# 저장소: Postgres(jsonb) → 파일 폴백
# ----------------------------------------------------------------------
def _pg_driver():
    try:
        import psycopg  # v3
        return "psycopg", psycopg
    except ImportError:
        pass
    try:
        import psycopg2
        return "psycopg2", psycopg2
    except ImportError:
        return None, None


_DRIVER_NAME, _DRIVER = _pg_driver()
USE_DB = bool(DATABASE_URL and _DRIVER)


def _dsn() -> str:
    dsn = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    if "sslmode=" not in dsn and "localhost" not in dsn and "127.0.0.1" not in dsn:
        dsn += ("&" if "?" in dsn else "?") + "sslmode=require"
    return dsn


def _db_get() -> Optional[dict]:
    with _DRIVER.connect(_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS site_settings("
                "key TEXT PRIMARY KEY, value JSONB NOT NULL,"
                "updated_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            cur.execute("SELECT value FROM site_settings WHERE key=%s",
                        (SETTINGS_KEY,))
            row = cur.fetchone()
        conn.commit()
    if not row:
        return None
    val = row[0]
    return val if isinstance(val, dict) else json.loads(val)


def _db_set(data: dict) -> None:
    with _DRIVER.connect(_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS site_settings("
                "key TEXT PRIMARY KEY, value JSONB NOT NULL,"
                "updated_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            cur.execute(
                "INSERT INTO site_settings(key,value) VALUES(%s,%s) "
                "ON CONFLICT (key) DO UPDATE "
                "SET value=EXCLUDED.value, updated_at=now()",
                (SETTINGS_KEY, json.dumps(data, ensure_ascii=False)),
            )
        conn.commit()


def _file_get() -> Optional[dict]:
    if not os.path.exists(HERO_FILE):
        return None
    with open(HERO_FILE, encoding="utf-8") as f:
        return json.load(f)


def _file_set(data: dict) -> None:
    os.makedirs(os.path.dirname(HERO_FILE) or ".", exist_ok=True)
    tmp = HERO_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, HERO_FILE)


def load_data() -> dict:
    try:
        data = _db_get() if USE_DB else _file_get()
    except Exception as e:  # DB 일시 장애 시에도 히어로는 떠야 함
        print(f"[hero_api] load error ({e}) — 기본값 사용")
        data = None
    return data or DEFAULT_DATA


def save_data(data: dict) -> None:
    if USE_DB:
        _db_set(data)
    else:
        _file_set(data)


# 5초 인메모리 캐시 (GET 트래픽이 DB를 두드리지 않게)
_cache = {"t": 0.0, "data": None}


def cached_load() -> dict:
    now = time.time()
    if _cache["data"] is None or now - _cache["t"] > 5:
        _cache["data"] = load_data()
        _cache["t"] = now
    return _cache["data"]


# ----------------------------------------------------------------------
# 인증 — 기존 관리자 세션이 있으면 이 함수만 교체
# ----------------------------------------------------------------------
def require_admin(x_admin_token: str = Header(default="")):
    if not ADMIN_TOKEN:
        raise HTTPException(503, "서버에 ADMIN_TOKEN 이 설정되지 않았습니다.")
    if not hmac.compare_digest(x_admin_token, ADMIN_TOKEN):
        raise HTTPException(401, "관리자 토큰이 올바르지 않습니다.")


# ----------------------------------------------------------------------
# 라우트
# ----------------------------------------------------------------------
@router.get("/api/hero")
def get_hero():
    return cached_load()


@router.put("/api/hero")
def put_hero(payload: HeroData,
             x_admin_token: str = Header(default="")):
    require_admin(x_admin_token)
    data = payload.model_dump()
    save_data(data)
    _cache["data"], _cache["t"] = data, time.time()
    return {"ok": True, "slides": len(data["slides"]),
            "storage": "postgres" if USE_DB else "file"}
