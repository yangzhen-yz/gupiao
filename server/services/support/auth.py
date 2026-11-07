"""auth.py - 多用户账号与会话管理（2026-10-12 新增）

为什么只做「私有数据隔离」而不是全站登录门禁：
  持仓是每个用户私有的，必须隔离；而行情/推荐/收评/趋势是公共数据，
  收评定时任务、推荐快照、截图脚本等既有自动化都直接打这些接口。
  因此只对持仓等私有资源加认证，公共接口保持免登录 —— 零副作用地满足需求。

实现要点：
  1. 密码：标准库 hashlib.pbkdf2_hmac('sha256') + 随机盐，不存明文、不引入第三方依赖。
  2. 会话：secrets.token_urlsafe 随机 token 存库，可撤销、可过期（无需 JWT 依赖）。
  3. 存量持仓已随迁移落到 user_id=1；首个注册用户自增 id 即为 1，天然认领原持仓。
  4. 登录失败计数走进程内内存，重启清空 —— 内部系统的最小防爆破够用。
"""
import hashlib, re, secrets, threading, time as _time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import (AUTH_ENABLED, AUTH_ALLOW_REGISTER, AUTH_SESSION_DAYS,
                        AUTH_MIN_PASSWORD_LEN, AUTH_PBKDF2_ROUNDS,
                        AUTH_MAX_LOGIN_FAILS, AUTH_LOCK_MINUTES)
from db.database import get_db_conn

# 用户名：2-20 位字母/数字/下划线/中文/点/短横线
_USERNAME_RE = re.compile(r'^[\w一-鿿.\-]{2,20}$')
_bearer = HTTPBearer(auto_error=False)

# 登录失败计数：username(小写) -> (连续失败次数, 最后失败时间戳)
_fail_state: Dict[str, Tuple[int, float]] = {}
_fail_lock = threading.Lock()


# ========== 内部工具 ==========
def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        'sha256', password.encode('utf-8'), salt.encode('utf-8'),
        AUTH_PBKDF2_ROUNDS).hex()


def _verify_password(password: str, salt: str, expected: str) -> bool:
    return secrets.compare_digest(_hash_password(password, salt), expected)


def _now_utc() -> str:
    """统一用 UTC 存储/比较，与表内 datetime('now','+8 hours') 默认值同精度格式。"""
    return datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')


# ========== 用户 ==========
def create_user(username: str, password: str, display_name: str = ''
                ) -> Tuple[bool, str, Optional[Dict]]:
    """注册用户。返回 (是否成功, 提示信息, 用户公开信息)"""
    username = (username or '').strip()
    password = password or ''
    display_name = (display_name or '').strip()

    if not _USERNAME_RE.match(username):
        return False, '用户名需为 2-20 位字母/数字/中文/下划线/点/短横线', None
    if len(password) < AUTH_MIN_PASSWORD_LEN:
        return False, f'密码至少 {AUTH_MIN_PASSWORD_LEN} 位', None

    try:
        conn = get_db_conn(wait_for_lock=True)
        cur = conn.cursor()
        cur.execute('SELECT id FROM users WHERE username = ?', (username,))
        if cur.fetchone():
            conn.close()
            return False, '该用户名已被注册', None
        salt = secrets.token_hex(16)
        # id 显式按 max+1 分配（不用自增）：保证「系统首位注册者」拿到 id=1，
        # 与迁移时存量持仓归属的 user_id=1 对齐 —— 否则 sqlite_sequence 前进过
        # 之后首个用户拿不到 1，存量持仓会变成无主的孤儿数据。
        next_id = int(cur.execute('SELECT COALESCE(MAX(id), 0) FROM users').fetchone()[0]) + 1
        cur.execute('''INSERT INTO users (id, username, password_hash, salt, display_name)
                       VALUES (?, ?, ?, ?, ?)''',
                    (next_id, username, _hash_password(password, salt), salt,
                     display_name or username))
        conn.commit()
        uid = next_id
        conn.close()
        return True, '注册成功', {'id': uid, 'username': username,
                                  'displayName': display_name or username,
                                  'isAdmin': False}
    except Exception as error:
        return False, f'注册失败: {error}', None


def has_any_user() -> bool:
    """是否已存在账号（前端据此判断显示「注册」还是仅「登录」）"""
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute('SELECT COUNT(*) AS n FROM users')
        row = cur.fetchone()
        conn.close()
        return bool(row and row['n'] > 0)
    except Exception:
        return False


def list_users() -> List[Dict]:
    """用户列表（不含任何密码/盐字段），供前端「切换用户」用"""
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute('''SELECT id, username, display_name, created_at, last_login_at
                       FROM users ORDER BY id''')
        rows = cur.fetchall()
        conn.close()
        return [{'id': r['id'], 'username': r['username'],
                 'displayName': r['display_name'] or r['username'],
                 'createdAt': r['created_at'], 'lastLoginAt': r['last_login_at']}
                for r in rows]
    except Exception as error:
        print(f'读取用户列表失败: {error}')
        return []


def change_password(user_id: int, old_password: str, new_password: str
                    ) -> Tuple[bool, str]:
    """修改密码（需校验原密码），成功后吊销该用户全部会话"""
    new_password = new_password or ''
    if len(new_password) < AUTH_MIN_PASSWORD_LEN:
        return False, f'新密码至少 {AUTH_MIN_PASSWORD_LEN} 位'
    try:
        conn = get_db_conn(wait_for_lock=True)
        cur = conn.cursor()
        cur.execute('SELECT * FROM users WHERE id = ?', (int(user_id),))
        row = cur.fetchone()
        if not row:
            conn.close()
            return False, '用户不存在'
        if not _verify_password(old_password or '', row['salt'], row['password_hash']):
            conn.close()
            return False, '原密码错误'
        salt = secrets.token_hex(16)
        cur.execute('UPDATE users SET password_hash = ?, salt = ? WHERE id = ?',
                    (_hash_password(new_password, salt), salt, int(user_id)))
        cur.execute('DELETE FROM user_sessions WHERE user_id = ?', (int(user_id),))
        conn.commit()
        conn.close()
        return True, '密码已修改，请重新登录'
    except Exception as error:
        return False, f'修改失败: {error}'


# ========== 会话 ==========
def authenticate(username: str, password: str
                 ) -> Tuple[Optional[Dict], Optional[str], Optional[str]]:
    """校验账号密码并签发会话。返回 (user, token, error)，三者互斥"""
    username = (username or '').strip()
    key = username.lower()

    with _fail_lock:
        fails, last_at = _fail_state.get(key, (0, 0.0))
        if fails >= AUTH_MAX_LOGIN_FAILS and (_time.time() - last_at) < AUTH_LOCK_MINUTES * 60:
            left = int(AUTH_LOCK_MINUTES * 60 - (_time.time() - last_at))
            return None, None, f'密码错误次数过多，请 {left} 秒后再试'

    try:
        conn = get_db_conn(wait_for_lock=True)
        cur = conn.cursor()
        cur.execute('SELECT * FROM users WHERE username = ?', (username,))
        row = cur.fetchone()

        if not row or not _verify_password(password or '', row['salt'], row['password_hash']):
            with _fail_lock:
                f, _l = _fail_state.get(key, (0, 0.0))
                _fail_state[key] = (f + 1, _time.time())
            conn.close()
            # 不区分「用户不存在」与「密码错误」，避免账号枚举
            return None, None, '用户名或密码错误'

        with _fail_lock:
            _fail_state.pop(key, None)

        # 顺手清理过期会话，避免表无限膨胀
        cur.execute('DELETE FROM user_sessions WHERE expires_at < ?', (_now_utc(),))
        token = secrets.token_urlsafe(32)
        expires_at = (datetime.utcnow() + timedelta(days=AUTH_SESSION_DAYS)
                      ).strftime('%Y-%m-%d %H:%M:%S')
        cur.execute('''INSERT INTO user_sessions (token, user_id, expires_at)
                       VALUES (?, ?, ?)''', (token, row['id'], expires_at))
        cur.execute('UPDATE users SET last_login_at = ? WHERE id = ?',
                    (_now_utc(), row['id']))
        conn.commit()
        user = {'id': row['id'], 'username': row['username'],
                'displayName': row['display_name'] or row['username'],
                'isAdmin': bool(row['is_admin'] or 0)}
        conn.close()
        return user, token, None
    except Exception as error:
        return None, None, f'登录失败: {error}'


def get_user_by_token(token: str) -> Optional[Dict]:
    """凭 token 取当前用户；token 无效/过期返回 None"""
    if not token:
        return None
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute('''SELECT u.id, u.username, u.display_name, u.is_admin, s.expires_at
                       FROM user_sessions s JOIN users u ON u.id = s.user_id
                       WHERE s.token = ?''', (token,))
        row = cur.fetchone()
        conn.close()
        if not row:
            return None
        if row['expires_at'] < _now_utc():
            logout(token)
            return None
        return {'id': row['id'], 'username': row['username'],
                'displayName': row['display_name'] or row['username'],
                'isAdmin': bool(row['is_admin'] or 0)}
    except Exception as error:
        print(f'会话校验失败: {error}')
        return None


def logout(token: str) -> bool:
    """注销会话（吊销 token）"""
    if not token:
        return False
    try:
        conn = get_db_conn(wait_for_lock=True)
        cur = conn.cursor()
        cur.execute('DELETE FROM user_sessions WHERE token = ?', (token,))
        conn.commit()
        conn.close()
        return True
    except Exception as error:
        print(f'注销失败: {error}')
        return False


# ========== FastAPI 依赖 ==========
async def current_user(request: Request,
                        creds: HTTPAuthorizationCredentials = Depends(_bearer)) -> Dict:
    """私有资源接口的依赖：未登录/会话过期 → 401。

    支持两种传 token 方式：Authorization: Bearer <token> 或 X-Auth-Token 头。
    """
    if not AUTH_ENABLED:
        # 整体关闭认证时的兼容模式：所有请求视为 1 号本地用户
        return {'id': 1, 'username': 'local', 'displayName': '本地模式'}
    token = ''
    if creds is not None and creds.credentials:
        token = creds.credentials
    if not token:
        token = request.headers.get('X-Auth-Token', '')
    user = get_user_by_token(token)
    if not user:
        raise HTTPException(status_code=401, detail='未登录或登录已过期，请重新登录')
    return user
