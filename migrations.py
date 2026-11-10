"""migrations.py — 기동 시 데이터 마이그레이션 실행기 (2026-11-10)

종전 admin_v2.ensure_ready() 는 부팅할 때마다 20여 개 '멱등 마이그레이션'을 전부 다시 돌리고,
실패는 `except Exception: pass` 로 삼켰다. 그래서
  · 이미 끝난 일회성 정리(표기 치환·사고 복구 등)가 매 배포마다 DB 편집본을 다시 훑고,
  · 실패해도 아무 흔적이 남지 않았다.

이 모듈은 두 가지 실행 방식을 제공한다.
  run_once(fn)       일회성 마이그레이션. schema_migrations 에 (이름, 코드 지문)을 기록하고
                     같은 코드가 이미 성공했으면 건너뛴다. 함수 본문을 고치면 지문이 바뀌어 한 번 더 돈다.
  run_each_boot(fn)  매 기동 동기화(배포 정적본 ↔ DB 편집본 등). 매번 돌리되 실패를 기록·출력한다.

실패는 삼키지 않는다 — 기동은 계속하되(종전과 같은 가용성) 로그와 schema_migrations.status='failed',
관리자 API(/admin/api/migrations)로 드러낸다. 실패한 일회성 마이그레이션은 다음 기동에 다시 시도된다.

MIGRATIONS_FORCE=1 이면 일회성 마이그레이션도 이번 기동에 한해 모두 다시 실행한다.
"""
import hashlib
import inspect
import os
import time
import traceback

DDL = ('CREATE TABLE IF NOT EXISTS schema_migrations('
       'name TEXT PRIMARY KEY, version TEXT, applied_at TEXT, status TEXT, duration_ms INTEGER, detail TEXT)')


def fingerprint(fn):
    """함수 소스 기준 지문 — 본문이 바뀌면 달라진다."""
    try:
        src = inspect.getsource(fn)
    except (OSError, TypeError):
        src = repr(getattr(fn, '__code__', fn).co_code) if hasattr(fn, '__code__') else repr(fn)
    return hashlib.sha1(src.encode('utf-8')).hexdigest()[:12]


def _force():
    return str(os.getenv('MIGRATIONS_FORCE', '')).strip().lower() in ('1', 'true', 'yes', 'on')


class Runner:
    """db: run(sql, params) · one(sql, params) 를 가진 객체(admin_v2 모듈). now: 시각 문자열 함수."""

    def __init__(self, db, now, log=print):
        self.db, self.now, self.log = db, now, log
        self.results = []
        self._table_ok = False

    def _ensure(self):
        if self._table_ok:
            return True
        try:
            self.db.run(DDL)
            self._table_ok = True
        except Exception as e:
            self.log('[migrations] schema_migrations 생성 실패 — 기록 없이 실행: %s' % e)
        return self._table_ok

    def _record(self, name, version, status, ms, detail=''):
        if not self._table_ok:
            return
        args = (version, self.now(), status, int(ms), str(detail)[:500], name)
        try:
            if self.db.one('SELECT name FROM schema_migrations WHERE name=?', (name,)):
                self.db.run('UPDATE schema_migrations SET version=?, applied_at=?, status=?, duration_ms=?, detail=? '
                            'WHERE name=?', args)
            else:
                self.db.run('INSERT INTO schema_migrations(version, applied_at, status, duration_ms, detail, name) '
                            'VALUES(?,?,?,?,?,?)', args)
        except Exception as e:
            self.log('[migrations] 기록 실패 %s: %s' % (name, e))

    def _exec(self, fn, name, version, mode):
        t0 = time.time()
        try:
            fn()
        except Exception as e:
            ms = (time.time() - t0) * 1000
            tb = traceback.format_exc(limit=3)
            self.log('[migrations] FAILED %s (%s): %s\n%s' % (name, mode, e, tb))
            self._record(name, version, 'failed', ms, '%s: %s' % (type(e).__name__, e))
            self.results.append((name, 'failed'))
            return 'failed'
        ms = (time.time() - t0) * 1000
        self._record(name, version, 'ok' if mode == 'once' else 'ok-boot', ms)
        self.results.append((name, 'ok'))
        return 'ok'

    def run_once(self, fn, name=None):
        name = name or fn.__name__
        version = fingerprint(fn)
        if self._ensure() and not _force():
            try:
                r = self.db.one('SELECT version, status FROM schema_migrations WHERE name=?', (name,))
            except Exception:
                r = None
            if r and r.get('status') == 'ok' and r.get('version') == version:
                self.results.append((name, 'skipped'))
                return 'skipped'
        return self._exec(fn, name, version, 'once')

    def run_each_boot(self, fn, name=None):
        name = name or fn.__name__
        self._ensure()
        return self._exec(fn, name, fingerprint(fn), 'boot')

    def summary(self):
        c = {}
        for _n, st in self.results:
            c[st] = c.get(st, 0) + 1
        return c


def status_rows(db):
    try:
        return db.rows('SELECT name, version, applied_at, status, duration_ms, detail FROM schema_migrations '
                       'ORDER BY name') or []
    except Exception:
        return []
