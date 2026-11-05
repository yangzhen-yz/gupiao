"""DAL domain: market."""

from ..dal_state import *

def load_latest_focus_plan() -> Dict:
    """最新一条收评的「明日重点关注」（行动台盘后/休市段的主数据源）。

    只取一行 + 只解析 focus_json，避免 load_daily_market_reviews 那样把最近 N 天
    收评的全部 JSON（板块/涨停/资金榜…）都解一遍。

    Returns:
        {'date': 'YYYY-MM-DD' | '', 'items': [...]}  # items 内含 buyTiming / tomorrowPlan
    """
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute('SELECT review_date, focus_json FROM daily_market_reviews '
                       'ORDER BY review_date DESC LIMIT 1')
        row = cursor.fetchone()
        conn.close()
        if not row:
            return {'date': '', 'items': []}
        items = []
        if 'focus_json' in row.keys() and row['focus_json']:
            try:
                items = json.loads(row['focus_json']) or []
            except Exception:
                items = []
        return {'date': row['review_date'] or '', 'items': items}
    except Exception as error:
        print(f'加载明日关注计划失败: {str(error)}')
        return {'date': '', 'items': []}


def load_daily_market_reviews() -> List[Dict]:
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute(f'SELECT * FROM daily_market_reviews ORDER BY review_date DESC LIMIT {QUERY_MARKET_REVIEWS_LIMIT}')
        rows = cursor.fetchall()
        conn.close()

        reviews = []
        for row in rows:
            review = {
                'date': row['review_date'],
                'timestamp': row['created_at'].isoformat() if row['created_at'] and hasattr(row['created_at'], 'isoformat') else (str(row['created_at']) if row['created_at'] else ''),
                'summary': row['summary'] or '',
                'market': json.loads(row['market_json']) if row['market_json'] else {},
            }
            # 兼容新旧格式
            if 'industry_json' in row.keys() and row['industry_json']:
                review['industrySectors'] = json.loads(row['industry_json'])
            else:
                review['industrySectors'] = []
            if 'concept_json' in row.keys() and row['concept_json']:
                review['conceptSectors'] = json.loads(row['concept_json'])
            else:
                review['conceptSectors'] = []
            if 'limit_up_json' in row.keys() and row['limit_up_json']:
                review['limitUpStocks'] = json.loads(row['limit_up_json'])
            else:
                review['limitUpStocks'] = []
            if 'limit_down_json' in row.keys() and row['limit_down_json']:
                review['limitDownStocks'] = json.loads(row['limit_down_json'])
            else:
                review['limitDownStocks'] = []
            if 'top_gainers_json' in row.keys() and row['top_gainers_json']:
                review['topGainers'] = json.loads(row['top_gainers_json'])
            else:
                review['topGainers'] = []
            if 'top_losers_json' in row.keys() and row['top_losers_json']:
                review['topLosers'] = json.loads(row['top_losers_json'])
            else:
                review['topLosers'] = []
            if 'focus_json' in row.keys() and row['focus_json']:
                review['tomorrowFocus'] = json.loads(row['focus_json'])
            else:
                review['tomorrowFocus'] = []
            # 明日关注的整篮子预期（2026-10-14）：旧数据没这一列，前端按 v-if 自动降级
            if 'focus_meta_json' in row.keys() and row['focus_meta_json']:
                try:
                    review['focusExpect'] = json.loads(row['focus_meta_json'])
                except Exception:
                    review['focusExpect'] = None
            if 'top_funds_json' in row.keys() and row['top_funds_json']:
                review['topFunds'] = json.loads(row['top_funds_json'])
            else:
                review['topFunds'] = []
            if 'ai_analysis_json' in row.keys() and row['ai_analysis_json']:
                try:
                    review['aiAnalysis'] = json.loads(row['ai_analysis_json'])
                except Exception:
                    review['aiAnalysis'] = None
            else:
                review['aiAnalysis'] = None
            # 短线情绪周期（2026-09-04 新增；历史数据无此字段时前端不渲染该区块）
            if 'sentiment_cycle_json' in row.keys() and row['sentiment_cycle_json']:
                try:
                    review['sentimentCycle'] = json.loads(row['sentiment_cycle_json'])
                except Exception:
                    review['sentimentCycle'] = None
            # 涨停/跌停真实总数（2026-07-30 补齐）；若缺失则降级回数组长度（历史数据）
            if 'limit_up_count' in row.keys() and row['limit_up_count'] is not None:
                review['limitUpCount'] = int(row['limit_up_count'])
            else:
                review['limitUpCount'] = len(review.get('limitUpStocks', []))
            if 'limit_down_count' in row.keys() and row['limit_down_count'] is not None:
                review['limitDownCount'] = int(row['limit_down_count'])
            else:
                review['limitDownCount'] = len(review.get('limitDownStocks', []))
            # 旧格式兼容
            if 'trend_json' in row.keys() and row['trend_json']:
                review['trendStocks'] = json.loads(row['trend_json'])
            if 'pool_json' in row.keys() and row['pool_json']:
                review['stockPool'] = json.loads(row['pool_json'])
            reviews.append(review)
        return reviews
    except Exception as error:
        print(f'加载收评失败: {str(error)}')
        return []


def save_daily_market_reviews(reviews: List[Dict]) -> bool:
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        for review in reviews:
            review_date = review.get('date', '')
            summary = review.get('summary', '')
            market_json = json.dumps(review.get('market', {}), ensure_ascii=False)
            industry_json = json.dumps(review.get('industrySectors', []), ensure_ascii=False)
            concept_json = json.dumps(review.get('conceptSectors', []), ensure_ascii=False)
            limit_up_json = json.dumps(review.get('limitUpStocks', []), ensure_ascii=False)
            limit_down_json = json.dumps(review.get('limitDownStocks', []), ensure_ascii=False)
            top_gainers_json = json.dumps(review.get('topGainers', []), ensure_ascii=False)
            top_losers_json = json.dumps(review.get('topLosers', []), ensure_ascii=False)
            focus_json = json.dumps(review.get('tomorrowFocus', []), ensure_ascii=False)
            # 整篮子预期（胜率/平均收益/被过滤掉几只）。不落库的话，前端刷新一次
            # 就再也算不出 dropped —— 候选总数在选完之后已经不在这份名单里了。
            _focus_expect = review.get('focusExpect')
            focus_meta_json = json.dumps(_focus_expect, ensure_ascii=False) if _focus_expect else None
            top_funds_json = json.dumps(review.get('topFunds', []), ensure_ascii=False)
            trend_json = json.dumps(review.get('trendStocks', {}), ensure_ascii=False)
            pool_json = json.dumps(review.get('stockPool', {}), ensure_ascii=False)
            ai_analysis_json = json.dumps(review.get('aiAnalysis'), ensure_ascii=False) if review.get('aiAnalysis') else None
            sentiment_cycle_json = json.dumps(review.get('sentimentCycle'), ensure_ascii=False) if review.get('sentimentCycle') else None
            # 真实涨停/跌停总数：优先读 count 字段，否则退化为数组长度（兼容旧数据）
            limit_up_count = review.get('limitUpCount')
            if limit_up_count is None:
                limit_up_count = len(review.get('limitUpStocks', []))
            limit_down_count = review.get('limitDownCount')
            if limit_down_count is None:
                limit_down_count = len(review.get('limitDownStocks', []))
            cursor.execute('''
                INSERT OR REPLACE INTO daily_market_reviews (review_date, summary, market_json, trend_json, pool_json, industry_json, concept_json, limit_up_json, limit_down_json, top_gainers_json, top_losers_json, focus_json, top_funds_json, ai_analysis_json, sentiment_cycle_json, focus_meta_json, limit_up_count, limit_down_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (review_date, summary, market_json, trend_json, pool_json, industry_json, concept_json, limit_up_json, limit_down_json, top_gainers_json, top_losers_json, focus_json, top_funds_json, ai_analysis_json, sentiment_cycle_json, focus_meta_json, limit_up_count, limit_down_count))
        conn.commit()
        conn.close()
        return True
    except Exception as error:
        print(f'保存收评失败: {str(error)}')
        import traceback
        print(f'[收评保存] 错误堆栈: {traceback.format_exc()}')
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def load_kline_cache(symbol: str, trade_date: str) -> List:
    """加载某股票指定交易日的日线历史缓存

    Args:
        symbol: 小写带前缀 sh600519
        trade_date: 交易日 YYYY-MM-DD
    Returns:
        成功返回日线列表（同 get_kline_data 的 qfqday 结构），未命中返回 None
    """
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT kline_json FROM kline_cache WHERE symbol = ? AND trade_date = ?',
            (symbol, trade_date))
        row = cursor.fetchone()
        conn.close()
        if row and row['kline_json']:
            return json.loads(row['kline_json'])
        return None
    except Exception as _e:
        print(f'[数据库] 加载K线缓存失败: {_e}')
        return None


def save_kline_cache(symbol: str, trade_date: str, kline: List, updated_at: str = '') -> bool:
    """保存某股票指定交易日的日线历史缓存（UPSERT 语义）

    Args:
        symbol: 小写带前缀 sh600519
        trade_date: 交易日 YYYY-MM-DD
        kline: 日线列表
    """
    if not kline:
        return False
    try:
        from datetime import datetime
        updated_at = updated_at or datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn = get_db_conn(fast_fail=True)
        cursor = conn.cursor()
        cursor.execute('''INSERT OR REPLACE INTO kline_cache
            (symbol, trade_date, kline_json, updated_at)
            VALUES (?, ?, ?, ?)''',
            (symbol, trade_date, json.dumps(kline, ensure_ascii=False), updated_at))
        conn.commit()
        conn.close()
        return True
    except Exception as _e:
        print(f'[数据库] 保存K线缓存失败: {_e}')
        return False


def save_cross_check(date_key: str, rows: List[Dict]) -> int:
    """保存某交易日双源比对结果（UPSERT，authoritative=westock）。"""
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
            # 三态：True→1 / False→0 / None（未比对）→ NULL（不与"不一致"混淆）
            qg = r.get('quote_agreed')
            qg_val = None if qg is None else (1 if qg else 0)
            dc = r.get('direction_conflict')
            dc_val = None if dc is None else (1 if dc else 0)
            conn.execute('''
                INSERT INTO data_cross_check
                    (date, symbol, name, our_change_pct, westock_change_pct, quote_agreed,
                     our_main_inflow, westock_main_inflow, inflow_diff, inflow_diff_pct,
                     direction_conflict, authoritative, checked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'westock', ?)
                ON CONFLICT(date, symbol) DO UPDATE SET
                    name=excluded.name, our_change_pct=excluded.our_change_pct,
                    westock_change_pct=excluded.westock_change_pct, quote_agreed=excluded.quote_agreed,
                    our_main_inflow=excluded.our_main_inflow,
                    westock_main_inflow=excluded.westock_main_inflow,
                    inflow_diff=excluded.inflow_diff, inflow_diff_pct=excluded.inflow_diff_pct,
                    direction_conflict=excluded.direction_conflict,
                    authoritative='westock', checked_at=excluded.checked_at''',
                         (date_key, sym, r.get('name'), r.get('our_change_pct'),
                          r.get('westock_change_pct'), qg_val,
                          r.get('our_main_inflow'), r.get('westock_main_inflow'),
                          r.get('inflow_diff'), r.get('inflow_diff_pct'),
                          dc_val, now))
            n += 1
        conn.commit()
        return n
    except Exception as e:
        print(f'[数据库] 保存双源比对失败: {e}')
        return 0
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def load_cross_check(date_key: str) -> List[Dict]:
    """读取某交易日双源比对结果（无记录返回 []）。"""
    conn = None
    try:
        conn = get_db_conn()
        rows = conn.execute(
            'SELECT * FROM data_cross_check WHERE date = ?', (date_key,)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f'[数据库] 读取双源比对失败: {e}')
        return []
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def save_optimization_history(module: str, parameter_set: str = '',
                              metric_name: str = '', metric_value: float = 0,
                              old_value: float = None, new_value: float = None,
                              details: str = '',
                              metric_v2_name: str = '', metric_v2_value: float = None,
                              objective_version: str = 'v1',
                              walk_forward: int = 0,
                              backfill_note: str = '') -> bool:
    """保存优化历史

    2026-11-01（T1.3）：旧 metric 列继续写（审计用），新口径写
    metric_v2_name/metric_v2_value；objective_version 标明口径版本，
    方便回滚后区分同一张表里的两类记录。
    """
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        _ensure_optimizer_v2_columns(cursor)
        cursor.execute('''
            INSERT INTO optimization_history (module, parameter_set, metric_name, metric_value,
                                              old_value, new_value, details,
                                              metric_v2_name, metric_v2_value, objective_version,
                                              walk_forward, backfill_note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (module, parameter_set, metric_name, metric_value,
              old_value, new_value, details,
              metric_v2_name, metric_v2_value, objective_version,
              int(walk_forward or 0), backfill_note))
        conn.commit()
        conn.close()
        return True
    except Exception as error:
        print(f'保存优化历史失败: {str(error)}')
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def load_optimization_history(module: str = None, limit: int = 50) -> List[Dict]:
    """加载优化历史"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        if module:
            cursor.execute(
                'SELECT * FROM optimization_history WHERE module = ? ORDER BY created_at DESC LIMIT ?',
                (module, limit)
            )
        else:
            cursor.execute(
                'SELECT * FROM optimization_history ORDER BY created_at DESC LIMIT ?',
                (limit,)
            )
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as error:
        print(f'加载优化历史失败: {str(error)}')
        return []


def save_theme_member_daily(rows: List[Dict], theme: str = 'robot') -> int:
    """主题篮子成分日线 UPSERT（2026-10-31）。

    rows 每项：{symbol, date, name, open, high, low, close, prev_close, amount, volume, source}
    只写盘后快照，不做全量回补；返回成功写入条数。
    """
    if not rows:
        return 0
    try:
        from datetime import datetime
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn = get_db_conn()
        cursor = conn.cursor()
        cursor.executemany('''
            INSERT INTO theme_member_daily
                (theme, symbol, date, name, open, high, low, close, prev_close,
                 amount, volume, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(theme, symbol, date) DO UPDATE SET
                name=excluded.name, open=excluded.open, high=excluded.high,
                low=excluded.low, close=excluded.close, prev_close=excluded.prev_close,
                amount=excluded.amount, volume=excluded.volume,
                source=excluded.source, created_at=excluded.created_at''',
            [(theme, r.get('symbol'), r.get('date'), r.get('name'),
              r.get('open'), r.get('high'), r.get('low'), r.get('close'), r.get('prev_close'),
              r.get('amount'), r.get('volume'), r.get('source') or 'tencent_quote', now)
             for r in rows if r.get('symbol') and r.get('date')])
        conn.commit()
        conn.close()
        return len(rows)
    except Exception as error:
        print(f'[数据库] 保存主题篮子日线失败: {str(error)}')
        return 0


def load_theme_member_daily(theme: str = 'robot', since: str = '') -> List[Dict]:
    """读取主题篮子成分日线（按日期升序）。since 为空 = 全部。"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        if since:
            rows = cursor.execute(
                'SELECT * FROM theme_member_daily WHERE theme = ? AND date >= ? '
                'ORDER BY symbol, date', (theme, since)).fetchall()
        else:
            rows = cursor.execute(
                'SELECT * FROM theme_member_daily WHERE theme = ? ORDER BY symbol, date',
                (theme,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as error:
        print(f'[数据库] 读取主题篮子日线失败: {str(error)}')
        return []


def save_cls_pool_daily(day: str, rows: List[Dict]) -> int:
    """财联社四池逐票落库（2026-10-31，影子）。按 (trade_date, pool, symbol) 覆盖写。

    rows 每项字段与 services/ops/cls_pool._normalize_pool 一致：
    pool/symbol/code/name/changePct/lastPx/sealTime/days/isSt/reason/themes/plates/numStocks。
    失败只打印、不抛：这是影子数据，接口抖动不应该影响收盘任务的其他环节。
    """
    if not rows:
        return 0
    try:
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        data = []
        for r in rows:
            if not (r.get('pool') and r.get('symbol')):
                continue
            data.append((
                day, r.get('pool'), r.get('symbol'), r.get('code'), r.get('name'),
                r.get('changePct'), r.get('lastPx'), r.get('sealTime'), r.get('days'),
                int(r.get('isSt') or 0), r.get('reason'),
                json.dumps(r.get('themes') or [], ensure_ascii=False),
                json.dumps(r.get('plates') or [], ensure_ascii=False),
                r.get('numStocks'), now,
            ))
        if not data:
            return 0
        conn = get_db_conn()
        try:
            conn.executemany('''
                INSERT INTO cls_pool_daily
                    (trade_date, pool, symbol, code, name, change_pct, last_px, seal_time,
                     days, is_st, reason, themes, plates, num_stocks, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'cls', ?)
                ON CONFLICT(trade_date, pool, symbol) DO UPDATE SET
                    code=excluded.code, name=excluded.name, change_pct=excluded.change_pct,
                    last_px=excluded.last_px, seal_time=excluded.seal_time, days=excluded.days,
                    is_st=excluded.is_st, reason=excluded.reason, themes=excluded.themes,
                    plates=excluded.plates, num_stocks=excluded.num_stocks,
                    created_at=excluded.created_at''', data)
            conn.commit()
        finally:
            conn.close()
        return len(data)
    except Exception as error:
        print(f'[数据库] 保存财联社四池失败 day={day}: {str(error)}')
        return 0


def save_cls_plate_daily(day: str, rows: List[Dict]) -> int:
    """财联社板块榜日快照（2026-10-31，展示兜底）。按 (trade_date, plate_type, code) 覆盖写。

    rows 每项由 services/ops/cls_plate.sync_today 组装：plateType/code/name/changePct/
    mainFundDiff/upCount/downCount/limitUpNum/limitDownNum + firstStockJson。
    失败只打印、不抛：这是兜底展示数据，接口抖动不应该影响收盘任务。
    """
    if not rows:
        return 0
    try:
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        data = []
        for r in rows:
            if not (r.get('plateType') and r.get('code')):
                continue
            first = r.get('firstStockJson')
            if first is None and r.get('firstStock'):
                first = json.dumps(r.get('firstStock'), ensure_ascii=False)
            data.append((
                day, r.get('plateType'), r.get('code'), r.get('name'),
                r.get('changePct'), r.get('mainFundDiff'), r.get('upCount'),
                r.get('downCount'), r.get('limitUpNum'), r.get('limitDownNum'),
                first, now,
            ))
        if not data:
            return 0
        conn = get_db_conn()
        try:
            conn.executemany('''
                INSERT INTO cls_plate_daily
                    (trade_date, plate_type, code, name, change_pct, main_fund_diff,
                     up_count, down_count, limit_up_num, limit_down_num, first_stock,
                     source, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'cls', ?)
                ON CONFLICT(trade_date, plate_type, code) DO UPDATE SET
                    name=excluded.name, change_pct=excluded.change_pct,
                    main_fund_diff=excluded.main_fund_diff, up_count=excluded.up_count,
                    down_count=excluded.down_count, limit_up_num=excluded.limit_up_num,
                    limit_down_num=excluded.limit_down_num, first_stock=excluded.first_stock,
                    created_at=excluded.created_at''', data)
            conn.commit()
        finally:
            conn.close()
        return len(data)
    except Exception as error:
        print(f'[数据库] 保存财联社板块失败 day={day}: {str(error)}')
        return 0


def save_theme_chain_daily(day: str, chains: List[Dict]) -> int:
    """动态热门板块链落库（2026-10-31）。**整日替换**，不是增量覆盖。

    chains 每项由 services/ops/hot_chains 组装：chain_id/rank/name/plateCode/
    plateName/changePct/limitUpNum/breadth/members/note。失败只打印不抛。

    为什么整日删再插：同一天可能重建多次（盘中跟随、重叠板块被踢掉、收盘重算），
    UPSERT 会把已淘汰的板块留在表里，重启后 `load_theme_chain_daily` 又把它读回来，
    等于"页面上多出一条今天已经不该展示的链"（10-31 实测发生过：酿酒被重叠过滤后
    仍以 rank=2 留在库中）。当日表就是"当前生效的链"，所以按天全量覆盖。
    """
    if not chains:
        return 0
    try:
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        data = []
        for c in chains:
            members = c.get('members') or []
            data.append((
                day, c.get('chain_id') or c.get('id'), c.get('rank'),
                c.get('name'), c.get('plateCode'), c.get('plateName'),
                c.get('changePct'), c.get('limitUpNum'), c.get('breadth'),
                len(members), json.dumps(members, ensure_ascii=False),
                c.get('note'), now,
            ))
        conn = get_db_conn()
        try:
            conn.execute('DELETE FROM theme_chain_daily WHERE trade_date = ?', (day,))
            conn.executemany('''
                INSERT INTO theme_chain_daily
                    (trade_date, chain_id, rank, name, plate_code, plate_name,
                     change_pct, limit_up_num, breadth, member_count, members_json,
                     note, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'cls', ?)''', data)
            conn.commit()
        finally:
            conn.close()
        return len(data)
    except Exception as error:
        print(f'[数据库] 保存动态板块链失败 day={day}: {str(error)}')
        return 0


def load_theme_chain_daily(day: str = '') -> List[Dict]:
    """读动态板块链。day 为空时取最近一个有数据的一天（重启后先显示上次的链）。"""
    try:
        conn = get_db_conn()
        cursor = conn.cursor()
        if not day:
            row = cursor.execute(
                'SELECT MAX(trade_date) FROM theme_chain_daily').fetchone()
            day = (row[0] if row else '') or ''
            if not day:
                conn.close()
                return []
        rows = cursor.execute(
            'SELECT * FROM theme_chain_daily WHERE trade_date = ? ORDER BY rank',
            (day,)).fetchall()
        conn.close()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d['members'] = json.loads(d.get('members_json') or '[]')
            except Exception:
                d['members'] = []
            out.append(d)
        return out
    except Exception as error:
        print(f'[数据库] 读取动态板块链失败: {str(error)}')
        return []


def load_cls_pool_daily(day: str) -> Dict[str, List[Dict]]:
    """读某天财联社四池，按池名分组；字段名与 _normalize_pool 对齐。"""
    out: Dict[str, List[Dict]] = {}
    try:
        conn = get_db_conn()
        try:
            rows = conn.execute(
                'SELECT * FROM cls_pool_daily WHERE trade_date=? ORDER BY pool, days DESC, seal_time',
                (day,)).fetchall()
        finally:
            conn.close()
        for r in rows:
            d = dict(r)
            try:
                themes = json.loads(d.get('themes') or '[]')
            except Exception:
                themes = []
            try:
                plates = json.loads(d.get('plates') or '[]')
            except Exception:
                plates = []
            out.setdefault(d['pool'], []).append({
                'pool': d['pool'], 'symbol': d['symbol'], 'code': d.get('code'),
                'name': d.get('name'), 'changePct': d.get('change_pct'),
                'lastPx': d.get('last_px'), 'sealTime': d.get('seal_time'),
                'days': d.get('days'), 'isSt': d.get('is_st'),
                'reason': d.get('reason'), 'themes': themes, 'plates': plates,
                'numStocks': d.get('num_stocks'),
            })
        return out
    except Exception as error:
        print(f'[数据库] 读取财联社四池失败 day={day}: {str(error)}')
        return {}

