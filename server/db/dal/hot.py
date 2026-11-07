"""DAL domain: hot."""

from ..dal_state import *

def load_hot_stock_buttons(user_id: Optional[int] = None) -> List[Dict]:
    """快捷查询自选（前端 chips）。

    user_id=None → 所有用户的并集（调度器语义：扫描宇宙覆盖每个人关注的票）
    user_id=具体 → 该用户的 chips
    """
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        if user_id is None:
            cursor.execute('SELECT code, MAX(name) AS name FROM hot_stock_buttons '
                           'GROUP BY code ORDER BY MIN(id)')
        else:
            cursor.execute('SELECT code, name FROM hot_stock_buttons WHERE user_id = ? ORDER BY id',
                           (int(user_id),))
        rows = cursor.fetchall()
        conn.close()
        if rows:
            return [{'code': row['code'], 'name': row['name']} for row in rows]
        # 仅在全库为空（全新安装、第一次有人打开）时播下默认自选。
        # 用户自己清空过就不再自动加回——否则"删干净的列表"会被系统偷偷填满。
        if user_id is not None:
            conn = get_db_conn()
            total = conn.execute('SELECT COUNT(*) AS n FROM hot_stock_buttons').fetchone()['n']
            conn.close()
            if total == 0:
                save_hot_stock_buttons(_DEFAULT_HOT_STOCKS, user_id=user_id)
                return list(_DEFAULT_HOT_STOCKS)
        return []
    except Exception as error:
        print(f'加载热门股票失败: {str(error)}')
        return []


def save_hot_stock_buttons(stocks: List[Dict], user_id: int = 1) -> bool:
    """整列表覆盖写入（只动该用户的行）"""
    conn = None
    try:
        conn = get_db_conn(wait_for_lock=True)
        cursor = conn.cursor()
        cursor.execute('DELETE FROM hot_stock_buttons WHERE user_id = ?', (int(user_id),))
        for stock in stocks:
            if isinstance(stock, dict) and stock.get('code') and stock.get('name'):
                cursor.execute(
                    'INSERT OR IGNORE INTO hot_stock_buttons (user_id, code, name) VALUES (?, ?, ?)',
                    (int(user_id), stock['code'].lower(), stock['name'])
                )
        conn.commit()
        return True
    except Exception as error:
        print(f'保存热门股票失败: {str(error)}')
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def load_stock_concept_tags() -> Dict[str, List[str]]:
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute('SELECT symbol, tags_json FROM stock_concept_tags')
        rows = cursor.fetchall()
        conn.close()
        if rows:
            return {row['symbol']: json.loads(row['tags_json']) for row in rows if row['tags_json']}
        # 首次加载，初始化默认标签到数据库
        save_stock_concept_tags(DEFAULT_STOCK_TAGS)
        return DEFAULT_STOCK_TAGS.copy()
    except Exception as error:
        print(f'加载股票标签失败: {str(error)}')
        return DEFAULT_STOCK_TAGS.copy()


def save_stock_concept_tags(tags: Dict[str, List[str]]) -> bool:
    try:
        conn = get_db_conn(wait_for_lock=True)
        cursor = conn.cursor()
        cursor.execute('DELETE FROM stock_concept_tags')
        for symbol, tag_list in tags.items():
            cursor.execute(
                'INSERT INTO stock_concept_tags (symbol, tags_json) VALUES (?, ?)',
                (symbol, json.dumps(tag_list, ensure_ascii=False))
            )
        conn.commit()
        conn.close()
        return True
    except Exception as error:
        print(f'保存股票标签失败: {str(error)}')
        return False


def add_stock_concept_tags(symbol: str, new_tags: List[str]) -> List[str]:
    """添加标签到指定股票，返回更新后的标签列表"""
    tags = load_stock_concept_tags()
    symbol_lower = symbol.lower()
    
    if symbol_lower not in tags:
        tags[symbol_lower] = []
    
    for tag in new_tags:
        tag_clean = tag.strip()
        if tag_clean and tag_clean not in tags[symbol_lower]:
            tags[symbol_lower].append(tag_clean)
    
    save_stock_concept_tags(tags)
    return tags[symbol_lower]


def remove_stock_tag(symbol: str, tag: str) -> List[str]:
    """从指定股票移除标签，返回更新后的标签列表"""
    tags = load_stock_concept_tags()
    symbol_lower = symbol.lower()
    
    if symbol_lower in tags:
        tag_clean = tag.strip()
        if tag_clean in tags[symbol_lower]:
            tags[symbol_lower].remove(tag_clean)
        if len(tags[symbol_lower]) == 0:
            del tags[symbol_lower]
    
    save_stock_concept_tags(tags)
    return tags.get(symbol_lower, [])


def save_fund_flow_snapshot(snap_key: str, data) -> bool:
    """保存资金流向某区块（个股/板块/非热点上涨股）最后成功快照（UPSERT）。

    用途：服务重启会清空内存兜底 _LAST_GOOD_*，持久化后重启仍可回退到最后成功数据，
    覆盖「收盘后东财 push2 风控拒连 → 页面全空」的场景（收盘后数据本应保持不变）。
    """
    if not data:
        return False
    conn = None
    try:
        from datetime import datetime
        # 2026-11-03 从 fast_fail 摘回 30 秒：这是"最后成功快照"，写失败的代价不是"这次没缓存上"，
        # 而是让 fundflow 的 fresh_snap 判假 → 收盘后每次重走新浪补算（实测重启后首次 8.99 秒）。
        # 11-03 15:20~19:46 写锁被占死那段时间快照全没落库，就是这个后果的现场。
        conn = get_db_conn()
        conn.execute('''INSERT INTO fund_flow_snapshot (snap_key, snap_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(snap_key) DO UPDATE SET
                snap_json=excluded.snap_json, updated_at=excluded.updated_at''',
            (snap_key, json.dumps(data, ensure_ascii=False),
             datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        return True
    except Exception as _e:
        print(f'[数据库] 保存资金流向快照失败({snap_key}): {_e}')
        return False
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def load_fund_flow_snapshot(snap_key: str):
    """读取资金流向某区块的最后成功快照，无记录/解析失败返回 None。"""
    conn = None
    try:
        conn = get_db_conn()
        row = conn.execute(
            'SELECT snap_json FROM fund_flow_snapshot WHERE snap_key = ?', (snap_key,)).fetchone()
        return json.loads(row['snap_json']) if row else None
    except Exception as _e:
        print(f'[数据库] 读取资金流向快照失败({snap_key}): {_e}')
        return None
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def save_westock_snapshot(date_key: str, rows: List[Dict]) -> int:
    """保存某交易日 westock 快照（逐只 UPSERT，按 date+symbol 幂等）。

    rows: [{'symbol','name','close_price','change_pct','main_net_inflow',...}]
    返回写入条数。
    """
    from datetime import datetime
    if not rows:
        return 0
    conn = None
    try:
        conn = get_db_conn(fast_fail=True)
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        n = 0
        for r in rows:
            sym = (r.get('symbol') or '').lower()
            if not sym:
                continue
            conn.execute('''
                INSERT INTO westock_snapshot
                    (date, symbol, name, close_price, change_pct, main_net_inflow,
                     jumbo_net_flow, mid_net_flow, retail_net_flow,
                     main_net_5d, main_net_10d, main_net_20d, industry_rank, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date, symbol) DO UPDATE SET
                    name=excluded.name, close_price=excluded.close_price,
                    change_pct=excluded.change_pct, main_net_inflow=excluded.main_net_inflow,
                    jumbo_net_flow=excluded.jumbo_net_flow, mid_net_flow=excluded.mid_net_flow,
                    retail_net_flow=excluded.retail_net_flow,
                    main_net_5d=excluded.main_net_5d, main_net_10d=excluded.main_net_10d,
                    main_net_20d=excluded.main_net_20d, industry_rank=excluded.industry_rank,
                    fetched_at=excluded.fetched_at''',
                         (date_key, sym, r.get('name'), r.get('close_price'),
                          r.get('change_pct'), r.get('main_net_inflow'),
                          r.get('jumbo_net_flow'), r.get('mid_net_flow'),
                          r.get('retail_net_flow'), r.get('main_net_5d'),
                          r.get('main_net_10d'), r.get('main_net_20d'),
                          r.get('industry_rank'), now))
            n += 1
        conn.commit()
        return n
    except Exception as e:
        print(f'[数据库] 保存 westock 快照失败: {e}')
        return 0
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def load_westock_snapshot(date_key: str) -> List[Dict]:
    """读取某交易日 westock 快照（无记录返回 []）。"""
    conn = None
    try:
        conn = get_db_conn()
        rows = conn.execute(
            'SELECT * FROM westock_snapshot WHERE date = ?', (date_key,)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f'[数据库] 读取 westock 快照失败: {e}')
        return []
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def load_hot_search_ranking() -> Dict:
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        # 读取元数据
        cursor.execute('SELECT meta_key, meta_value FROM hot_search_meta')
        meta_rows = cursor.fetchall()
        meta = {row['meta_key']: row['meta_value'] for row in meta_rows}
        # 读取股票数据
        cursor.execute('SELECT * FROM hot_search_ranking ORDER BY sort_order ASC')
        stock_rows = cursor.fetchall()
        conn.close()

        stocks = []
        for row in stock_rows:
            stocks.append({
                'symbol': row['symbol'],
                'code': row['code'],
                'name': row['name'],
                'market': row['market'],
                'price': row['price'],
                'changePercent': row['change_percent'],
                'change': row['change_val'],
                'hotRank': row['hot_rank'] if 'hot_rank' in row.keys() else 0
            })

        return {
            'updateTime': meta.get('updateTime', ''),
            'total': int(meta.get('total', 0)),
            'filteredCount': int(meta.get('filteredCount', 0)),
            'source': meta.get('source', ''),
            'stocks': stocks
        }
    except Exception as error:
        print(f'加载hot_search_ranking数据失败: {str(error)}')
        return {'updateTime': '', 'total': 0, 'filteredCount': 0, 'stocks': []}


def save_hot_search_ranking(data: Dict) -> bool:
    try:
        conn = get_db_conn(fast_fail=True)
        cursor = conn.cursor()
        # 2026-10-23：熔断/降级时抓不到任何榜单，不能拿空结果覆盖上一份可用缓存。
        # 否则封禁期一次服务重启（启动时会拉热搜）就会把当日榜单清成 0 只，
        # 连当日快照也一起删掉——数据看起来"更新成功"，实际是把可用数据擦掉。
        if not (data.get('stocks') or []):
            existing = cursor.execute('SELECT COUNT(*) FROM hot_search_ranking').fetchone()[0]
            if existing:
                conn.close()
                print(f'[热搜榜] 本轮结果为空（熔断/降级），保留已有 {existing} 只缓存，'
                      f'不覆盖也不删当日快照')
                return False
        cursor.execute('DELETE FROM hot_search_ranking')
        cursor.execute('DELETE FROM hot_search_meta')
        for key, value in [('updateTime', data.get('updateTime', '')),
                           ('total', str(data.get('total', 0))),
                           ('filteredCount', str(data.get('filteredCount', 0))),
                           ('source', str(data.get('source', '') or ''))]:
            cursor.execute(
                'INSERT INTO hot_search_meta (meta_key, meta_value) VALUES (?, ?)',
                (key, value)
            )
        for idx, stock in enumerate(data.get('stocks', [])):
            cursor.execute('''
                INSERT INTO hot_search_ranking (symbol, code, name, market, price, change_percent, change_val, hot_rank, sort_order)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                stock.get('symbol', ''),
                stock.get('code', ''),
                stock.get('name', ''),
                stock.get('market', ''),
                stock.get('price', 0),
                stock.get('changePercent', 0),
                stock.get('change', 0),
                stock.get('hotRank', 0),
                idx
            ))
        today_str = date.today().isoformat()
        try:
            cursor.execute('DELETE FROM hot_search_daily_snapshot WHERE snapshot_date = ?', (today_str,))
            for stock in data.get('stocks', []):
                if stock.get('hotRank', 0) <= HOT_SEARCH_TOP_N:
                    cursor.execute('''
                        INSERT OR REPLACE INTO hot_search_daily_snapshot (snapshot_date, symbol, name, hot_rank)
                        VALUES (?, ?, ?, ?)
                    ''', (today_str, stock.get('symbol', ''), stock.get('name', ''), stock.get('hotRank', 0)))
        except Exception as snap_err:
            print(f'保存热搜快照失败(非致命): {snap_err}')
        cutoff = (date.today() - timedelta(days=HOT_SEARCH_SNAPSHOT_CLEANUP_DAYS)).isoformat()
        try:
            cursor.execute('DELETE FROM hot_search_daily_snapshot WHERE snapshot_date < ?', (cutoff,))
        except Exception:
            pass
        conn.commit()
        conn.close()
        _db_cache_invalidate('hot_search_ranking')
        return True
    except Exception as error:
        print(f'保存hot_search_ranking数据失败: {str(error)}')
        return False

