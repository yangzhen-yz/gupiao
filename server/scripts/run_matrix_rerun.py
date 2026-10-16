"""run_matrix_rerun.py - 等 baostock 冷却解除后，把买法矩阵整体重跑成 2462 口径（一次性）

为什么需要"等冷却 + 一次性"这两个约束（2026-10-16 实测）：
  · 分时那张表（`intraday_matrix.py`）要按事件抽样去补 5 分钟线，绕不开 baostock，
    而本机 IP 今晚被判定 `10001011 黑名单用户`，冷却解除前跑 = 白跑 + 可能续封；
  · 重跑一遍四张表要几十分钟，不能占着调度器那根单线程循环（会推迟 09:00 的海选），
    所以由 `core/scheduler.py` 以**脱离进程**的方式拉起本脚本，跑完写完成标记；
  · 产物一律落到 `server/analysis_out/` —— 今晚这批 .txt 在仓库重构（services 拆包）
    时被清掉过一次，数字白算了一遍，别再犯同样的错。

用法：python scripts/run_matrix_rerun.py [--force] [--max-wait 240]
"""
import argparse
import os
import subprocess
import sys
import time
from datetime import datetime

SERVER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT_DIR = os.path.join(SERVER, 'scripts')
OUT_DIR = os.path.join(SERVER, 'analysis_out')
FLAG = os.path.join(OUT_DIR, '.done_matrix_2462')
LOG = os.path.join(OUT_DIR, 'rerun.log')
EXE = sys.executable

# 顺序有讲究：前三张只吃本地日K（不碰 baostock，先出数），分时那张才需要分钟线
SCRIPTS = [('crash_profile', '形态分层与尾部风险'),
           ('buy_rule_stats', '买法逐条称重'),
           ('timing_matrix', '日K四锚点：竞价/尾盘/追最高/挂单低吸'),
           ('intraday_matrix', '真实分钟六时点')]


def log(msg: str) -> None:
    line = f'[{datetime.now():%m-%d %H:%M:%S}] {msg}'
    print(line, flush=True)
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        pass


def cooldown_min() -> float:
    sys.path.insert(0, SERVER)
    try:
        from services.data.kline_history import ban_cooldown_remaining
        return ban_cooldown_remaining()
    except Exception as e:
        log(f'读不到冷却状态，按 0 处理: {e}')
        return 0.0


def wait_for_cooldown(max_wait_min: int) -> bool:
    t0 = time.time()
    while True:
        left = cooldown_min()
        if left <= 0:
            return True
        if (time.time() - t0) / 60.0 > max_wait_min:
            log(f'等了 {max_wait_min} 分钟 baostock 仍在冷却（剩 {int(left)} 分钟），'
                f'本次不跑分时；前三张只吃本地日K，仍然会跑')
            return False
        log(f'baostock 冷却中（剩 {int(left)} 分钟），5 分钟后再看')
        time.sleep(300)


def _scope_ok(out_path: str) -> tuple:
    """产物第一行必须证明它真的按选股范围筛过（防"静默全市场"）。

    2026-10-16 的实测教训：脚本搬进 scripts/ 后 sys.path 引导失效，`import services` 报错，
    旧版 apply_universe 只 print 一句就"不过滤继续"，于是三份产物都变成了全市场 218 万条的数
    —— 看着完整、其实是错数，而且退出码还是 0。所以现在把"出数"和"出对数"分开判。
    """
    try:
        sys.path.insert(0, SERVER)
        from services.data import universe
        want = universe.selectable_count()    # 产物筛选后应该剩下的只数（选票范围）
    except Exception as e:
        return False, f'读不到选票范围数量: {e}'
    try:
        with open(out_path, encoding='utf-8', errors='replace') as f:
            head = ''.join([f.readline() for _ in range(3)])
    except OSError as e:
        return False, f'产物读不到: {e}'
    if f'→ {want}' in head.replace(',', '') or f'→ {want:,}' in head:
        return True, f'选股范围 {want} 只已生效'
    return False, (f'产物头部没有"→ {want}"的筛选凭证，疑似全市场口径：{head.strip()[:120]}')


def run_one(name: str, desc: str) -> bool:
    out = os.path.join(OUT_DIR, f'{name}_2462.txt')
    log(f'开始：{name}（{desc}）→ analysis_out/{name}_2462.txt')
    t0 = time.time()
    try:
        with open(out, 'w', encoding='utf-8') as fh:
            r = subprocess.run([EXE, os.path.join(SCRIPT_DIR, f'{name}.py')],
                               cwd=SERVER, stdout=fh, stderr=subprocess.STDOUT, timeout=5400)
        if r.returncode != 0:
            log(f'失败(退出码 {r.returncode})：{name}，耗时 {int(time.time() - t0)}s')
            return False
        ok, why = _scope_ok(out)
        log(f'{"完成" if ok else "出数但口径不对"}：{name}，耗时 {int(time.time() - t0)}s｜{why}')
        return ok
    except Exception as e:
        log(f'异常：{name}: {type(e).__name__}: {e}')
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--force', action='store_true', help='忽略完成标记，重跑一遍')
    ap.add_argument('--max-wait', type=int, default=240, help='最多等多少分钟解除冷却')
    a = ap.parse_args()
    os.makedirs(OUT_DIR, exist_ok=True)
    if os.path.exists(FLAG) and not a.force:
        log(f'已跑过（{FLAG}），要重跑加 --force')
        return 0
    log('=== 买法矩阵重跑（选票范围：主板·非ST/退·市值≥35亿·日均额>0.8亿·换手>1.5%·价>3元）===')
    ready = wait_for_cooldown(a.max_wait)
    done = []
    for name, desc in SCRIPTS:
        if name == 'intraday_matrix' and not ready:
            log('分时那张跳过（baostock 仍在冷却），前三张先出数；分时等下次带 --force 再跑')
            continue
        done.append((name, run_one(name, desc)))
    all_ok = all(ok for _, ok in done) and len(done) == len(SCRIPTS)
    if all_ok:
        with open(FLAG, 'w', encoding='utf-8') as f:
            f.write(datetime.now().isoformat())
        log('四张表全部出数，写完成标记 → 可以更新 docs/短线买法-2026-10.md 的口径与表格')
    else:
        log(f'未全部完成（{[(n, o) for n, o in done]}），不写完成标记，下次 02:10 会重试')
    return 0 if all_ok else 1


if __name__ == '__main__':
    sys.exit(main())
