import json
import os
from datetime import datetime

# ========== 多用户账号与认证（2026-10-12） ==========
# 背景：持仓是每个用户私有的，行情/推荐/收评是公共的 —— 因此只对持仓等私有资源做认证，
# 公共接口保持免登录，收评定时任务、截图脚本等既有自动化不受影响。
AUTH_ENABLED = True             # 总开关（置 False 可整体回退到免登录模式，持仓归 user_id=1）
# 2026-10-13：cloudflare 隧道暴露公网期间临时置 False，隧道关闭后恢复 True。
# 下次若要再次对外分享链接，记得先把它置 False（自助注册 = 任何人都能进来读全量公共数据）。
AUTH_ALLOW_REGISTER = False      # 2026-10-14 隧道暴露公网期间临时关闭自助注册，隧道关闭后恢复 True
AUTH_SESSION_DAYS = 30          # 会话有效期（天）
AUTH_MIN_PASSWORD_LEN = 6       # 最短密码长度
AUTH_PBKDF2_ROUNDS = 120000     # PBKDF2-SHA256 迭代轮数（标准库实现，无额外依赖）
AUTH_MAX_LOGIN_FAILS = 5        # 同账号连续登录失败上限，超过后临时锁定
AUTH_LOCK_MINUTES = 5           # 登录失败锁定时长（分钟）


__all__ = ['AUTH_ALLOW_REGISTER', 'AUTH_ENABLED', 'AUTH_LOCK_MINUTES', 'AUTH_MAX_LOGIN_FAILS', 'AUTH_MIN_PASSWORD_LEN', 'AUTH_PBKDF2_ROUNDS', 'AUTH_SESSION_DAYS']
