# -*- coding: utf-8 -*-
"""db_sentinel.py - 盘中写锁哨兵（2026-11-03）

为什么需要它
    2026-11-03 那次：回测补验在事务里等外部 K 线，把 gupiao.db 的写锁占了 1 小时以上。
    发现方式是**用户打不开页面**（16:5x），而锁从 15:20 就开始坏了；定位又花掉约两小时，
    中间还误判过一次（怀疑到遗留进程头上）、白重启两次。哨兵要改的不是"谁来处理"，
    而是这两件事：发现时间（用户撞上前就先报）与起点信息（直接给"锁被谁占、占多久"，
    而不是从"服务挂了"开始猜）。

它只回答一个问题
    现在有没有人长时间占着写锁。做法是拿一条**独立连接**试 `BEGIN IMMEDIATE`：
      · 阈值秒内拿到 → 说明库是通的，立刻 ROLLBACK 交还，什么都不写；
      · 阈值秒内拿不到 → 记一次告警，附上能帮定位的现场证据。
    因为拿到就回滚，哨兵自身不会变成新的持锁者——这是它必须守住的底线。

为什么放后端进程而不是仓库外的看护脚本
    看护在仓库外、由 SYSTEM 计划任务拉起、改动不进版本控制，改它等于在没有审查和回滚的
    地方动线上基础设施；而锁竞争发生时后端进程是活的（只是写排队），进程内哨兵完全够用。
    进程整个挂掉那种情况，看护本来就会重启，不需要这里重复覆盖。

边界（如实）
    · 只测 gupiao.db 主库；history_kline.db / history_minute.db 不在监控范围。
    · 只在盘中跑（默认 09:10~15:10）：收盘后锁竞争的表现是页面慢，用户自己会撞上，
      不值得为它发告警；盘中错过 09:45 / 14:45 批次才是真损失。
    · 它不解除锁，只报告。解除要靠重启或等持锁方放手。
"""
from __future__ import annotations

import io
import os
import sqlite3
import time
from datetime import datetime
from typing import Any, Dict, Optional

# 本文件在 server/services/ops/ 下，要到 server 根得往上走三层（ops→services→server）。
# 首版只走两层，结果日志路径算成 server/services/server_supervised.log，
# 告警里的"最近任务锚点"一直是读日志失败——自测补了断言才抓到。
# 2026-11-10 根目录收敛：日志统一进 server/logs/（看护重定向同步改）；
# 新文件还没产出的重启间隙回退旧路径，保证锚点不断。
SERVER_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SUPERVISED_LOG = os.path.join(SERVER_DIR, 'logs', 'server_supervised.log')
if not os.path.exists(SUPERVISED_LOG):
    _legacy_log = os.path.join(SERVER_DIR, 'server_supervised.log')
    if os.path.exists(_legacy_log):
        SUPERVISED_LOG = _legacy_log

# 进程内状态：冷却计时 + 连续异常计数（重启清零，够用）
_last_alert_ts = 0.0
_consecutive_busy = 0


def _cfg(key: str, default):
    """现读 app_settings 覆盖表；读不到（表还没建/库被锁）就退回 config 默认值。
    这里绝不能因为读配置失败就不告警——库被锁的时候恰恰是最需要告警的时候。"""
    try:
        from services.support import app_settings as A
        if key in [s['key'] for s in A.SPEC]:
            v = A.value(key)
            if v is not None:
                return v
    except Exception:
        pass
    import app.config as cfg
    return getattr(cfg, key.upper(), default)


def probe_write_lock(threshold_sec: float) -> Dict[str, Any]:
    """独立连接试抢写锁。返回 {waited, got, error}。"""
    from app.config import SQLITE_DB_PATH
    t0 = time.time()
    conn = None
    try:
        # timeout 就等于阈值：拿不到会在这之后抛 OperationalError，waited≈阈值
        conn = sqlite3.connect(SQLITE_DB_PATH, timeout=max(0.5, float(threshold_sec)),
                               isolation_level=None)
        conn.execute('BEGIN IMMEDIATE')
        waited = time.time() - t0
        conn.execute('ROLLBACK')          # 立刻交还：哨兵绝不持锁
        return {'waited': waited, 'got': True, 'error': None}
    except Exception as e:
        return {'waited': time.time() - t0, 'got': False, 'error': str(e)[:120]}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _recent_task_anchor(lines_back: int = 400) -> str:
    """从看护日志尾部找最近一条"任务在跑"的锚点，给告警当现场证据。"""
    keys = ('定时任务', '预热', '回测', '自愈', '补日线', '保活', '批量扫描', '重建')
    try:
        with io.open(SUPERVISED_LOG, encoding='utf-8', errors='replace') as f:
            tail = f.readlines()[-lines_back:]
        hits = [l.strip()[:90] for l in tail if any(k in l for k in keys)]
        return hits[-1] if hits else '(日志里没找到任务锚点)'
    except Exception as e:
        return '(读日志失败: %s)' % str(e)[:40]


def _lock_wait_minutes() -> Optional[float]:
    """用 -wal 的 mtime 反推"多久没有成功写入了"——这是今天定位时最有用的一条证据。"""
    from app.config import SQLITE_DB_PATH
    try:
        st = os.stat(SQLITE_DB_PATH)
        return round((time.time() - st.st_mtime) / 60.0, 1)
    except Exception:
        return None


def check_and_alert(force: bool = False) -> Dict[str, Any]:
    """跑一次探测；超阈值且不在冷却期就推企微。返回本次结果（供自测与日志）。"""
    global _last_alert_ts, _consecutive_busy
    threshold = float(_cfg('db_lock_sentinel_threshold_sec', 2))
    cooldown = float(_cfg('db_lock_sentinel_cooldown_min', 10)) * 60.0
    enabled = bool(_cfg('db_lock_sentinel_enabled', True))

    # 开关关掉时**连探测都不做**（注释与配置页都这么写的，就得真做到）：
    # 哨兵自己每分钟开一条连接抢一次写锁，关掉后不该继续产生这点副作用
    if not enabled:
        return {'enabled': False, 'probed': False, 'got': None, 'alerted': False}

    r = probe_write_lock(threshold)
    r['enabled'] = enabled
    r['threshold'] = threshold
    r['probed'] = True
    if r['got']:
        _consecutive_busy = 0
        return r

    _consecutive_busy += 1
    r['consecutive_busy'] = _consecutive_busy
    # force=True 用于自测：跳过冷却，验证告警文本与推送链路真的走通
    if not force and (time.time() - _last_alert_ts) < cooldown:
        r['alerted'] = False
        return r

    idle_min = _lock_wait_minutes()
    content = ('⚠ 数据库写锁告警 %s\n'
               '独立连接试 BEGIN IMMEDIATE，等 %.1fs 仍拿不到写锁（阈值 %.1fs，连续第 %d 次）。\n'
               '主库距最后一次成功写入 %s 分钟。\n'
               '最近任务锚点：%s\n'
               '含义：有连接占着写锁不放手，全库写会排队到 busy_timeout。'
               '盘中这会拖慢批次与页面，收盘后表现为页面卡。\n'
               '处置：先看上面的任务锚点是谁在写，必要时写 '
               'server/data/backend_restart_requested 让看护重启。'
               % (datetime.now().strftime('%H:%M:%S'), r['waited'], threshold,
                  _consecutive_busy, ('未知' if idle_min is None else '%.1f' % idle_min),
                  _recent_task_anchor()))
    pushed = False
    try:
        from services.support.push import push_wecom_webhook
        pushed = bool(push_wecom_webhook(content))
    except Exception as e:
        print(f'[写锁哨兵] 推送失败: {e}')
    _last_alert_ts = time.time()
    r['alerted'] = pushed
    print('[写锁哨兵 %s] 等 %.1fs 拿不到写锁（阈值 %.1fs，连续第 %d 次），推送=%s，最后写入 %s 分钟前'
          % (datetime.now().strftime('%H:%M:%S'), r['waited'], threshold,
             _consecutive_busy, pushed, idle_min))
    return r
