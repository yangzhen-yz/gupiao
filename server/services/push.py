"""services/push.py - 企业微信选股推送（盘中准实时）

2026-09-01 新增：
- 盘中每 1 分钟扫描用户池，满足条件的候选股（评分高 + 龙头/中军 + 未涨停 + 预期涨幅够 + 有量能）
  通过 wecom-cli 以机器人身份推送到授权人企业微信（markdown 格式）。
- 去重：同一只股票当天首次达标才推，之后不再重复推送。
- 开盘前 9:25 推送"今日关注池"预览（评分达标 + 非跟风/杂毛）。

依赖：
- wecom-cli（企业微信 CLI，已在 PATH，位于 connector_runtime artifacts）
- 本机 FastAPI 服务 /api/hot-stocks-scan（复用完整评分链路，pool_only=1 仅扫用户池）
"""
import json
import subprocess
import urllib.request
from datetime import datetime

from app.config import (WECOM_PUSH_ENABLED, WECOM_CLI_CHAT_ID, WECOM_PUSH_MIN_SCORE,
                        WECOM_PUSH_MIN_GAIN, WECOM_PUSH_MIN_VOL_RATIO, WECOM_PUSH_MAX_CHANGE,
                        WECOM_PUSH_EXCLUDE_ROLES)
from db.database import load_user_scan_pool, get_db_conn

SCAN_API_BASE = 'http://127.0.0.1:3003/api/hot-stocks-scan'


# ---------- 发送通道 ----------

def push_wecom_markdown(content: str) -> bool:
    """通过 wecom-cli 以机器人身份向授权人发送 markdown 消息。返回是否成功。"""
    if not WECOM_PUSH_ENABLED:
        return False
    payload = {
        'chat_id': WECOM_CLI_CHAT_ID,
        'msg_type': 'markdown',
        'markdown': {'content': content},
    }
    try:
        proc = subprocess.run(
            ['wecom-cli', 'message', 'aibot', 'send', '--json', json.dumps(payload, ensure_ascii=False)],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30,
        )
        out = f'{proc.stdout or ""}\n{proc.stderr or ""}'
        if '"success": true' in out:
            return True
        print(f'[推送] wecom-cli 发送失败 rc={proc.returncode}: {out[:300]}')
        return False
    except Exception as e:
        print(f'[推送] wecom-cli 调用异常: {e}')
        return False


# ---------- 扫描与过滤 ----------

def _load_pool_symbols() -> list:
    try:
        return load_user_scan_pool()
    except Exception as e:
        print(f'[推送] 加载用户池失败: {e}')
        return []


def _scan_pool() -> list:
    """内部 HTTP 调本机扫描 API（pool_only=1 仅扫用户池），复用完整评分/角色链路。"""
    pool = _load_pool_symbols()
    if not pool:
        return []
    url = f'{SCAN_API_BASE}?codes={",".join(pool)}&pool_only=1'
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f'[推送] 扫描API调用失败: {e}')
        return []


def _expected_gain(stock) -> float | None:
    """预期涨幅 % = (卖出参考价 - 现价) / 现价 * 100，源自 buySellPoints.sell。"""
    bsp = stock.get('buySellPoints') or {}
    try:
        sell = float(bsp.get('sell'))
        price = float(stock.get('price'))
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    return (sell - price) / price * 100


def _pass_filter(stock) -> bool:
    """是否满足推送条件（与用户确认方案）：
    评分≥阈值 + 角色非跟风/杂毛 + 未涨停 + 预期涨幅≥阈值 + 量比≥阈值。
    """
    try:
        score = float(stock.get('score') or 0)
    except (TypeError, ValueError):
        return False
    if score < WECOM_PUSH_MIN_SCORE:
        return False

    role = (stock.get('buySellPoints') or {}).get('stockType') or ''
    if any(kw in role for kw in WECOM_PUSH_EXCLUDE_ROLES):
        return False

    try:
        change = float(stock.get('changePercent') or 0)
    except (TypeError, ValueError):
        change = 0.0
    if change >= WECOM_PUSH_MAX_CHANGE:
        return False  # 已涨停，不推（无买入空间）

    gain = _expected_gain(stock)
    if gain is None or gain < WECOM_PUSH_MIN_GAIN:
        return False

    try:
        vr = float(stock.get('volumeRatio') or 0)
    except (TypeError, ValueError):
        vr = 0.0
    if vr < WECOM_PUSH_MIN_VOL_RATIO:
        return False
    return True


# ---------- 去重（当天每只票只推一次） ----------

def _ensure_table():
    try:
        conn = get_db_conn()
        conn.execute('''CREATE TABLE IF NOT EXISTS push_sent_log (
            push_date TEXT NOT NULL,
            symbol   TEXT NOT NULL,
            pushed_at TEXT NOT NULL,
            PRIMARY KEY (push_date, symbol)
        )''')
        conn.commit()
        conn.close()
    except Exception as e:
        print(f'[推送] 初始化去重表失败: {e}')


def _already_pushed(date_str: str, symbol: str) -> bool:
    try:
        conn = get_db_conn()
        row = conn.execute(
            'SELECT 1 FROM push_sent_log WHERE push_date=? AND symbol=?', (date_str, symbol)
        ).fetchone()
        conn.close()
        return row is not None
    except Exception:
        return False


def _mark_pushed(date_str: str, symbol: str):
    try:
        conn = get_db_conn()
        conn.execute(
            'INSERT OR IGNORE INTO push_sent_log (push_date, symbol, pushed_at) VALUES (?,?,?)',
            (date_str, symbol, datetime.now().strftime('%Y-%m-%d %H:%M:%S')),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f'[推送] 记录去重失败: {e}')


# ---------- 推送内容格式化 ----------

def _fmt_markdown(stocks: list, title: str) -> str:
    lines = [f'**{title}**', '', f'扫描时间 {datetime.now():%H:%M:%S}，共 {len(stocks)} 只达标：', '']
    for i, s in enumerate(stocks, 1):
        name = s.get('name', '')
        code = s.get('code', '')
        price = s.get('price', 0)
        change = s.get('changePercent', 0)
        score = s.get('score', 0)
        role = (s.get('buySellPoints') or {}).get('stockType', '')
        gain = _expected_gain(s)
        vr = s.get('volumeRatio', 0)
        lines.append(f'{i}. **{name}**（{code}）')
        lines.append(f'   💰 现价 {price}｜涨幅 {change}%｜评分 {score}｜{role}')
        lines.append(f'   🎯 预期涨幅 {gain:.1f}%｜量比 {vr:.1f}')
    return '\n'.join(lines)


# ---------- 任务入口 ----------

async def push_candidates_task():
    """盘中推送主任务：扫描用户池 → 过滤 → 去重 → 推送。失败不抛异常（由 scheduler 兜底）。"""
    if not WECOM_PUSH_ENABLED:
        return
    try:
        stocks = _scan_pool()
        if not stocks:
            print(f'[推送 {datetime.now():%H:%M:%S}] 扫描无结果，跳过')
            return

        today = datetime.now().strftime('%Y-%m-%d')
        _ensure_table()
        hits = []
        for s in stocks:
            sym = s.get('symbol', '')
            if not _pass_filter(s):
                continue
            if _already_pushed(today, sym):
                continue
            hits.append(s)

        if not hits:
            print(f'[推送 {datetime.now():%H:%M:%S}] 无新达标候选（扫描 {len(stocks)} 只）')
            return

        content = _fmt_markdown(hits, '⚡ 选股推送（达标即推）')
        if push_wecom_markdown(content):
            for s in hits:
                _mark_pushed(today, s.get('symbol', ''))
            print(f'[推送 {datetime.now():%H:%M:%S}] 已推送 {len(hits)} 只: '
                  f'{", ".join(s.get("name", "") for s in hits)}')
    except Exception as e:
        print(f'[推送 {datetime.now():%H:%M:%S}] 推送任务异常: {e}')
        import traceback
        print(f'  堆栈: {traceback.format_exc()[:300]}')


async def push_daily_preview_task():
    """开盘前 9:25 推送"今日关注池"预览：评分≥阈值 且 非跟风/杂毛（竞价阶段涨幅/量比参考意义弱，放宽）。
    预览不占用去重名额，盘中达标仍会推。"""
    if not WECOM_PUSH_ENABLED:
        return
    try:
        stocks = _scan_pool()
        if not stocks:
            return
        preview = []
        for s in stocks:
            try:
                score = float(s.get('score') or 0)
            except (TypeError, ValueError):
                continue
            role = (s.get('buySellPoints') or {}).get('stockType', '')
            if score >= WECOM_PUSH_MIN_SCORE and not any(kw in role for kw in WECOM_PUSH_EXCLUDE_ROLES):
                preview.append(s)
        if not preview:
            print(f'[推送 {datetime.now():%H:%M:%S}] 今日关注池预览无达标股')
            return
        preview.sort(key=lambda x: x.get('score', 0) or 0, reverse=True)
        content = _fmt_markdown(preview[:10], '📋 今日关注池（开盘前预览）')
        push_wecom_markdown(content)
        print(f'[推送 {datetime.now():%H:%M:%S}] 今日关注池预览已推送 {len(preview[:10])} 只')
    except Exception as e:
        print(f'[推送 {datetime.now():%H:%M:%S}] 预览任务异常: {e}')
        import traceback
        print(f'  堆栈: {traceback.format_exc()[:300]}')
