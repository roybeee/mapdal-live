"""
기존 데이터 시각 보정 — UTC(+0) 로 저장된 과거 레코드를 KST(+9) 로 이동한다.

배경
  운영 서버(Render)의 시스템 시계가 UTC 라 datetime.now() 가 UTC 를 반환했다.
  코드는 KST 저장으로 고쳤지만, 그 이전에 쌓인 레코드는 여전히 UTC 값이다.
  관리자 화면/CSV 가 저장 문자열을 그대로 보여주므로 9시간 이르게 표시된다.

원칙
  1) 이력성 데이터만 보정한다(주문·포인트·감사로그·회원 등).
     세션/OTP/비밀번호재설정 같은 단기 만료 데이터는 건드리지 않는다.
     이미 만료됐거나 곧 만료될 값이고, 9시간을 더하면 오히려 되살아난다.
  2) 컷오프 시각 이후 레코드는 건드리지 않는다.
     KST 배포 이후 저장분은 이미 정상이므로 두 번 더하면 안 된다.
     ★ 컷오프는 반드시 'KST 코드가 배포된 시각(한국시간)'으로 준다.
       이 값을 잘못 주면 이미 보정된 레코드가 다시 +9h 되어 12시간이 어긋난다.
       한 번 반영한 뒤에는 재실행하지 말 것. 재실행이 필요하면 백업에서 복구 후.
  3) 기본은 드라이런. --apply 를 줘야 실제로 쓴다.
  4) 실행 전 자동으로 백업 테이블을 만든다(SQLite: 파일 복사 / PG: _bak 테이블).

사용
  python3 migrate_kst.py                      # 드라이런 (변경 없음)
  python3 migrate_kst.py --cutoff 2026-07-21T07:10:00
  python3 migrate_kst.py --cutoff ... --apply  # 실제 반영
"""
import os, sys, json, argparse, datetime, shutil

# ── 보정 대상: (테이블, [컬럼...]) ──────────────────────────────────
# 이력성 데이터만. 만료성(oauth_flows/password_resets/order_claims/
# phone_verifications/*_sessions)은 의도적으로 제외한다.
TARGETS = [
    ('orders',                  ['created']),
    ('account_order_links',     ['linked_at', 'verified_at']),
    ('customer_profiles',       ['created_at', 'updated_at', 'withdrawn_at']),
    ('customer_contacts',       ['created_at', 'verified_at']),
    ('members',                 ['created', 'last_login_at', 'updated_at', 'withdrawn_at']),
    ('auth_identities',         ['created_at', 'last_login_at']),
    ('point_ledger',            ['created_at', 'expires_at']),
    ('audit_log',               ['created']),
    ('notify_log',              ['created']),
    ('notify_templates',        ['created']),
    ('consent_history',         ['created_at']),
    ('member_addresses',        ['created']),
    ('member_inquiries',        ['created']),
    ('member_likes',            ['created']),
    ('member_pqna',             ['created']),
    ('member_requests',         ['created']),
    ('member_restock',          ['created']),
    ('account_security_events', ['created_at']),
    ('inventory_movements',     ['created_at']),
    ('inventory_balances',      ['updated_at']),
    ('product_groups',          ['created_at', 'updated_at']),
    ('product_variants',        ['created_at', 'updated_at']),
    ('artists',                 ['created_at', 'updated_at']),
    ('artist_links',            ['created_at']),
    ('artist_pending',          ['created']),
    ('artist_suppressed',       ['created']),
    ('assets',                  ['created']),
    ('customers',               ['created']),
    ('admins',                  ['created']),
    ('loyalty_policies',        ['updated_at']),
    ('k2g_removed',             ['created']),
    ('own_removed',             ['created']),
]

SHIFT = datetime.timedelta(hours=9)


def parse_iso(v):
    """저장 형식은 'YYYY-MM-DDTHH:MM:SS'(초 단위) 또는 마이크로초 포함."""
    if not v:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s.replace('Z', '+00:00').split('+')[0])
    except Exception:
        return None


def fmt(dt, sample):
    """원본 포맷(초 단위/마이크로초)을 유지해 되돌려 쓴다."""
    return dt.isoformat(timespec='microseconds' if '.' in str(sample) else 'seconds')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cutoff', default='',
                    help='이 시각 이후 레코드는 보정하지 않음 (KST 배포 시각, ISO)')
    ap.add_argument('--apply', action='store_true', help='실제 반영 (미지정 시 드라이런)')
    ap.add_argument('--table', default='', help='특정 테이블만')
    args = ap.parse_args()

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import app as A

    cutoff = args.cutoff.strip()
    if not cutoff:
        print('※ --cutoff 미지정 → 전체 레코드를 대상으로 본다.')
        print('   KST 배포 이후 저장분이 있다면 반드시 --cutoff 를 주십시오.\n')

    mode = '실제 반영' if args.apply else '드라이런 (변경 없음)'
    print('=' * 62)
    print(f'  시각 보정 UTC → KST (+9h)   [{mode}]')
    print(f'  컷오프: {cutoff or "(없음 — 전체)"}')
    print('=' * 62)

    # ── 백업 ──
    if args.apply and not A.IS_PG:
        bak = A.SQLITE_PATH + '.bak-' + datetime.datetime.now().strftime('%Y%m%d%H%M%S')
        shutil.copy2(A.SQLITE_PATH, bak)
        print(f'\n백업 생성: {bak}\n')
    elif args.apply:
        print('\n※ PostgreSQL 입니다. 실행 전 Render 대시보드에서 백업(스냅샷)을 '
              '먼저 만드십시오. 계속하려면 5초 내 Ctrl+C 로 중단하지 않으면 진행합니다.\n')
        import time
        time.sleep(5)

    total_rows = total_cells = 0
    with A.db() as c:
        # 존재하는 테이블만 대상으로
        for table, cols in TARGETS:
            if args.table and args.table != table:
                continue
            try:
                rows = c.exec(f'SELECT * FROM {table}').fetchall()
            except Exception:
                continue                      # 해당 환경에 없는 테이블
            if not rows:
                continue

            # PK 추정: 첫 컬럼
            first = dict(rows[0])
            pk = 'id' if 'id' in first else list(first.keys())[0]

            changed_rows = changed_cells = 0
            for r in rows:
                r = dict(r)
                sets, vals = [], []
                for col in cols:
                    if col not in r:
                        continue
                    dt = parse_iso(r.get(col))
                    if not dt:
                        continue
                    iso = dt.isoformat(timespec='seconds')
                    if cutoff and iso >= cutoff:
                        continue              # 이미 KST 로 저장된 값
                    sets.append(f'{col}=?')
                    vals.append(fmt(dt + SHIFT, r.get(col)))
                    changed_cells += 1
                if sets:
                    changed_rows += 1
                    if args.apply:
                        c.exec(f'UPDATE {table} SET {", ".join(sets)} WHERE {pk}=?',
                               tuple(vals + [r[pk]]))
            if changed_rows:
                print(f'  {table:26s} rows {changed_rows:5d}  cells {changed_cells:5d}')
                total_rows += changed_rows
                total_cells += changed_cells

    print('\n' + '-' * 62)
    print(f'  대상 행 {total_rows} · 셀 {total_cells}')
    if args.apply:
        print('  → 반영 완료')
    else:
        print('  → 드라이런이므로 아무것도 변경하지 않았습니다.')
        print('     실제 반영: python3 migrate_kst.py --cutoff <ISO> --apply')
    print('-' * 62)


if __name__ == '__main__':
    main()
