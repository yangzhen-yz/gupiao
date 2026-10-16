"""push_channel.py - 按账号隔离的企业微信推送通道（2026-10-14）

为什么要有这一层
----------------
改造前推送只有一个全局出口（`push.py` 的 wecom-cli chat_id + 一个 webhook 地址），
所有代码路径共用它。这在单账号时看不出问题，多账号一上线就是两个事故：
  · 串台 —— A 的止损提醒发到 B 的群里；
  · 隐私 —— 别人的持仓明细进了你的群。
而且"给别人开推送"只能改代码或改配置文件，等于每个新用户都要动一次服务。

本模块把"发给谁"从配置里拿出来，变成**每个账号自己绑定、自己验证**的数据：
  · 绑定即验证 —— 先校验地址、再真的发一条确认消息，只有企微返回 errcode=0 才落库
    并写 verified_at。所以通道列表里出现的每一条都是"能送达"的，不存在"配了但发不出"。
  · 发送按归属 —— 持仓类消息走 `send_to_user(持仓所属 user_id, ...)`，
    谁的仓发给谁；该账号一条通道都没绑时回落到全局地址（保持 yz 现有群可用）。
  · 地址白名单 —— 只接受 `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=<uuid>`。
    这一条不是洁癖：把任意字符串存进服务端并让服务端去 POST，等于给自己开了一个
    内网 SSRF 入口（用户可填 http://169.254.169.254/ 之类探测内网）。

关于"扫码绑定"
--------------
真正能规模化、且用户只需点一下链接的扫码/授权，需要企业微信**自建应用**
（corpid + agentid + corpsecret，由企微管理员一次性创建），届时用 OAuth 拿到该用户的
userid、走 message/send 推单聊。表里的 `kind` 就是为它预留的第二通道位。
本机的 wecom-cli aibot 只有**一份机器人凭据、绑一个授权人**，没法给每个注册用户
各自建立连接，所以它继续作为运维者的全局兜底通道，不承担多用户绑定。
没有拿到自建应用凭据之前，这里不实现任何"猜测中的 OAuth 逻辑"。
"""
import json
import re
import sqlite3
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import requests

from app.config import (PUSH_DAILY_LIMIT_PER_USER, WECOM_PUSH_ENABLED, WECOM_AIBOT_ENABLED,
                        push_muted_today, WECOM_WEBHOOK_URL,
                        WECOM_WEBHOOK_FILE, WECOM_WEBHOOK_ENABLED,
                        WECOM_AGENT_CORP_ID, WECOM_AGENT_ID, WECOM_AGENT_SECRET,
                        WECOM_AGENT_REDIRECT_BASE, wecom_agent_missing, wecom_agent_ready)
from db.database import get_db_conn
from services.support.push import (_md_to_plain, _truncate_utf8, push_wecom_markdown,
                           push_aibot, aibot_sessions, aibot_search_users)

WEBHOOK_PREFIX = 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send'
_KEY_RE = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
                     r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')
# 全局兜底地址（运维者自己的群）：配置文件或环境变量，进程内读一次
_GLOBAL_FALLBACK: Dict = {'done': False, 'url': None}
# 自建应用 access_token 缓存
_TOKEN_CACHE: Dict = {'token': None, 'exp': 0.0}


def _ensure_table():
    """通道表 + 快反去重表（去重表 2026-10-14 起按 user_id 隔离，见 _migrate_alert_log）"""
    conn = get_db_conn()
    try:
        conn.execute('''CREATE TABLE IF NOT EXISTS user_push_channels (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            kind        TEXT NOT NULL,
            target      TEXT NOT NULL,
            label       TEXT,
            verified_at TEXT,
            created_at  TEXT NOT NULL,
            UNIQUE (user_id, kind, target)
        )''')
        conn.execute(_ALERT_DDL)
        _migrate_alert_log(conn)
        conn.commit()
    finally:
        conn.close()


_ALERT_DDL = '''CREATE TABLE IF NOT EXISTS position_alert_log (
            alert_date TEXT NOT NULL,
            user_id    INTEGER NOT NULL DEFAULT 0,
            symbol     TEXT NOT NULL,
            kind       TEXT NOT NULL,
            pushed_at  TEXT NOT NULL,
            price      REAL,
            note       TEXT,
            PRIMARY KEY (alert_date, user_id, symbol, kind)
        )'''


def _migrate_alert_log(conn):
    """去重表加 user_id：主键必须含 user_id，否则 A 绑过的那只票会把 B 的同票提醒一起挡掉。

    旧表主键是 (alert_date, symbol, kind)，SQLite 不能改主键 —— 只能建新表搬数据。
    旧数据都是"全局单通道时代"产生的，归属未知，统一记到 user_id=0（不会与真实账号冲突，
    真实账号 id 从 1 开始）。
    """
    cols = [r[1] for r in conn.execute('PRAGMA table_info(position_alert_log)')]
    if not cols or 'user_id' in cols:
        return
    conn.execute('ALTER TABLE position_alert_log RENAME TO position_alert_log_old')
    conn.execute(_ALERT_DDL)
    conn.execute('INSERT OR IGNORE INTO position_alert_log '
                 '(alert_date, user_id, symbol, kind, pushed_at, price, note) '
                 'SELECT alert_date, 0, symbol, kind, pushed_at, price, note '
                 'FROM position_alert_log_old')
    conn.execute('DROP TABLE position_alert_log_old')
    print('[推送通道] position_alert_log 已加 user_id 并按账号重建主键')


def validate_webhook_url(url: str) -> Tuple[bool, str]:
    """群机器人地址白名单：只允许企微官方 webhook 域与路径，key 必须是 uuid 形状"""
    u = (url or '').strip()
    if not u:
        return False, '地址为空'
    if not u.startswith(WEBHOOK_PREFIX):
        return False, f'只支持企业微信群机器人地址（须以 {WEBHOOK_PREFIX} 开头）'
    m = re.search(r'[?&]key=([^&\s]+)', u)
    if not m:
        return False, '地址里找不到 key='
    if not _KEY_RE.match(m.group(1)):
        return False, 'key 格式不像企微群机器人的 uuid，请整段复制不要手敲'
    return True, ''


def _post_webhook(url: str, content: str) -> Tuple[bool, str]:
    """向某个群机器人发一条消息（text 类型：微信端不渲染 markdown）"""
    body = {'msgtype': 'text', 'text': {'content': _truncate_utf8(_md_to_plain(content), 1800)}}
    try:
        r = requests.post(url, json=body, timeout=10)
        data = r.json() if r.text else {}
        if data.get('errcode') == 0:
            return True, ''
        return False, f'企微返回 errcode={data.get("errcode")} {str(data.get("errmsg") or "")[:80]}'
    except Exception as e:
        return False, f'请求异常: {e}'


def _agent_token() -> Tuple[Optional[str], str]:
    """自建应用 access_token（企微有效期 7200s，提前 5 分钟刷新；进程内缓存）"""
    global _TOKEN_CACHE
    now = time.time()
    if _TOKEN_CACHE['token'] and now < _TOKEN_CACHE['exp'] - 300:
        return _TOKEN_CACHE['token'], ''
    miss = wecom_agent_missing()
    if miss:
        return None, '自建应用未配置：缺 ' + '、'.join(miss)
    try:
        r = requests.get('https://qyapi.weixin.qq.com/cgi-bin/gettoken',
                         params={'corpid': WECOM_AGENT_CORP_ID, 'corpsecret': WECOM_AGENT_SECRET},
                         timeout=10)
        data = r.json() if r.text else {}
        if data.get('errcode') == 0 and data.get('access_token'):
            _TOKEN_CACHE.update({'token': data['access_token'],
                                 'exp': now + float(data.get('expires_in') or 7200)})
            return data['access_token'], ''
        return None, f'取 access_token 失败 errcode={data.get("errcode")} {str(data.get("errmsg") or "")[:60]}'
    except Exception as e:
        return None, f'取 access_token 异常: {e}'


def _post_agent(userid: str, content: str) -> Tuple[bool, str]:
    """自建应用 → 应用消息（单聊）。text 类型，微信端/企微端都能读。"""
    token, err = _agent_token()
    if not token:
        return False, err
    body = {'touser': userid, 'msgtype': 'text',
            'agentid': int(WECOM_AGENT_ID),
            'text': {'content': _truncate_utf8(_md_to_plain(content), 1800)}}
    try:
        r = requests.post('https://qyapi.weixin.qq.com/cgi-bin/message/send',
                          params={'access_token': token}, json=body, timeout=10)
        data = r.json() if r.text else {}
        if data.get('errcode') == 0:
            # invaliduser 非空表示"接口成功但这个 userid 收不到"，不能算送达
            bad = (data.get('invaliduser') or '').strip()
            if bad:
                return False, f'企微判定该账号收不到（invaliduser={bad}），请核对 userid 与应用可见范围'
            return True, ''
        return False, f'企微返回 errcode={data.get("errcode")} {str(data.get("errmsg") or "")[:80]}'
    except Exception as e:
        return False, f'请求异常: {e}'


def discover_sessions() -> Tuple[bool, str, List[Dict]]:
    """列出机器人最近的会话，供用户在页面上"认领"自己的那一条。

    这是自助绑定里体验最好的一条：新用户只要在企微里给机器人发一句话，会话就会出现在
    这里，点一下就完成绑定 —— 不用手抄 userid，也不用管理员去通讯录查。
    返回的每条做了归一化（不同版本字段名不一致，取到哪个算哪个），只保留展示需要的字段。
    """
    ok, err, raw = aibot_sessions()
    if not ok:
        return False, err, []
    items = []
    for s in raw:
        cid = str(s.get('chat_id') or s.get('chatId') or s.get('session_id')
                  or s.get('id') or '').strip()
        if not cid:
            continue
        items.append({
            'chat_id': cid,
            'name': str(s.get('name') or s.get('chat_name') or s.get('nickname') or '').strip(),
            'type': str(s.get('chat_type') or s.get('type') or '').strip(),
            'last_at': str(s.get('last_time') or s.get('update_time') or '').strip(),
        })
    return True, '', items


def bind_aibot(user_id: int, chat_id: str, label: str = '') -> Tuple[bool, str, Optional[int]]:
    """绑定智能机器人单聊：实发一条确认消息，送达才落库（与 webhook 同一套规矩）"""
    cid = (chat_id or '').strip()
    if not WECOM_AIBOT_ENABLED:
        # 用户当前只要群聊推送（config.WECOM_AIBOT_ENABLED=False）。这里明确拒绝而不是
        # "绑上但发不出"：绑一个发不出的通道，比不绑更坏 —— 它会让人以为提醒已经在路上。
        return False, '单聊通道暂未启用（当前只用企业微信群聊推送），代码已就绪，开启开关即可用', None
    if not cid:
        return False, '请选择或填写要绑定的会话', None
    if len(cid) < 6:
        return False, '会话标识太短，不像企微的 userid/会话 ID', None
    content = ('**📲 单聊推送已绑定**\n\n'
               '这条消息走的是企业微信「智能机器人 → 单聊」，只有你本人看得到。\n'
               '· 触发条件：跌破止损 / 到达止盈 / 炸板减仓 / 冲高回落 / 短线情绪转弱\n'
               f'· 每人每天默认最多 {PUSH_DAILY_LIMIT_PER_USER} 条持仓类提醒，超出只记在行动台页面\n'
               '· 解绑：持仓页「推送到企业微信」里点删除')
    ok, err = push_aibot(cid, content)
    if not ok:
        return False, f'确认消息没发出去，未绑定：{err}', None
    _ensure_table()
    conn = get_db_conn()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn.execute('INSERT OR IGNORE INTO user_push_channels '
                 '(user_id, kind, target, label, verified_at, created_at) VALUES (?,?,?,?,?,?)',
                 (user_id, 'aibot', cid, (label or '').strip()[:40] or '企微单聊', now, now))
    row = conn.execute('SELECT id FROM user_push_channels WHERE user_id=? AND kind=? AND target=?',
                       (user_id, 'aibot', cid)).fetchone()
    conn.commit()
    conn.close()
    return True, '已发送确认消息并绑定单聊', (row[0] if row else None)


def search_users(keyword: str) -> Tuple[bool, str, List[Dict]]:
    """按姓名查企微成员（需机器人「通讯录」权限；未授权时错误里会带授权链接）"""
    ok, err, raw = aibot_search_users(keyword)
    if not ok:
        return False, err, []
    items = []
    for u in raw:
        uid = str(u.get('userid') or u.get('user_id') or u.get('id') or '').strip()
        if not uid:
            continue
        items.append({'userid': uid, 'name': str(u.get('name') or '').strip(),
                      'dept': str(u.get('department') or u.get('main_department') or '').strip()})
    return True, '', items


def agent_status() -> Dict:
    """给前端的自检结论：配了什么、能不能取到 token、还缺管理员哪一步。

    这个接口存在的意义是"别让人靠猜"：自建应用失败原因（IP 白名单、secret 错、
    可见范围不含该用户）在发送日志里长得很像，提前一次性摊开更快。
    """
    miss = wecom_agent_missing()
    out = {'configured': not miss, 'missing': miss,
           'oauth_link_enabled': bool(WECOM_AGENT_REDIRECT_BASE and not miss),
           'token_ok': False, 'detail': ''}
    if miss:
        out['detail'] = '等管理员提供：企业 ID / AgentId / Secret，并把本机出口 IP 加进「企业可信 IP」'
        return out
    token, err = _agent_token()
    out['token_ok'] = bool(token)
    out['detail'] = err or 'access_token 获取正常，可绑定 userid 推单聊'
    return out


_USERID_RE = re.compile(r'^[A-Za-z0-9._\-@]{1,64}$')


def bind_userid(user_id: int, userid: str, label: str = '') -> Tuple[bool, str, Optional[int]]:
    """绑定自建应用单聊：先实发一条确认消息，送达才落库（与 webhook 同一套规矩）"""
    if wecom_agent_missing():
        return False, '自建应用未配置（缺 ' + '、'.join(wecom_agent_missing()) + '）', None
    uid = (userid or '').strip()
    if not _USERID_RE.match(uid):
        return False, '企微账号ID格式不对（一般是通讯录里的「账号」，字母数字组成）', None
    content = ('**📲 单聊推送已绑定**\n\n'
               '这条消息走的是企业微信「自建应用 → 应用消息」，只有你本人能看到。\n'
               '· 触发条件：跌破止损 / 到达止盈 / 炸板减仓 / 冲高回落 / 短线情绪转弱\n'
               '· 每人每天默认最多 10 条持仓类提醒，超出只记在行动台页面')
    ok, err = _post_agent(uid, content)
    if not ok:
        return False, f'确认消息没发出去，未绑定：{err}', None
    _ensure_table()
    conn = get_db_conn()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn.execute('INSERT OR IGNORE INTO user_push_channels '
                 '(user_id, kind, target, label, verified_at, created_at) VALUES (?,?,?,?,?,?)',
                 (user_id, 'agent', uid, (label or '').strip()[:40] or '企微单聊', now, now))
    row = conn.execute('SELECT id FROM user_push_channels WHERE user_id=? AND kind=? AND target=?',
                       (user_id, 'agent', uid)).fetchone()
    conn.commit()
    conn.close()
    return True, '已发送确认消息并绑定单聊', (row[0] if row else None)


def _global_fallback_url() -> Optional[str]:
    if _GLOBAL_FALLBACK['done']:
        return _GLOBAL_FALLBACK['url']
    url = (WECOM_WEBHOOK_URL or '').strip()
    if not url and WECOM_WEBHOOK_ENABLED:
        try:
            with open(WECOM_WEBHOOK_FILE, encoding='utf-8') as f:
                raw = f.read().strip()
            url = str(json.loads(raw).get('webhook_url') or '').strip() if raw.startswith('{') else raw
        except FileNotFoundError:
            url = ''
        except Exception as e:
            print(f'[推送通道] 读全局 webhook 配置失败: {e}')
            url = ''
    ok, _ = validate_webhook_url(url)
    _GLOBAL_FALLBACK.update({'done': True, 'url': url if ok else None})
    return _GLOBAL_FALLBACK['url']


def list_channels(user_id: int, raw: bool = False) -> List[Dict]:
    """本账号的通道列表。raw=True 时保留完整地址（仅服务端内部发送用）。

    默认脱敏是因为这个列表会经接口回到前端：地址里的 key 就等于凭据，
    拿到它任何人都能往同一个群发消息，不该出现在浏览器里。
    """
    _ensure_table()
    conn = get_db_conn()
    rows = conn.execute('SELECT id, kind, target, label, verified_at FROM user_push_channels '
                        'WHERE user_id=? ORDER BY verified_at IS NULL, id', (user_id,)).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        if not raw:
            d['target_hint'] = re.sub(r'key=([^&]{6})[^&]*', r'key=\1***', d.get('target') or '')
            d.pop('target', None)
        out.append(d)
    return out


def bind_webhook(user_id: int, url: str, label: str = '') -> Tuple[bool, str, Optional[int]]:
    """绑定一条群机器人地址：先校验、再实发确认消息，成功才落库"""
    ok, err = validate_webhook_url(url)
    if not ok:
        return False, err, None
    url = url.strip()
    _ensure_table()
    content = ('**📲 推送绑定成功**\n\n'
               '这条消息来自「只做龙头」，说明这个通道已经能收到你的持仓提醒。\n'
               '· 触发条件：跌破止损 / 到达止盈 / 炸板减仓 / 冲高回落 / 短线情绪转弱\n'
               '· 去重：每只票每种提醒每天一条，止损后续跌会再提醒\n'
               '· 解绑：持仓页「推送到企业微信」里点删除')
    sent, serr = _post_webhook(url, content)
    if not sent:
        return False, f'地址校验通过但消息没发出去：{serr}', None
    conn = get_db_conn()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    conn.execute('INSERT OR IGNORE INTO user_push_channels '
                 '(user_id, kind, target, label, verified_at, created_at) VALUES (?,?,?,?,?,?)',
                 (user_id, 'webhook', url, (label or '').strip()[:40] or '我的群', now, now))
    row = conn.execute('SELECT id FROM user_push_channels WHERE user_id=? AND kind=? AND target=?',
                       (user_id, 'webhook', url)).fetchone()
    conn.commit()
    conn.close()
    return True, '已发送确认消息并绑定', (row[0] if row else None)


def unbind(user_id: int, channel_id: int) -> bool:
    _ensure_table()
    conn = get_db_conn()
    n = conn.execute('DELETE FROM user_push_channels WHERE id=? AND user_id=?',
                     (channel_id, user_id)).rowcount
    conn.commit()
    conn.close()
    return n > 0


def test_channel(user_id: int, channel_id: int) -> Tuple[bool, str]:
    conn = get_db_conn()
    row = conn.execute('SELECT kind, target FROM user_push_channels WHERE id=? AND user_id=?',
                       (channel_id, user_id)).fetchone()
    conn.close()
    if not row:
        return False, '通道不存在或不属于本账号'
    msg = (f'🔔 测试消息 {datetime.now():%H:%M:%S} —— 这条通道可用')
    if row[0] == 'aibot':
        return push_aibot(row[1], msg)
    if row[0] == 'agent':
        return _post_agent(row[1], msg)
    return _post_webhook(row[1], msg)


def sent_today(user_id: int) -> int:
    """今天已经给该账号发出过多少条持仓类提醒（用于每日上限）"""
    try:
        conn = get_db_conn()
        n = conn.execute("SELECT COUNT(*) FROM position_alert_log "
                         "WHERE alert_date=? AND user_id=? AND symbol != '__market__' "
                         "AND (note IS NULL OR note != 'capped')",
                         (datetime.now().strftime('%Y-%m-%d'), user_id)).fetchone()[0]
        conn.close()
        return int(n or 0)
    except Exception:
        return 0


def over_daily_limit(user_id: int) -> bool:
    if not PUSH_DAILY_LIMIT_PER_USER or PUSH_DAILY_LIMIT_PER_USER <= 0:
        return False
    return sent_today(user_id) >= PUSH_DAILY_LIMIT_PER_USER


def send_to_user(user_id: int, content: str, personal: bool = True) -> Dict:
    """按账号发送。

    `personal=True`（持仓、委托、任何带账号私有数据的内容）：**只走该账号自己绑定的通道**，
    一条都没绑就直接返回失败，绝不回落到全局群 —— 否则 A 的止损明细会出现在 B 的群里，
    这是隐私事故，不是兜底。调用方拿到 ok=False 后应在页面上提示"未绑定"。

    `personal=False`（市场级情绪提醒这类对所有人一样的内容）：允许回落到全局出口，
    保证运维者自己不漏掉信号。
    """
    if not WECOM_PUSH_ENABLED:
        return {'ok': False, 'via': 'none', 'errors': ['推送总开关已关闭']}
    if push_muted_today():
        # 用户当天明确要求静默（config.PUSH_MUTE_DATES）：信号照记进行动台，消息不发
        return {'ok': False, 'via': 'muted', 'errors': ['今日推送已静音（config.PUSH_MUTE_DATES）']}
    errors = []
    chans = list_channels(user_id, raw=True)
    # 顺序：机器人单聊 → 自建应用单聊 → 群。给自己的消息不该先落到一个群里。
    # 单聊两条按开关过滤：WECOM_AIBOT_ENABLED=False（用户当前只用群聊）时，
    # 即使库里残留着 aibot 通道记录也不会去试 —— 开关说了算，不靠"数据里碰巧有"。
    kinds = [k for k, on in (('aibot', WECOM_AIBOT_ENABLED), ('agent', wecom_agent_ready()),
                             ('webhook', True)) if on]
    for kind in kinds:
        for ch in [x for x in chans if x.get('kind') == kind]:
            target = ch.get('target') or ''
            if not target:
                continue
            if kind == 'aibot':
                ok, err = push_aibot(target, content)
            elif kind == 'agent':
                ok, err = _post_agent(target, content)
            else:
                ok, err = _post_webhook(target, content)
            if ok:
                return {'ok': True, 'via': kind, 'channel_id': ch['id'], 'errors': errors}
            errors.append(f"#{ch['id']} {err}")
            print(f"[推送通道] 账号{user_id} 通道{ch['id']}({kind}) 发送失败：{err}")
    if chans:
        # 绑了但全失败：个人内容不回落全局 —— 宁可漏发一次，也不能把 A 的持仓发进 B 的群
        if personal:
            return {'ok': False, 'via': 'none', 'errors': errors}

    if personal and not chans:
        return {'ok': False, 'via': 'unbound',
                'errors': ['该账号还没有绑定推送通道，持仓提醒不会发给任何人']}
    if personal and errors:
        # 绑了但全失败：也不回落全局 —— 内容是个人的，宁可漏发一次也不能发错地方
        return {'ok': False, 'via': 'none', 'errors': errors}

    fb = _global_fallback_url()
    if fb:
        ok, err = _post_webhook(fb, content)
        if ok:
            return {'ok': True, 'via': 'global', 'errors': errors}
        errors.append(f'global {err}')
    # 全局兜底再走一次 aibot（运维者单聊）；它失败也不影响上面的结论
    try:
        if push_wecom_markdown(content):
            return {'ok': True, 'via': 'global-aibot', 'errors': errors}
    except Exception as e:
        errors.append(f'aibot {e}')
    print(f'[推送通道] ⚠️ 账号{user_id} 没有任何可用通道送达：{errors}')
    return {'ok': False, 'via': 'none', 'errors': errors}
