"""DAL domain: user."""

from ..dal_state import *

def list_user_ids() -> List[int]:
    """全部注册用户 id（调度器按用户循环写各自的池/自选时用）"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute('SELECT id FROM users ORDER BY id')
        rows = cursor.fetchall()
        conn.close()
        return [r['id'] for r in rows]
    except Exception as error:
        print(f'读取用户列表失败: {str(error)}')
        return []


def load_user_scan_pool(user_id: Optional[int] = None) -> List[str]:
    """自定义扫描池。

    user_id=None → 所有用户的并集（调度器语义：扫描宇宙要覆盖每个人关注的票，
                   与"谁在查看"无关；漏传也只是退化成改造前的全局行为，不会丢数据）
    user_id=具体 → 该用户的池（前端展示与写入）
    """
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        if user_id is None:
            cursor.execute('SELECT symbol FROM user_scan_pool GROUP BY symbol ORDER BY MIN(id)')
        else:
            cursor.execute('SELECT symbol FROM user_scan_pool WHERE user_id = ? ORDER BY id',
                           (int(user_id),))
        rows = cursor.fetchall()
        conn.close()
        return [row['symbol'] for row in rows]
    except Exception as error:
        print(f'加载自定义扫描池失败: {str(error)}')
        return []


def save_user_scan_pool(symbols: List[str], user_id: int = 1) -> bool:
    """整池覆盖写入（只动该用户的行，不再清空全表）"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM user_scan_pool WHERE user_id = ?', (int(user_id),))
        for symbol in symbols:
            cursor.execute(
                'INSERT OR IGNORE INTO user_scan_pool (user_id, symbol) VALUES (?, ?)',
                (int(user_id), symbol)
            )
        conn.commit()
        conn.close()
        return True
    except Exception as error:
        print(f'保存自定义扫描池失败: {str(error)}')
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def load_user_positions(user_id: int = 1) -> List[Dict]:
    """加载指定用户的全部持仓记录（user_id 缺省为 1，兼容旧调用）"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM user_positions WHERE user_id = ? ORDER BY id',
                       (int(user_id),))
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as error:
        print(f'加载持仓失败: {str(error)}')
        return []


def save_user_position(pos: Dict, user_id: int = 1) -> bool:
    """新增/更新一条持仓（按 (user_id, symbol) 去重，不同用户互不覆盖）"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute('''INSERT INTO user_positions (user_id, symbol, name, cost_price, shares, buy_date, note, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now', '+8 hours'))
            ON CONFLICT(user_id, symbol) DO UPDATE SET
                name=excluded.name, cost_price=excluded.cost_price, shares=excluded.shares,
                buy_date=excluded.buy_date, note=excluded.note,
                updated_at=datetime('now', '+8 hours')''',
            (int(user_id), pos.get('symbol', '').lower(), pos.get('name', ''),
             float(pos.get('cost_price', 0) or 0), int(pos.get('shares', 0) or 0),
             pos.get('buy_date', ''), pos.get('note', '')))
        conn.commit()
        conn.close()
        return True
    except Exception as error:
        print(f'保存持仓失败: {str(error)}')
        return False


def delete_user_position(symbol: str, user_id: int = 1) -> bool:
    """按 (user_id, symbol) 删除持仓 —— 只能删自己的"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM user_positions WHERE user_id = ? AND symbol = ?',
                       (int(user_id), symbol.lower()))
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        return deleted > 0
    except Exception as error:
        print(f'删除持仓失败: {str(error)}')
        return False


def save_position_trade_log(rec: Dict, user_id: int = 1) -> bool:
    """追加一条持仓流水。开关关掉或写入失败都只返回 False，绝不向上抛。"""
    try:
        from app.config import POSITION_TRADE_LOG_ENABLED
        if not POSITION_TRADE_LOG_ENABLED:
            return False
    except Exception:
        return False
    try:
        conn = get_db_conn()
        conn.execute('''INSERT INTO position_trade_log
            (trade_date, user_id, symbol, name, source, shares_before, price, qty,
             shares_after, cost_price_after, realized_pnl, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (rec.get('trade_date') or '', int(user_id), (rec.get('symbol') or '').lower(),
             rec.get('name', ''), rec.get('source', ''),
             rec.get('shares_before'), rec.get('price'), rec.get('qty'),
             rec.get('shares_after'), rec.get('cost_price_after'),
             rec.get('realized_pnl'), (rec.get('note') or '')[:200]))
        conn.commit()
        conn.close()
        return True
    except Exception as error:
        print(f'[流水] 写入失败（不影响持仓）: {str(error)}')
        return False


def load_position_trade_log(user_id: int = 1, symbol: str = '', limit: int = 200,
                            trade_only: bool = False) -> list:
    """读流水（只给回测与审计页用，不参与任何决策）。
    trade_only=True 只返回 adjust_*/create——回测默认要用这个口径，否则编辑历史会被当成买卖。"""
    try:
        conn = get_db_conn()
        sql = 'SELECT * FROM position_trade_log WHERE user_id = ?'
        args: list = [int(user_id)]
        if symbol:
            sql += ' AND symbol = ?'
            args.append(symbol.lower())
        if trade_only:
            sql += " AND source IN ('adjust_add','adjust_reduce','adjust_clear','create')"
        sql += ' ORDER BY id DESC LIMIT ?'
        args.append(int(limit))
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
        conn.close()
        return rows
    except Exception as error:
        print(f'读取持仓流水失败: {str(error)}')
        return []


def get_user_capital(user_id: int = 1) -> float:
    """读账号初始本金；未设置返回 0（调用方据此回退到旧的"市值/市值"口径）"""
    try:
        conn = get_db_conn()
        row = conn.execute('SELECT principal FROM user_capital WHERE user_id = ?',
                           (int(user_id),)).fetchone()
        conn.close()
        return float(row['principal'] or 0) if row else 0.0
    except Exception as error:
        print(f'读取本金失败: {str(error)}')
        return 0.0


def save_user_capital(principal: float, user_id: int = 1, note: str = '') -> bool:
    """写入初始本金（每个账号一行，重复保存为更新）"""
    try:
        conn = get_db_conn()
        conn.execute('''INSERT INTO user_capital (user_id, principal, note, updated_at)
            VALUES (?, ?, ?, datetime('now', '+8 hours'))
            ON CONFLICT(user_id) DO UPDATE SET
                principal=excluded.principal, note=excluded.note,
                updated_at=datetime('now', '+8 hours')''',
            (int(user_id), float(principal), note))
        conn.commit()
        conn.close()
        return True
    except Exception as error:
        print(f'保存本金失败: {str(error)}')
        return False


def get_user_realized_pnl(user_id: int = 1) -> float:
    """读账号累计已实现盈亏；无记录返回 0（从未减仓/清仓过）"""
    try:
        conn = get_db_conn()
        row = conn.execute('SELECT realized_pnl FROM user_realized_pnl WHERE user_id = ?',
                           (int(user_id),)).fetchone()
        conn.close()
        return round(float(row['realized_pnl'] or 0), 2) if row else 0.0
    except Exception as error:
        print(f'读取已实现盈亏失败: {str(error)}')
        return 0.0


def add_user_realized_pnl(delta: float, user_id: int = 1) -> float:
    """累计一笔已实现盈亏（减仓/清仓时调用），返回累计后的总值；失败返回 None"""
    if delta is None:
        return None
    try:
        conn = get_db_conn()
        conn.execute('''INSERT INTO user_realized_pnl (user_id, realized_pnl, trades, updated_at)
            VALUES (?, ?, 1, datetime('now', '+8 hours'))
            ON CONFLICT(user_id) DO UPDATE SET
                realized_pnl=ROUND(realized_pnl + excluded.realized_pnl, 2),
                trades=trades + 1,
                updated_at=datetime('now', '+8 hours')''',
            (int(user_id), round(float(delta), 2)))
        row = conn.execute('SELECT realized_pnl FROM user_realized_pnl WHERE user_id = ?',
                           (int(user_id),)).fetchone()
        conn.commit()
        conn.close()
        return round(float(row['realized_pnl'] or 0), 2) if row else None
    except Exception as error:
        print(f'累计已实现盈亏失败: {str(error)}')
        return None

