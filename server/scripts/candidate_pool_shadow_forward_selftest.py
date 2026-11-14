"""Verify historical backfill cannot satisfy the forward sample gate."""
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from services.pick import candidate_pool_shadow as cps  # noqa: E402
from services.support import sample_days  # noqa: E402


def main():
    passed = 0
    # 用 mkdtemp 而不是 TemporaryDirectory：db_hygiene_check 的「全临时路径」判据
    # 只认 mkstemp/mkdtemp/gettempdir，显式写出来才能被静态检查识别为已隔离。
    # 语义不变——db_path 始终落在这个临时目录下，绝不碰生产库。
    tmp = tempfile.mkdtemp(prefix='candidate_shadow_forward_')
    db_path = os.path.join(tmp, 'shadow.db')
    original_conn = cps._conn
    original_filter = sample_days.filter_sample_days
    cps._conn = lambda: sqlite3.connect(db_path)
    sample_days.filter_sample_days = lambda dates: {
        'counted': sorted(dates), 'skipped': [],
        'calendarSource': 'selftest', 'rule': 'selftest',
    }
    try:
        conn = cps._conn()
        cps._ensure_table(conn)
        dates = [f'2026-10-{day:02d}' for day in range(1, 21)]
        dates.extend(('2026-11-09', '2026-11-10'))
        rows = []
        for day in dates:
            for i in range(10):
                rows.append((
                    day, f'sh{600000 + i:06d}', 1.0, 1, 1, 0, None,
                    1.0, 2.0, 1, 'selftest', 'selftest',
                ))
        conn.executemany(
            'INSERT INTO candidate_pool_shadow '
            '(pool_date,symbol,chg,passed,in_pool,n_fail,rejected_by,'
            't1_close_ret_pct,t1_high_ret_pct,t1_filled,created_at,updated_at) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            rows,
        )
        conn.commit()
        conn.close()

        mixed = cps.evaluate(min_days=20)
        assert mixed['ready'] is True and mixed['days'] == 22
        forward = cps.evaluate(min_days=20, forward_from='2026-11-06')
        assert forward['ready'] is False and forward['days'] == 2
        assert forward['sampleScope'] == 'forward'
        assert forward['groups']['弱势0~2 | 进池']['n'] == 20
        assert cps.evaluate(min_days=2, forward_from='2026-11-06')['ready'] is True
        try:
            cps.evaluate(forward_from='2026/11/06')
            raise AssertionError('malformed forward_from unexpectedly accepted')
        except ValueError:
            pass
        passed = 4
    finally:
        cps._conn = original_conn
        sample_days.filter_sample_days = original_filter
        shutil.rmtree(tmp, ignore_errors=True)
    print(f'candidate_pool_shadow_forward_selftest: {passed}/4 passed')


if __name__ == '__main__':
    main()
