# -*- coding: utf-8 -*-
"""今日行动台截图验证（2026-10-13）

为什么要新写一个脚本而不是复用 shot_review.py：登录门禁上线后，
shot_review.py 里"打开首页→点 Tab"的假设已经失效（未登录只会停在登录页）。

用法（凭据只从命令行或环境变量来，脚本里不写死任何账号密码）:
  set GUPIAO_USER=你的账号
  set GUPIAO_PASS=你的密码
  python shot_desk.py                      # 默认三档办公室常见视口 × 当前时段
  python shot_desk.py tail                 # 只截尾盘版式（用 ?phase= 覆盖时段）
  python shot_desk.py all --dark           # 各时段 + 暗色各来一张

  也可以直接给已登录的 token（跳过登录接口）:
  python shot_desk.py --token=xxxx

产物：screenshots/desk_<视口>_<phase>[_dark].png
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright

BASE = os.environ.get('GUPIAO_BASE', 'http://127.0.0.1:3003')
OUTDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'screenshots')

# 办公室真实场景：1280 全屏 / 860 半屏并排（最常见）/ 420 手机核对
VIEWPORTS = [
    ('wide', 1280, 900),
    ('narrow', 860, 900),
    ('phone', 420, 860),
]
PHASES = ['auction', 'morning', 'lunch', 'mid', 'tail', 'post', 'closed']
TOKEN_KEY = 'gupiao_auth_token'
USER_KEY = 'gupiao_auth_user'


def login(username: str, password: str):
    """调用真实登录接口拿 token + user，字段形状与 /api/auth/login 一致"""
    req = urllib.request.Request(
        f'{BASE}/api/auth/login',
        data=json.dumps({'username': username, 'password': password}).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read().decode('utf-8'))
    if not payload.get('success'):
        raise SystemExit(f'登录失败: {payload}')
    return payload['data']['token'], payload['data']['user']


def desk_probe(page, phase: str):
    """读取一次行动台接口，把区块/降级情况打到控制台，便于确认不是"截图看着还行但其实空数据" """
    raw = page.evaluate(f"""async () => {{
      const r = await fetch('/api/action-desk' + ('{phase}' ? '?phase={phase}' : ''), {{
        headers: {{ Authorization: 'Bearer ' + localStorage.getItem('{TOKEN_KEY}') }}
      }});
      return await r.json();
    }}""")
    if not raw.get('success'):
        print('    ! 接口返回失败:', raw)
        return
    d = raw['data']
    print(f"    phase={d['phase']} 区块={d['sections']} "
          f"持仓待办={len(d['positionActions'])} 可买={len(d['buyNow'])} "
          f"明日计划={len(d['tomorrowPlan'])} 自选异动={len(d['watchlistAlerts'])} "
          f"降级={d['degraded'] or '无'}")
    print(f"    headline: {d['headline'].get('text')} / {d['headline'].get('sub')}")


def main():
    ap = argparse.ArgumentParser(description='行动台多视口截图')
    ap.add_argument('phase', nargs='?', default='',
                    help='固定某个时段（auction/morning/lunch/mid/tail/post/closed），留空=当前真实时段')
    ap.add_argument('--all', action='store_true', help='遍历全部时段')
    ap.add_argument('--dark', action='store_true', help='同时截暗色')
    ap.add_argument('--token', default='', help='已有 token（跳过登录）')
    ap.add_argument('--user', default=os.environ.get('GUPIAO_USER', ''))
    ap.add_argument('--pass', dest='password', default=os.environ.get('GUPIAO_PASS', ''))
    args = ap.parse_args()

    token, user = args.token, {'username': 'token-user', 'displayName': 'token-user'}
    if not token:
        if not args.user or not args.password:
            raise SystemExit('需要凭据：set GUPIAO_USER/GUPIAO_PASS，或传 --user/--pass，或用 --token 复用已登录会话')
        token, user = login(args.user, args.password)

    phases = PHASES if args.all else ([args.phase] if args.phase else [''])
    os.makedirs(OUTDIR, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for vp_name, w, h in VIEWPORTS:
            ctx = browser.new_context(viewport={'width': w, 'height': h})
            # 登录态必须在 goto 之前写进 localStorage，否则会先撞登录页
            ctx.add_init_script(
                f"localStorage.setItem('{TOKEN_KEY}', '{token}');"
                f"localStorage.setItem('{USER_KEY}', '{json.dumps(user, ensure_ascii=False)}');"
            )
            page = ctx.new_page()
            for phase in phases:
                for dark in (False, True) if args.dark else (False,):
                    url = f'{BASE}/#/desk' + (f'?phase={phase}' if phase else '')
                    page.goto(url, wait_until='networkidle', timeout=45000)
                    # 主题覆盖同样要在渲染前落地：data-theme 由 index.html 内联脚本 + localStorage 决定
                    page.evaluate(
                        f"localStorage.setItem('gupiao_theme', '{'dark' if dark else 'light'}');"
                        "document.documentElement.dataset.theme = localStorage.getItem('gupiao_theme');"
                    )
                    page.reload(wait_until='networkidle')
                    page.wait_for_timeout(2500)
                    if vp_name == 'wide':
                        desk_probe(page, phase)
                    suffix = f"_{phase}" if phase else '_now'
                    tag = f"{vp_name}{suffix}{'_dark' if dark else ''}"
                    out = os.path.join(OUTDIR, f'desk_{tag}.png')
                    page.screenshot(path=out, full_page=True)
                    print(f'  saved: {os.path.basename(out)}  ({w}x{h})')
            ctx.close()
        browser.close()
    print('完成。目录:', OUTDIR)


if __name__ == '__main__':
    sys.exit(main())
