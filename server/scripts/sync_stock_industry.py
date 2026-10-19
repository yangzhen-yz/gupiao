"""sync_stock_industry.py - 用 baostock 一次性把全市场行业分类落到本地表（2026-10-19）

为什么要有这张表：资金流向页的「行业」列原来只有东财 clist 的 f100 一个来源，
于是东财一封（10-19 实测 clist 全断近一天）整列变成"—"，板块资金流聚合也一起没了分母。
行业分类是**几个月都不怎么变**的东西，根本不该跟着行情接口一起失效 ——
baostock 的 `query_stock_industry()` 一次调用就返回全市场分类，落成表之后：
  · 个股排名的行业列不再依赖任何被封的接口；
  · 东财不可用时，板块资金流还能用「新浪个股资金流 × 本地行业」聚合出来。

为什么走 baostock 而不是东财：东财只有 clist 这一条路给行业，而它正是会被封的那条。
baostock 的风险是**登录频率**（拉黑过），所以这里：
  · 一次登录、一次查询（不是逐票），
  · 复用 `kline_history` 那份跨进程冷却文件（被封期间连登录都不做），
  · 调度上放在周二凌晨 03:30（离 18:15~20:15 的日K同步和 09:00 海选都远）。

用法：
  python scripts/sync_stock_industry.py            # 同步（幂等，重跑覆盖）
  python scripts/sync_stock_industry.py --status   # 只看表里有多少、什么时候刷的
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import get_db_conn                     # noqa: E402

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

DDL = '''CREATE TABLE IF NOT EXISTS stock_industry_map (
    symbol        TEXT PRIMARY KEY,     -- 带交易所前缀：sh600519 / sz000001
    code          TEXT,                 -- 纯代码
    industry_code TEXT,
    industry      TEXT,
    updated_at    TEXT
)'''


def status() -> int:
    conn = get_db_conn()
    try:
        try:
            n, ind, last = conn.execute(
                'SELECT COUNT(1), COUNT(DISTINCT industry), MAX(updated_at) '
                'FROM stock_industry_map').fetchone()
        except Exception:
            print('stock_industry_map 表还不存在，先跑一次同步')
            return 1
        print(f'本地行业分类：{n} 只、{ind} 个行业，最后更新 {last}')
        for r in conn.execute('SELECT symbol, industry FROM stock_industry_map LIMIT 5'):
            print('  ', r[0], r[1])
        return 0
    finally:
        conn.close()


def sync() -> int:
    from services.data import kline_history as KH
    left = KH.ban_cooldown_remaining()
    if left > 0:
        print(f'[行业分类] baostock 仍在冷却（剩 {left:.0f} 分钟），本轮不登录 —— '
              f'这正是当初被拉黑的教训：越撞越封')
        return 2
    try:
        import baostock as bs
    except ImportError:
        print('[行业分类] 未安装 baostock')
        return 3
    lg = None
    try:
        lg = bs.login()
        if getattr(lg, 'error_code', '0') != '0':
            print(f'[行业分类] 登录失败：login {lg.error_code} {lg.error_msg}')
            # 故意**不**写 note_ban：这个文件是日K同步备路共用的冷却闸门，
            # 一个每周次要任务失败就把主备路锁 60 分钟，是拿别人的预算替自己买单。
            # 这里只读冷却、不写冷却。
            return 4
        rs = bs.query_stock_industry()
        if getattr(rs, 'error_code', '0') != '0':
            print(f'[行业分类] 查询失败：query {rs.error_code} {rs.error_msg}')
            return 4
        fields = list(rs.fields or [])
        rows = []
        while rs.next():
            rows.append(dict(zip(fields, rs.get_row_data())))
        if not rows:
            print('[行业分类] 返回 0 行（接口异常？本次不落库，保留旧表）')
            return 5
        now = time.strftime('%Y-%m-%d %H:%M:%S')
        recs = []
        for x in rows:
            code = str(x.get('code') or '')              # baostock 格式 sh.600519
            pure = code.split('.')[-1]
            prefix = code.split('.')[0] if '.' in code else ''
            if not pure or not prefix:
                continue
            recs.append((f'{prefix}{pure}', pure, str(x.get('industryCode') or ''),
                         str(x.get('industry') or ''), now))
        conn = get_db_conn()
        try:
            conn.execute(DDL)
            conn.execute('DELETE FROM stock_industry_map')      # 整表换水，不留旧行业
            conn.executemany('INSERT OR REPLACE INTO stock_industry_map '
                             '(symbol, code, industry_code, industry, updated_at) '
                             'VALUES (?,?,?,?,?)', recs)
            conn.commit()
        finally:
            conn.close()
        named = sum(1 for r in recs if r[3])
        print(f'[行业分类] 落库 {len(recs)} 只（有行业名 {named} 只，'
              f'{len(set(r[3] for r in recs if r[3]))} 个行业），耗时 {time.strftime("%H:%M:%S")}')
        return 0
    finally:
        try:
            bs.logout()
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--status', action='store_true', help='只看现状，不登录 baostock')
    args = ap.parse_args()
    return status() if args.status else sync()


if __name__ == '__main__':
    sys.exit(main())
