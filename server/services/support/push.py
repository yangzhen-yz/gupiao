"""services/push.py - 企业微信选股推送（盘中准实时）

2026-09-01 新增：
- 盘中每 1 分钟扫描候选，满足条件的股票（评分高 + 未涨停 + 预期涨幅够 + 有量能）
  通过企业微信推给授权人。
- 2026-10-16 扫描范围从"只有用户自选池（8~10 只）"扩成 **用户池 ∪ 推荐池**
  （盘前海选前 60 只 + 当日已生成推荐前 5 只）：否则 9:45 那批推荐生成后就再没人看，
  快照那一刻涨 1.1% 的票涨到 7% 也不会提醒。开关见 config.WECOM_PUSH_SCAN_RECOMMEND_POOL。
- 去重：同一只股票当天首次达标才推，之后不再重复推送；每天另有额度上限
  （config.WECOM_PUSH_DAILY_LIMIT），用满就只记不推。
- 开盘前 9:25 推送"今日关注池"预览（评分达标）。

依赖：
- wecom-cli（企业微信 CLI，已在 PATH，位于 connector_runtime artifacts）
- 本机 FastAPI 服务 /api/hot-stocks-scan（复用完整评分链路，pool_only=1 让范围由本模块决定）
"""
import json
import os
import sqlite3
import subprocess
import time
import urllib.request

import requests
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from app.config import (WECOM_PUSH_ENABLED, WECOM_AIBOT_ENABLED, WECOM_CLI_CHAT_ID, WECOM_CLI_BIN,
                        push_muted_today,
                        WECOM_WEBHOOK_ENABLED, WECOM_WEBHOOK_URL, WECOM_WEBHOOK_FILE,
                        WECOM_WEBHOOK_MSGTYPE,
                        WECOM_PUSH_MIN_SCORE,
                        WECOM_PUSH_MIN_GAIN, WECOM_PUSH_MIN_VOL_RATIO, WECOM_PUSH_MAX_CHANGE,
                        WECOM_PUSH_SCAN_RECOMMEND_POOL, WECOM_PUSH_POOL_TOP_N, WECOM_PUSH_REC_TOP_N,
                        WECOM_PUSH_DAILY_LIMIT, WECOM_PUSH_MATRIX_VETO, WECOM_PUSH_MATRIX_LABEL,
                        WECOM_PUSH_APPLY_LOSER_BLACKLIST)
from db.database import load_user_scan_pool, get_db_conn

SCAN_API_BASE = 'http://127.0.0.1:3003/api/hot-stocks-scan'


# ---------- 发送通道 ----------

# 2026-10-14 修复：wecom-cli **不在服务进程的 PATH 里**。
# 实测 `shutil.which('wecom-cli') → None`，而 push_sent_log 自建立以来 0 行，
# 也就是说 9:25 关注池预览与盘中选股推送**从来没有真正发出去过**（subprocess 每次
# FileNotFoundError，被 except 吞成一行日志）。机器人本身是 authorized 的
# （`wecom-cli auth show` 有 Bot ID），坏的是路径这一环 —— 持仓快反推送复用同一个函数，
# 不修就是"上线了一个从来不会响的闹钟"。
# 查找顺序：环境变量 WECOM_CLI_BIN → PATH → 企微连接器的 connector_runtime/artifacts 目录。
_BIN_CACHE: Dict = {'done': False, 'path': None, 'tried': []}
# 群机器人 webhook 同样只做一次探测（文件基本不会在进程生命周期里出现/消失）
_HOOK_CACHE: Dict = {'done': False, 'url': None}
# aibot 连续失败计数（熔断用；见 push_wecom_markdown）
_AIBOT_FAIL_STREAK = 0


def _resolve_wecom_bin() -> Optional[str]:
    if _BIN_CACHE['done']:
        return _BIN_CACHE['path']
    import glob
    import shutil as _shutil

    tried = []
    found = None

    env_bin = (WECOM_CLI_BIN or '').strip()
    if env_bin:
        tried.append(f'环境变量 WECOM_CLI_BIN={env_bin}')
        if os.path.isfile(env_bin):
            found = env_bin

    if not found:
        p = _shutil.which('wecom-cli')
        tried.append('PATH 中的 wecom-cli')
        if p:
            found = p

    if not found:
        local = os.environ.get('LOCALAPPDATA') or ''
        patterns = [
            os.path.join(local, '*', 'User Data', 'Default', 'connector_runtime',
                         'artifacts', 'wecom-cli.exe'),
            os.path.join(local, 'connector_runtime', 'artifacts', 'wecom-cli.exe'),
            os.path.expanduser('~/.qoder-cn/bin/wecom-cli*'),
        ]
        for pat in patterns:
            hits = sorted(glob.glob(pat))
            tried.append(f'glob {pat}（命中 {len(hits)}）')
            if hits:
                found = hits[0]
                break

    _BIN_CACHE.update({'done': True, 'path': found, 'tried': tried})
    if found:
        print(f'[推送] wecom-cli 通道就绪: {found}')
    else:
        print('[推送] [警告] 找不到 wecom-cli，所有企业微信推送都不会发出。找过：'
              + '；'.join(tried)
              + '。可设置环境变量 WECOM_CLI_BIN 指向 exe 绝对路径')
    return found


def aibot_run(sub_args: list, payload: Optional[Dict] = None, timeout: int = 25) -> Tuple[bool, str, any]:
    """跑一次 wecom-cli（所有 aibot/通讯录能力共用这一条出口）。

    返回 (是否成功, 人话错误, 原始返回)。错误里一定带上 CLI 的 help_message ——
    权限过期/未授权时那串内容就是"点这个链接去授权"，透出去用户照着点就能修，
    不必回来翻日志。
    """
    bin_path = _resolve_wecom_bin()
    if not bin_path:
        return False, '找不到 wecom-cli（PATH 与企微连接器目录都没有）', None
    cmd = [bin_path] + list(sub_args)
    if payload is not None:
        cmd += ['--json', json.dumps(payload, ensure_ascii=False)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=timeout)
    except FileNotFoundError:
        return False, f'wecom-cli 不可执行: {bin_path}', None
    except Exception as e:
        return False, f'调用异常: {e}', None
    raw = (proc.stdout or '') + (proc.stderr or '')
    try:
        data = json.loads(proc.stdout or '')
    except Exception:
        data = None
    if isinstance(data, dict):
        if data.get('errcode') in (0, None) and (data.get('success') is True or '"success": true' in raw):
            return True, '', data
        help_msg = str(data.get('help_message') or '').replace('\\n', ' ')
        err = f'errcode={data.get("errcode")} {data.get("errmsg") or ""}'
        return False, (err + '｜' + help_msg)[:400], data
    if '"success": true' in raw:
        return True, '', raw
    return False, f'rc={proc.returncode}: {raw[:200]}', raw


def push_aibot(chat_id: str, content: str, msgtype: str = 'markdown') -> Tuple[bool, str]:
    """以智能机器人身份发给某个会话（单聊传 userid、群聊传会话 ID）。

    关键点：`chat_id` 是**参数**而不是配置 —— 所以一个机器人就能给多个用户分别推单聊，
    不需要每人申请一个机器人，也不需要公网域名做回调（授权走腾讯托管页面，
    本地 CLI 轮询取结果）。
    """
    payload = {'chat_id': chat_id, 'msg_type': msgtype, msgtype: {'content': content}}
    ok, err, _data = aibot_run(['message', 'aibot', 'send'], payload)
    return ok, err


def aibot_sessions(limit: int = 20) -> Tuple[bool, str, List[Dict]]:
    """机器人最近的会话列表（按最后一条消息时间倒序）。

    这是"自助绑定"的地基：新用户只要在企微里给机器人发一句话，他的会话就会出现在这里，
    页面上认领一下就能绑定 —— 不用手抄 userid，也不用管理员去通讯录里查。
    """
    ok, err, data = aibot_run(['message', 'aibot', 'sessions', 'list'])
    if not ok:
        return False, err, []
    items = []
    if isinstance(data, dict):
        body = data.get('results_json') or data.get('data') or data.get('sessions') or []
        if isinstance(body, dict):
            body = body.get('list') or body.get('sessions') or []
        if isinstance(body, list):
            items = [x for x in body if isinstance(x, dict)][:limit]
    return True, '', items


def aibot_search_users(keyword: str) -> Tuple[bool, str, List[Dict]]:
    """按姓名/关键词查企微成员（需要机器人「通讯录」使用权限，未授权时错误里带授权链接）"""
    kw = (keyword or '').strip()
    if not kw:
        return False, '请输入要搜索的姓名', []
    ok, err, data = aibot_run(['contact', 'users', 'search', '--keywords', kw])
    if not ok:
        return False, err, []
    items = []
    if isinstance(data, dict):
        body = data.get('results_json') or data.get('data') or []
        if isinstance(body, dict):
            body = body.get('list') or body.get('users') or []
        if isinstance(body, list):
            items = [x for x in body if isinstance(x, dict)]
    return True, '', items


def push_wecom_markdown(content: str) -> bool:
    """发送 markdown 到企业微信，返回是否成功。

    通道优先级（2026-10-14）：
      1. 智能机器人 aibot（wecom-cli，发的是运维者的机器人单聊）；
      2. 群机器人 Webhook（HTTP POST，不过期、不依赖连接器进程）—— aibot 不可用或
         发送失败（实测过期的 errcode 850003）时自动改走它。
    日志一定写清最终走了哪条，否则"推送没响"和"推送发失败"这两种情况在日志里长得一样。
    """
    if not WECOM_PUSH_ENABLED:
        return False
    if push_muted_today():
        # 当天静音（config.PUSH_MUTE_DATES）：全局出口也不发
        print('[推送] 今日推送已静音（config.PUSH_MUTE_DATES）')
        return False
    global _AIBOT_FAIL_STREAK
    sent_via = ''
    bin_path = _resolve_wecom_bin() if WECOM_AIBOT_ENABLED else None
    if not WECOM_AIBOT_ENABLED:
        # 用户当前只用群聊（见 config.WECOM_AIBOT_ENABLED 注释）：不再每轮花一秒去试一条
        # 已知发不出的通道，日志里也不会有人误读成"aibot 坏了"。
        print('[推送] aibot 单聊通道已关闭（WECOM_AIBOT_ENABLED=False），本条走群机器人')
    # 熔断：持仓快反是 20 秒一轮的高频路径，aibot 权限过期时每轮都要白等一次子进程
    # 往返。连续失败 5 次就认定它当前不可用，后续直接走 webhook（服务重启后自动重试）。
    elif bin_path and _AIBOT_FAIL_STREAK < 5:
        ok, err = push_aibot(WECOM_CLI_CHAT_ID, content)
        if ok:
            _AIBOT_FAIL_STREAK = 0
            print('[推送] 已发送（通道=aibot）')
            return True
        _AIBOT_FAIL_STREAK += 1
        print(f'[推送] aibot 发送失败(连续第{_AIBOT_FAIL_STREAK}次): {err[:200]}')
    elif bin_path:
        print('[推送] aibot 连续失败已熔断，本轮直接走 webhook（服务重启后会自动重试 aibot）')
    else:
        print('[推送] aibot 通道不可用（找不到 wecom-cli），改试群机器人 Webhook')

    if push_wecom_webhook(content):
        sent_via = 'webhook'
    if sent_via:
        print('[推送] 已发送（通道=群机器人 webhook，兜底生效）')
        return True
    print('[推送] [警告] 两条通道都没发出去：aibot 权限过期且未配置 webhook。'
          '请建一个只有自己的企微群、加"自定义机器人"，'
          '把 webhook 地址写进 server/config/wecom_webhook.json（{"webhook_url": "..."}）')
    return False


def _resolve_webhook_url() -> Optional[str]:
    """群机器人 webhook 地址：环境变量优先，其次本地文件（文件不入库，含 key）"""
    if not WECOM_WEBHOOK_ENABLED:
        return None
    if _HOOK_CACHE['done']:
        return _HOOK_CACHE['url']

    url = (WECOM_WEBHOOK_URL or '').strip()
    src = '环境变量 WECOM_WEBHOOK_URL'
    if not url:
        try:
            with open(WECOM_WEBHOOK_FILE, encoding='utf-8') as f:
                raw = f.read().strip()
            if raw.startswith('{'):
                url = str(json.loads(raw).get('webhook_url') or '').strip()
            else:
                url = raw
            src = f'本地文件 {WECOM_WEBHOOK_FILE}'
        except FileNotFoundError:
            url = ''
        except Exception as e:
            print(f'[推送] 读取 webhook 配置失败: {e}')
            url = ''

    if url and 'qyapi.weixin.qq.com/cgi-bin/webhook/send' not in url:
        print(f'[推送] webhook 地址不像企微群机器人地址，已忽略（来源 {src}）')
        url = ''
    _HOOK_CACHE.update({'done': True, 'url': url or None})
    if url:
        print(f'[推送] 群机器人 webhook 通道就绪（来源 {src}）')
    return url or None


def _md_to_plain(content: str) -> str:
    """markdown → 纯文本：微信端不渲染 markdown，加粗/颜色标记会连符号一起露出来。

    只做"去掉装饰"，不改一个字的内容 —— 推送里每个数字都是决策依据，
    格式化阶段最不该发生的事就是悄悄丢信息。
    """
    import re as _re
    text = _re.sub(r'<font[^>]*>(.*?)</font>', r'\1', content, flags=_re.S)
    text = _re.sub(r'\*\*(.+?)\*\*', r'\1', text, flags=_re.S)
    text = _re.sub(r'`([^`]*)`', r'\1', text)
    text = _re.sub(r'^#{1,6}\s*', '', text, flags=_re.M)
    # 微信里长行会被折得很乱：把"· "项目符号统一成全角空格缩进，读起来更像清单
    text = _re.sub(r'^\s*[-*]\s+', '· ', text, flags=_re.M)
    return text


def push_wecom_webhook(content: str) -> bool:
    """企业微信群机器人 Webhook 发送。

    msgtype 由 config.WECOM_WEBHOOK_MSGTYPE 决定（默认 text，因为接收端是个人微信）；
    text 上限 2048 **字节**、markdown 上限 4096 字节，按 UTF-8 字节数截断，
    截断点回退到换行符，避免把一条止损提醒截在半句话上。
    """
    url = _resolve_webhook_url()
    if not url:
        return False
    msgtype = (WECOM_WEBHOOK_MSGTYPE or 'text').strip().lower()
    if msgtype != 'markdown':
        body = {'msgtype': 'text', 'text': {'content': _truncate_utf8(_md_to_plain(content), 1800)}}
    else:
        body = {'msgtype': 'markdown',
                'markdown': {'content': _truncate_utf8(content, 3600)}}
    try:
        r = requests.post(url, json=body, timeout=10)
        data = r.json() if r.text else {}
        if data.get('errcode') == 0:
            return True
        print(f'[推送] webhook 发送失败 http={r.status_code} resp={str(data)[:200]}')
        return False
    except Exception as e:
        print(f'[推送] webhook 调用异常: {e}')
        return False


def _truncate_utf8(s: str, max_bytes: int) -> str:
    """按 UTF-8 字节数截断，并回退到最近的换行处（不截在半句话/半个字上）"""
    raw = s.encode('utf-8')
    if len(raw) <= max_bytes:
        return s
    cut = raw[:max_bytes]
    try:
        text = cut.decode('utf-8')
    except UnicodeDecodeError:
        text = cut[:-1].decode('utf-8', errors='ignore')
    nl = text.rfind('\n')
    if nl > len(text) // 2:
        text = text[:nl]
    return text.rstrip() + '\n…（内容过长已截断，完整看行动台）'


# ---------- 扫描与过滤 ----------

def _set(key: str, cfg_name: str, default):
    try:
        from services.support import app_settings
        v = app_settings.value(key)
        return v if v is not None else default
    except Exception:
        import app.config as _cfg
        return getattr(_cfg, cfg_name, default)


def push_daily_limit() -> int:
    return int(_set('push_daily_limit', 'WECOM_PUSH_DAILY_LIMIT', 6))


def push_pool_top_n() -> int:
    return int(_set('push_pool_top_n', 'WECOM_PUSH_POOL_TOP_N', 60))


def _load_pool_symbols() -> list:
    """本轮连续扫描的股票范围 = 用户自选池 ∪ 推荐池（盘前海选 + 当日已生成的推荐）。

    2026-10-16 用户要求："把推荐池也塞进那条每 60 秒的连续扫描里"。改造前这条链路只扫
    自选池（8~10 只），于是 9:45 那批推荐生成后就再没人看过 —— 快照那一刻涨 1.1% 的票，
    10:05 涨到 7% 也不会提醒，页面上留着的永远是过期的时机判断。

    规模必须封顶：这条链路每 60 秒跑一次，海选池按 20 日强度取前 N 只。
    全塞进去会把腾讯批量接口打爆（今天 13:39 那批推荐就是它连不上而整批失败的）。
    """
    out, seen = [], set()

    def _add(codes):
        for c in (codes or []):
            c = (c or '').lower()
            if c and c not in seen:
                seen.add(c)
                out.append(c)

    try:
        _add(load_user_scan_pool())
    except Exception as e:
        print(f'[推送] 加载用户池失败: {e}')
    if not WECOM_PUSH_SCAN_RECOMMEND_POOL:
        return out
    try:
        from services.pick.candidate_pool import load_pool
        _add([p.get('symbol') for p in load_pool(limit=push_pool_top_n())])
    except Exception as e:
        print(f'[推送] 海选池并入失败（本轮只扫用户池）: {e}')
    try:
        conn = get_db_conn()
        try:
            rows = conn.execute(
                'SELECT symbol FROM daily_recommendations WHERE rec_date=? '
                'ORDER BY sort_order LIMIT ?',
                (datetime.now().strftime('%Y-%m-%d'), WECOM_PUSH_REC_TOP_N)).fetchall()
        finally:
            conn.close()
        _add([r[0] for r in rows])
    except Exception as e:
        print(f'[推送] 当日推荐并入失败: {e}')
    return out


def _scan_pool() -> list:
    """内部 HTTP 调本机扫描 API。pool_only=1 是故意的：扫描范围由 _load_pool_symbols 决定，
    不让路由再自行并热门池/热搜池，否则每 60 秒一次的规模就失控了。"""
    pool = _load_pool_symbols()
    if not pool:
        return []
    url = f'{SCAN_API_BASE}?codes={",".join(pool)}&pool_only=1'
    try:
        t0 = time.time()
        with urllib.request.urlopen(url, timeout=45) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        _SCAN_STATS['n'], _SCAN_STATS['sec'] = len(pool), round(time.time() - t0, 1)
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f'[推送] 扫描API调用失败: {e}')
        return []


# 本轮扫描规模（只给日志看：范围从 8 只扩到 70 只之后，单轮耗时是唯一的负载指标）
_SCAN_STATS: Dict = {'n': 0, 'sec': 0.0}


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


def _st_name_veto(stock) -> bool:
    """名称层 ST 复核（True=否决）。抽成函数是为了能被自测直接跑，含"判据坏了"那一支。

    11-07 修⑩-6：这一段原来是 `except Exception: pass`——判据坏了就静默放行，
    而它存在的理由就是"ST/退 永不推"（可交易范围的第一条），
    一个只在正常时才生效的闸门等于没有闸门。现在**坏了就否决这一只**并打一行。
    `is_st_name` 是纯字符串判断（`services/data/universe.py:64`），能抛异常的只有导入本身
    坏掉；真到那一步整条推送链都该被看见，不该静默把 ST 推出去。
    """
    try:
        from services.data.universe import is_st_name as _is_st
        if _is_st(stock.get('name')):
            print(f"[推送] ST 名称否决 {stock.get('name')}（{stock.get('symbol')}）")
            return True
        return False
    except Exception as e:
        print(f'[推送] ST 名称复核不可用，本轮对该票保守否决: {type(e).__name__}: {e}'
              f'（{stock.get("name") or stock.get("symbol")}）')
        return True


def _pass_filter(stock) -> bool:
    """是否满足推送条件（与用户确认方案）：
    评分≥阈值 + 未涨停 + 预期涨幅≥阈值 + 量比≥阈值。
    （2026-10-28 起删掉"角色非跟风/杂毛"一条：角色体系整体下线）
    """
    # 名称层 ST 复核（2026-11-01）：与智能推荐同一条判据，堵"推荐不推、推送推"的漏口。
    # 扫描接口虽然也走范围过滤，但它读的是本地名录；行情名称是当下的，多挡一次不花成本。
    if _st_name_veto(stock):
        return False
    try:
        score = float(stock.get('score') or 0)
    except (TypeError, ValueError):
        return False
    if score < WECOM_PUSH_MIN_SCORE:
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
    # 矩阵否决（2026-10-16 分钟矩阵实测出的明确负期望档：过热 / 天量 / 巨量拉升 / 极热日）。
    # 只否决这几档 —— "当天不追""不接力"照样推、但在文案里标出来：把矩阵全当成硬否决，
    # 就又变成一个没人看得懂的黑箱，两周后也没法回头验证它到底对不对。
    m = matrix_of(stock)
    if m.get('verdict') in WECOM_PUSH_MATRIX_VETO:
        print(f"[推送] 矩阵否决 {stock.get('name')}: {m.get('verdict')}（{m.get('why')}）")
        return False
    # 亏损黑名单（2026-10-17 补的漏口）：这张表以前**只有智能推荐**读它，
    # 盘中 60 秒扫描不查，于是被拉黑的票还能从推送里回来 —— 同一只票一边"避让"一边"推荐你买"。
    sym = (stock.get('symbol') or stock.get('code') or '').lower()
    if sym and sym in _loser_blacklist():
        print(f'[推送] 亏损黑名单否决 {stock.get("name") or sym}（近30天推荐后 T+1/T+3 亏超阈值）')
        return False
    # 涨幅段闸门（2026-10-18）：盘中推送与智能推荐主链同一张表（momentum_zone），
    # 避免"主链不推、推送推"的漏口。只拦 drop 模式 —— demote 的本意是罚分但可选，
    # 推送链没有评分可罚，硬拦就跟主链口径不一致了。区间依据见 momentum_zone docstring。
    try:
        from services.strategy.momentum_zone import gate_stock as _zone_gate
        _z = _zone_gate(stock)
    except Exception as e:
        # 读不到就不否决：闸门是"少推一只"的保守措施，不该把整条推送链路打断
        print(f'[推送] 涨幅段闸门读取失败(本轮不否决): {type(e).__name__}: {e}')
        _z = None
    if _z:
        print(f"[推送] 涨幅段闸门否决 {stock.get('name')}: {_z['label']}"
              f"（当日涨幅 {_z['chg']:+.1f}%）")
        return False
    return True


_LOSER_BL_CACHE = {'ts': 0.0, 'set': set()}


def _loser_blacklist() -> set:
    """当前禁推的票（带 60 秒级缓存，实际 TTL 由 recommend_review 那侧统一控）。"""
    if not WECOM_PUSH_APPLY_LOSER_BLACKLIST:
        return set()
    try:
        from services.pick.recommend_review import get_recent_loser_symbols
        return get_recent_loser_symbols()
    except Exception as e:
        # 读不到就不否决：避让是"少推一只"的保守措施，不该把整条推送链路打断
        print(f'[推送] 亏损黑名单读取失败(本轮不否决): {type(e).__name__}: {e}')
        return set()


# ---------- 矩阵判定（推送侧只读，不改评分） ----------

_MATRIX_CACHE: Dict = {}
_PREV_BAR_CACHE: Dict = {}


def matrix_of(stock: dict) -> dict:
    """这只票此刻落在矩阵的哪一档。带缓存：一轮扫描里同一只票会被过滤和文案各用一次。"""
    try:
        from services.strategy.buy_rules import classify
        sym = (stock.get('symbol') or stock.get('code') or '').lower()
        key = f'{sym}|{stock.get("changePercent")}'
        hit = _MATRIX_CACHE.get(key)
        if hit:
            return hit
        res = classify({'changePercent': stock.get('changePercent'),
                        'volumeRatio': stock.get('volumeRatio'),
                        'gain20': stock.get('gain20')},
                       _prev_bar_for(sym), {'limitUp': _market_limit_ups()})
        if len(_MATRIX_CACHE) > 400:
            _MATRIX_CACHE.clear()
        _MATRIX_CACHE[key] = res
        return res
    except Exception as e:
        return {'verdict': '未判定', 'why': str(e)[:60]}


def _prev_bar_for(symbol: str) -> dict:
    """上一交易日的涨幅与是否封板 —— 矩阵里"不接力""可竞价低吸"两档要看昨天。"""
    code = (symbol or '')[-6:]
    if not code:
        return {}
    if code in _PREV_BAR_CACHE:
        return _PREV_BAR_CACHE[code]
    out: Dict = {}
    try:
        from app.config import KLINE_HISTORY_DB_PATH
        conn = sqlite3.connect(KLINE_HISTORY_DB_PATH, timeout=15)
        try:
            rows = conn.execute('SELECT close FROM stock_daily WHERE symbol=? '
                                'ORDER BY date DESC LIMIT 3', (code,)).fetchall()
        finally:
            conn.close()
        if len(rows) >= 2 and rows[1][0]:
            chg = (rows[0][0] / rows[1][0] - 1) * 100
            lim = 0.20 if code.startswith(('30', '68')) else 0.10
            out = {'changePercent': round(chg, 2), 'limitClose': chg >= lim * 98.5}
    except Exception:
        out = {}
    if len(_PREV_BAR_CACHE) > 1200:
        _PREV_BAR_CACHE.clear()
    _PREV_BAR_CACHE[code] = out
    return out


def _market_limit_ups() -> float:
    """今天全市场涨停家数（读情绪快照，不在同步链路里 await 异步任务）。

    2026-11-07 修两处，两处叠在一起才致命：
      · **键读错了**：老代码取顶层 `d['limitUp']`，而快照顶层根本没有这个键，
        涨停家数实际在 `indicators.limitUpCount`（`recommend.py:1606` 取的就是对的）。
        实测 11-06 最后一条快照：顶层无 `limitUp`、`indicators.limitUpCount=32`。
        后果是推送链路每票的矩阵判定永远拿到 `limitUp=0`，矩阵里那条
        "极热日闸门（涨停 >120 家 → 减仓日、不开新仓）"**在推送侧永远不触发**，
        而推荐侧会触发 —— 同一个市场状态，两条链路给出不一样结论。
      · **SQL 没有当日过滤**：`ORDER BY ts DESC LIMIT 1` 跨天取最新。这条以前被
        "永远读到 0"掩盖着；键修对之后它就变成真风险（今天开盘前会拿昨天的家数
        决定今天开不开新仓），所以必须一起改。
      现在按 `sent_date=今天` 取；读不到就返回 0 并维持"不触发闸门"的老行为
      —— 宁可不拦，也不拿昨天的数拦今天。
    """
    try:
        conn = get_db_conn()
        try:
            row = conn.execute(
                'SELECT payload FROM market_sentiment_snapshots '
                'WHERE sent_date=? ORDER BY ts DESC LIMIT 1',
                (datetime.now().strftime('%Y-%m-%d'),)).fetchone()
        finally:
            conn.close()
        d = json.loads(row[0]) if row and row[0] else {}
        v = (d.get('indicators') or {}).get('limitUpCount')
        if v is None:
            v = d.get('limitUp')          # 历史快照/老口径兜底，别让旧行读成 0
        return float(v or 0)
    except Exception:
        return 0.0


def pushed_today(date_str: str) -> int:
    """今天已经推过几只达标票（额度用满就只记不推）。"""
    try:
        conn = get_db_conn()
        try:
            return conn.execute('SELECT COUNT(1) FROM push_sent_log WHERE push_date=?',
                                (date_str,)).fetchone()[0]
        finally:
            conn.close()
    except Exception:
        return 0


# ---------- 去重（当天每只票只推一次） ----------

def _ensure_table():
    try:
        conn = get_db_conn(wait_for_lock=True)
        conn.execute('''CREATE TABLE IF NOT EXISTS push_sent_log (
            push_date TEXT NOT NULL,
            symbol   TEXT NOT NULL,
            pushed_at TEXT NOT NULL,
            PRIMARY KEY (push_date, symbol)
        )''')
        # 2026-11-03 补两列：score=推送时的评分，channel=哪条推送链（达标即推/龙头/主推送…）。
        # 老行为 NULL；CREATE TABLE IF NOT EXISTS 不会加列，只能逐个 ALTER 兼容已存在的库。
        for col, ddl in (('score', 'REAL'), ('channel', 'TEXT')):
            try:
                conn.execute(f'ALTER TABLE push_sent_log ADD COLUMN {col} {ddl}')
            except Exception:
                pass  # 列已存在
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


def _mark_pushed(date_str: str, symbol: str, score=None, channel: str = 'screener'):
    try:
        conn = get_db_conn(wait_for_lock=True)
        conn.execute(
            'INSERT OR IGNORE INTO push_sent_log (push_date, symbol, pushed_at, score, channel) '
            'VALUES (?,?,?,?,?)',
            (date_str, symbol, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), score, channel),
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
        gain = _expected_gain(s)
        vr = s.get('volumeRatio', 0)
        lines.append(f'{i}. **{name}**（{code}）')
        lines.append(f'   💰 现价 {price}｜涨幅 {change}%｜评分 {score}')
        lines.append(f'   🎯 预期涨幅 {gain:.1f}%｜量比 {vr:.1f}')
        if WECOM_PUSH_MATRIX_LABEL:
            m = matrix_of(s)
            v = m.get('verdict') or ''
            if v and v != '未判定':
                lines.append(f'   🧭 位置判定 {v}：{m.get("action", "")}')
                if m.get('risk'):
                    lines.append(f'      历史：{m["risk"]}')
    return '\n'.join(lines)


# ---------- 任务入口 ----------

async def push_candidates_task():
    """盘中推送主任务：扫描用户池 → 过滤 → 去重 → 推送。失败不抛异常（由 scheduler 兜底）。

    限频：个股只数（push_daily_limit，页面可配，默认 6 只/天），每只票一天只推一次。
    （服务重启/自愈类运维通知的"每天最多 N 条"限频在仓库外守护脚本侧，不在此处。）
    """
    if not WECOM_PUSH_ENABLED:
        return
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        _ensure_table()

        stocks = _scan_pool()
        if not stocks:
            print(f'[推送 {datetime.now():%H:%M:%S}] 扫描无结果，跳过')
            return

        # ② 个股只数上限（每只一天只推一次）
        left = push_daily_limit() - pushed_today(today)
        if left <= 0:
            print(f'[推送 {datetime.now():%H:%M:%S}] 今日达标推送已用满 {push_daily_limit()} 只额度，'
                  f'本轮只记不推')
            return
        hits = []
        for s in stocks:
            sym = s.get('symbol', '')
            if not _pass_filter(s):
                continue
            if _already_pushed(today, sym):
                continue
            hits.append(s)

        if not hits:
            print(f'[推送 {datetime.now():%H:%M:%S}] 无新达标候选（扫描 {len(stocks)} 只；'
                  f'范围 {_SCAN_STATS["n"]} 只、单轮 {_SCAN_STATS["sec"]}s）')
            return
        if len(hits) > left:
            print(f'[推送] 本轮达标 {len(hits)} 只超剩余额度 {left} 只，只推前 {left} 只')
            hits = hits[:left]
        content = _fmt_markdown(hits, '⚡ 选股推送（达标即推）')
        if push_wecom_markdown(content):
            for s in hits:
                _mark_pushed(today, s.get('symbol', ''),
                             score=s.get('score'), channel='screener')
            print(f'[推送 {datetime.now():%H:%M:%S}] 已推送 {len(hits)} 只: '
                  f'{", ".join(s.get("name", "") for s in hits)}')
    except Exception as e:
        print(f'[推送 {datetime.now():%H:%M:%S}] 推送任务异常: {e}')
        import traceback
        print(f'  堆栈: {traceback.format_exc()[:300]}')


async def push_daily_preview_task():
    """开盘前 9:25 推送"今日关注池"预览：评分≥阈值（竞价阶段涨幅/量比参考意义弱，放宽）。
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
            if score >= WECOM_PUSH_MIN_SCORE:
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
