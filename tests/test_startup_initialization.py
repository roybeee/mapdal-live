"""Real asyncio/thread scheduling with mocked database initialization boundaries."""
import ast
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest


def startup_scope(monkeypatch, init_db, ensure_ready):
    scope = {'asyncio': asyncio, 'threading': threading,
             'asynccontextmanager': asynccontextmanager, 'DB_READY': False,
             '_init_db': init_db}
    monkeypatch.setitem(sys.modules, 'admin_v2', SimpleNamespace(ensure_ready=ensure_ready))
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'app.py').read_text())
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {
            '_initialize_services', 'lifespan'
        }:
            exec(compile(ast.Module(body=[node], type_ignores=[]), '<production>', 'exec'), scope)
    return scope


def test_startup_serializes_initialization_without_blocking_event_loop(monkeypatch):
    db_started, db_release = threading.Event(), threading.Event()
    admin_started, admin_release = threading.Event(), threading.Event()
    calls = []

    def init_db():
        calls.append(('db', threading.get_ident()))
        db_started.set()
        assert db_release.wait(2), 'test did not release database initialization'
        scope['DB_READY'] = True

    def ensure_ready():
        assert scope['DB_READY']
        calls.append(('admin', threading.get_ident()))
        admin_started.set()
        assert admin_release.wait(2), 'test did not release admin initialization'

    scope = startup_scope(monkeypatch, init_db, ensure_ready)

    async def run():
        main_thread = threading.get_ident()
        context = scope['lifespan'](None)
        task = asyncio.create_task(context.__aenter__())
        try:
            for _ in range(100):
                if db_started.is_set():
                    break
                await asyncio.sleep(.005)
            assert db_started.is_set()
            assert not task.done(), 'server accepted requests before database initialization'
            assert not admin_started.is_set()
            db_release.set()
            for _ in range(100):
                if admin_started.is_set():
                    break
                await asyncio.sleep(.005)
            assert admin_started.is_set()
            assert not task.done(), 'server accepted requests before admin initialization'
            admin_release.set()
            await asyncio.wait_for(task, 2)
            assert [call[0] for call in calls] == ['db', 'admin']
            assert all(call[1] != main_thread for call in calls)
        finally:
            db_release.set()
            admin_release.set()
            await task
            await context.__aexit__(None, None, None)

    asyncio.run(run())


def test_failed_database_readiness_prevents_startup_and_admin_initialization(monkeypatch):
    admin_calls = []
    scope = startup_scope(monkeypatch, lambda: None, lambda: admin_calls.append(True))

    async def run():
        with pytest.raises(RuntimeError, match='Database initialization failed'):
            async with scope['lifespan'](None):
                pytest.fail('startup must not yield when DB_READY is false')

    asyncio.run(run())
    assert admin_calls == []


def test_admin_initialization_failure_prevents_startup(monkeypatch):
    def init_db():
        scope['DB_READY'] = True

    def ensure_ready():
        raise RuntimeError('admin initialization failed')

    scope = startup_scope(monkeypatch, init_db, ensure_ready)

    async def run():
        with pytest.raises(RuntimeError, match='admin initialization failed'):
            async with scope['lifespan'](None):
                pytest.fail('startup must not yield before admin initialization succeeds')

    asyncio.run(run())
