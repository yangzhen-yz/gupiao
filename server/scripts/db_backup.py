#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""db_backup.py - SQLite 数据库定时备份（2026-11-05，P0-3 资产不丢）

为什么有它：
    系统的全部不可再生产资产都在几个 SQLite 文件里——主库 gupiao.db（73 张表、
    推荐/复盘/影子评估/收评全在这）、日K库 history_kline.db（346 万行 / 507MB+）、
    5 分钟库 history_minute.db。此前备份**只有两个手动目录**，scheduler 里零调用，
    等于库丢了就回到解放前。

做法：
    · 用 sqlite3 的在线备份 API（`Connection.backup()`）做快照，源库以 `mode=ro` 打开，
      即使服务正在写也不阻塞、不拷出半截文件（WAL 会被读进目标，得到一份一致副本）。
    · 源库发现：主库走 app.config.SQLITE_DB_PATH，其余扫 server/data/*.db；
      自动排除 analysis_out / *_backup_* 等备份目录，跳过 0 字节占位文件。
    · 每次备份落到 server/analysis_out/db_backup_YYYYMMDD_HHMMSS/，带 manifest.json。
    · 保留策略：只留最近 keep_days（默认 7）天内的备份目录，更早的删除；同时硬上限 30 个。

调用：
    python server/scripts/db_backup.py --mode incremental      # 平日
    python server/scripts/db_backup.py --mode full             # 周六全量
    python server/scripts/db_backup.py --list                  # 看现有备份
    python server/scripts/db_backup.py --dry-run               # 只打印要做什么

注：full / incremental 对 SQLite 都是整库拷贝，区别只在 manifest 标记与命名，
便于排查时一眼区分「这周内哪份是全量基线」。
"""
import argparse
import glob
import json
import os
import shutil
import sqlite3
import sys
import time

# server/ 目录（脚本在 server/scripts/ 下）
SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

# 备份落地根目录
BACKUP_ROOT = os.path.join(SERVER_DIR, 'analysis_out')
# 这些目录名里的 db 不算待备份源，且其中的目录不应被 prune 误删（它们是"备份根"的子目录）
_BACKUP_DIR_MARKERS = ('db_backup_', 'optim_backup', 'docs_backup', 'analysis_out')


def _is_excluded(path: str) -> bool:
    norm = os.path.normpath(path)
    parts = norm.split(os.sep)
    for m in _BACKUP_DIR_MARKERS:
        # 排除任何落在备份目录里的源（防止"备份备份"）
        if m in parts:
            return True
    return False


def discover_dbs(server_dir: str = SERVER_DIR):
    """返回待备份的 .db 文件绝对路径列表。"""
    found = []
    # 1) 主库（来自 config，最权威）
    try:
        from app.config import SQLITE_DB_PATH
        if os.path.exists(SQLITE_DB_PATH) and os.path.getsize(SQLITE_DB_PATH) >= 1:
            found.append(os.path.abspath(SQLITE_DB_PATH))
    except Exception as e:
        print(f'[db_backup] 读 app.config.SQLITE_DB_PATH 失败（跳过主库）: {e}')
    # 2) server/data/*.db
    data_dir = os.path.join(server_dir, 'data')
    if os.path.isdir(data_dir):
        for f in sorted(os.listdir(data_dir)):
            if not f.endswith('.db'):
                continue
            p = os.path.abspath(os.path.join(data_dir, f))
            if os.path.getsize(p) >= 1 and not _is_excluded(p):
                found.append(p)
    # 3) 兜底：server 树下其它 .db（排除备份目录、0 字节）
    for p in glob.glob(os.path.join(server_dir, '**', '*.db'), recursive=True):
        p = os.path.abspath(p)
        if p in found:
            continue
        if os.path.getsize(p) < 1 or _is_excluded(p):
            continue
        found.append(p)
    # 去重保序
    seen, out = set(), []
    for p in found:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def backup_one(src: str, dst: str) -> int:
    """在线备份单库；返回目标字节数。失败时抛异常。"""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    # 源以只读打开，避免与服务的写入者争锁；URI 模式保证 Windows 下也能 mode=ro
    src_uri = 'file:{}?mode=ro'.format(src.replace('\\', '/'))
    src_conn = sqlite3.connect(src_uri, uri=True, timeout=60)
    try:
        dst_conn = sqlite3.connect(dst, timeout=60)
        try:
            src_conn.backup(dst_conn, pages=1024)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()
    return os.path.getsize(dst)


def _list_backups(server_dir: str = SERVER_DIR):
    root = os.path.join(server_dir, 'analysis_out')
    if not os.path.isdir(root):
        return []
    out = []
    for d in sorted(os.listdir(root)):
        if not d.startswith('db_backup_'):
            continue
        full = os.path.join(root, d)
        if not os.path.isdir(full):
            continue
        man = os.path.join(full, 'manifest.json')
        info = {'dir': d, 'mtime': os.path.getmtime(full)}
        if os.path.exists(man):
            try:
                with open(man, encoding='utf-8') as fh:
                    m = json.load(fh)
                info['mode'] = m.get('mode')
                info['files'] = len(m.get('files', []))
                info['bytes'] = sum(x.get('bytes', 0) for x in m.get('files', []))
            except Exception:
                info['mode'] = '?'
        out.append(info)
    return out


def prune(server_dir: str = SERVER_DIR, keep_days: int = 7, dry_run: bool = False):
    """删除超过 keep_days 天的备份目录；并硬上限 30 个（保留最新）。"""
    backups = _list_backups(server_dir)
    now = time.time()
    cutoff = now - keep_days * 86400
    removed = []
    for b in backups:
        if b['mtime'] < cutoff:
            full = os.path.join(server_dir, 'analysis_out', b['dir'])
            if dry_run:
                removed.append((b['dir'], 'age'))
                continue
            try:
                shutil.rmtree(full)
                removed.append((b['dir'], 'age'))
            except Exception as e:
                print(f'[db_backup] 删除 {b["dir"]} 失败: {e}')
    # 硬上限：剩余超过 30 个则删最旧的
    remaining = _list_backups(server_dir)
    if len(remaining) > 30:
        excess = len(remaining) - 30
        for b in remaining[:excess]:
            full = os.path.join(server_dir, 'analysis_out', b['dir'])
            if dry_run:
                removed.append((b['dir'], 'cap'))
                continue
            try:
                shutil.rmtree(full)
                removed.append((b['dir'], 'cap'))
            except Exception as e:
                print(f'[db_backup] 删除(上限) {b["dir"]} 失败: {e}')
    return removed


def run_backup(mode: str = 'incremental', server_dir: str = SERVER_DIR,
               keep_days: int = 7, dry_run: bool = False):
    ts = time.strftime('%Y%m%d_%H%M%S')
    dest_dir = os.path.join(server_dir, 'analysis_out', f'db_backup_{ts}')
    dbs = discover_dbs(server_dir)
    if not dbs:
        raise RuntimeError('未发现任何待备份数据库，疑似路径配置错误')
    manifest = {
        'createdAt': ts,
        'mode': mode,
        'serverDir': os.path.abspath(server_dir),
        'keepDays': keep_days,
        'files': [],
    }
    print(f'[db_backup {ts}] 模式={mode}，待备份 {len(dbs)} 个库：')
    for src in dbs:
        rel = os.path.relpath(src, server_dir)
        dst = os.path.join(dest_dir, rel)
        src_size = os.path.getsize(src)
        print(f'  - {rel}  ({src_size/1024/1024:.1f} MB)')
        if not dry_run:
            try:
                backup_one(src, dst)
                manifest['files'].append({'src': src, 'dst': dst, 'bytes': src_size})
            except Exception as e:
                print(f'  ⚠️ 备份失败 {rel}: {type(e).__name__}: {e}')
                raise
    if not dry_run:
        os.makedirs(dest_dir, exist_ok=True)
        with open(os.path.join(dest_dir, 'manifest.json'), 'w', encoding='utf-8') as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2)
    removed = prune(server_dir, keep_days, dry_run)
    if removed:
        print(f'[db_backup {ts}] 清理 {len(removed)} 个过期/超限备份')
    tag = '演练：' if dry_run else ''
    print(f'[db_backup {ts}] {tag}完成，落盘 {dest_dir}')
    return manifest


def main():
    ap = argparse.ArgumentParser(description='A股系统 SQLite 定时备份')
    ap.add_argument('--mode', choices=['full', 'incremental'], default='incremental')
    ap.add_argument('--server-dir', default=SERVER_DIR)
    ap.add_argument('--keep-days', type=int, default=7)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--list', action='store_true', help='列出已有备份后退出')
    args = ap.parse_args()

    if args.list:
        for b in _list_backups(args.server_dir):
            mb = b.get('bytes', 0) / 1024 / 1024
            print(f"{b['dir']:<28} mode={b.get('mode','?'):<12} "
                  f"files={b.get('files','?'):<3} ~{mb:.0f}MB  "
                  f"mtime={time.strftime('%Y-%m-%d %H:%M', time.localtime(b['mtime']))}")
        return
    run_backup(args.mode, args.server_dir, args.keep_days, args.dry_run)


if __name__ == '__main__':
    main()
