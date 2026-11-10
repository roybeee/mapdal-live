"""migrations.Runner — 일회성은 같은 코드면 1회만, 실패는 기록·재시도, 매 기동 작업은 매번."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import migrations as mg  # noqa: E402
import inicis_meta  # noqa: E402


class DB:
    def __init__(self):
        self.c = sqlite3.connect(':memory:')
        self.c.row_factory = sqlite3.Row

    def run(self, sql, args=()):
        cur = self.c.execute(sql, args); self.c.commit(); return cur.rowcount

    def rows(self, sql, args=()):
        return [dict(r) for r in self.c.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        r = self.rows(sql, args); return r[0] if r else None


calls = []


def once_job():
    calls.append('once')


def boot_job():
    calls.append('boot')


def test_run_once_skips_same_code_and_each_boot_always_runs(monkeypatch):
    monkeypatch.delenv('MIGRATIONS_FORCE', raising=False)
    calls.clear()
    db = DB()
    for _ in range(3):
        r = mg.Runner(db, lambda: '2026-11-10T09:00:00', log=lambda *a: None)
        r.run_once(once_job)
        r.run_each_boot(boot_job)
    assert calls.count('once') == 1 and calls.count('boot') == 3
    st = {x['name']: x['status'] for x in mg.status_rows(db)}
    assert st == {'once_job': 'ok', 'boot_job': 'ok-boot'}


def test_changed_code_reruns(monkeypatch):
    calls.clear()
    db = DB()
    r = mg.Runner(db, lambda: 't', log=lambda *a: None)
    r.run_once(once_job)
    db.run("UPDATE schema_migrations SET version='old' WHERE name='once_job'")
    r.run_once(once_job)
    assert calls.count('once') == 2


def test_failure_is_recorded_and_retried(monkeypatch):
    db = DB()
    logs = []
    state = {'n': 0}

    def flaky():
        state['n'] += 1
        if state['n'] == 1:
            raise RuntimeError('boom')
    r = mg.Runner(db, lambda: 't', log=logs.append)
    assert r.run_once(flaky) == 'failed'
    assert any('FAILED flaky' in x for x in logs)
    row = db.one("SELECT status, detail FROM schema_migrations WHERE name='flaky'")
    assert row['status'] == 'failed' and 'boom' in row['detail']
    assert r.run_once(flaky) == 'ok'
    assert r.run_once(flaky) == 'skipped'


def test_force_reruns(monkeypatch):
    calls.clear()
    db = DB()
    mg.Runner(db, lambda: 't', log=lambda *a: None).run_once(once_job)
    monkeypatch.setenv('MIGRATIONS_FORCE', '1')
    mg.Runner(db, lambda: 't', log=lambda *a: None).run_once(once_job)
    assert calls.count('once') == 2


def test_inicis_meta_tid_routing(monkeypatch):
    monkeypatch.setenv('INICIS_MID', 'MAPDALNEW1')
    monkeypatch.setenv('INICIS_INIAPI', 'k-new')
    monkeypatch.setenv('INICIS_MID_OLD', 'MAPDALOLD1')
    monkeypatch.setenv('INICIS_INIAPI_OLD', 'k-old')
    assert inicis_meta._inicis_cred_for_tid('StdpayCARDMAPDALOLD120260101120000123456')[:2] == ('MAPDALOLD1', 'k-old')
    assert inicis_meta._inicis_cred_for_tid('StdpayCARDMAPDALNEW120260101120000123456')[:2] == ('MAPDALNEW1', 'k-new')
    assert inicis_meta._inicis_cred_for_tid('unknown') is None
    assert inicis_meta._inicis_mid_of_tid('StdpayCARDZZZZZZZZZZ20260101120000') == 'ZZZZZZZZZZ'
