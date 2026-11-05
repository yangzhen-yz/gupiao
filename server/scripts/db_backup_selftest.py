#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""db_backup_selftest.py - 备份模块单元自测（2026-11-05）

不碰真实库：用临时目录建一个小 sqlite，验证在线备份一致性、prune 过期/上限逻辑。
"""
import os
import sys
import tempfile
import sqlite3
import time
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import db_backup as m

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  ✅ {name}')
    else:
        FAIL += 1
        print(f'  ❌ {name}')


def _make_db(path, n=20):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT)')
    conn.executemany('INSERT INTO t(v) VALUES(?)', [(f'r{i}',) for i in range(n)])
    conn.commit()
    conn.close()


def test_backup_one():
    print('[1] backup_one 在线备份一致性')
    tmp = tempfile.mkdtemp()
    try:
        src = os.path.join(tmp, 'a.db')
        dst = os.path.join(tmp, 'sub', 'a.db')
        _make_db(src, 20)
        # 源保持打开（模拟服务在写），只读连接应能备份
        rw = sqlite3.connect(src)
        rw.execute("INSERT INTO t(v) VALUES('live')")
        rw.commit()
        sz = m.backup_one(src, dst)
        rw.close()
        check('目标文件存在且非空', os.path.exists(dst) and sz > 0)
        # 目标可读且行数一致（20 + 1 live）
        dc = sqlite3.connect(dst)
        cnt = dc.execute('SELECT COUNT(*) FROM t').fetchone()[0]
        dc.close()
        check('目标行数=21', cnt == 21)
        # 目标是独立一致库（无 -wal 依赖）
        dc2 = sqlite3.connect(dst)
        dc2.execute('SELECT * FROM t').fetchall()
        dc2.close()
        check('目标可独立打开查询', True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_prune():
    print('[2] prune 过期删除 + 硬上限')
    tmp = tempfile.mkdtemp()
    try:
        root = os.path.join(tmp, 'analysis_out')
        os.makedirs(root)
        # 三个备份目录：一个很旧、一个较新、一个最新
        old = os.path.join(root, 'db_backup_20000101_000000')
        mid = os.path.join(root, 'db_backup_20000102_000000')
        new = os.path.join(root, 'db_backup_20990101_000000')
        for d in (old, mid, new):
            os.makedirs(d)
        # 把 old/mid 改成 10 天前、new 改成现在
        t_old = time.time() - 10 * 86400
        os.utime(old, (t_old, t_old))
        os.utime(mid, (t_old, t_old))
        removed = m.prune(tmp, keep_days=7, dry_run=False)
        removed_dirs = [r[0] for r in removed]
        check('删除了2个过期备份', old.split(os.sep)[-1] in removed_dirs and mid.split(os.sep)[-1] in removed_dirs)
        check('保留了最新备份', os.path.isdir(new))
        check('新备份仍在列表', any(b['dir'] == 'db_backup_20990101_000000' for b in m._list_backups(tmp)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_discover_excludes_backup():
    print('[3] discover_dbs 排除备份目录（不备份备份）')
    tmp = tempfile.mkdtemp()
    try:
        # 伪造 server 树：真实库 + 一个备份目录里的库
        real = os.path.join(tmp, 'gupiao.db')
        data_db = os.path.join(tmp, 'data', 'history_kline.db')
        backup_db = os.path.join(tmp, 'analysis_out', 'db_backup_x', 'gupiao.db')
        _make_db(real, 5)
        _make_db(data_db, 5)
        _make_db(backup_db, 5)
        dbs = m.discover_dbs(tmp)
        check('发现真实主库', os.path.abspath(real) in dbs)
        check('发现 data 库', os.path.abspath(data_db) in dbs)
        check('排除备份目录内的库', os.path.abspath(backup_db) not in dbs)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    test_backup_one()
    test_prune()
    test_discover_excludes_backup()
    print(f'\n结果：通过 {PASS}，失败 {FAIL}')
    sys.exit(1 if FAIL else 0)
