"""scheduler_guard_selftest.py - 守"一个坏 job 不得冻结整条定时任务链"（2026-11-06）

为什么这份自测值得长期留着：11-06 03:30 的 `job_db_backup` 少一个 `import os`，
`schedule.run_pending()` 每次都在它身上抛异常。看日志像是"只刷了 4115 条错误"，
实际是**整天定时任务全停**：09:00 海选、09:15 竞价快照、09:45/10:00/13:30/14:05 推荐、
15:35 自动优化、每 60s 接口用量落库一条都没跑（当天 `daily_candidate_pool`、
`daily_recommendations`、`strategy_snapshot`、`auction_snapshot`、`cls_plate_daily` 全是 0 行），
只有主循环里的收评/回测自愈在 15:10/15:20 补回两项。用户侧的表现就是"今天什么都没推"。

根因在 schedule 库的实现细节：`Job.run()` 是「先执行 job_func，执行完才推进 next_run」，
而 `run_pending()` 是 `for job in sorted(runnable): _run_job(job)`。所以 job_func 抛异常时
next_run 不推进 → 这个 job 下一轮仍"到点"且排序第一 → for 循环再次被打断 → 永久卡死。
`core/scheduler.py::_run_pending_guarded()` 的做法是捕获异常后把"已到点却没跑成"的 job
推进一格并点名，其余任务照常。

这里断言三件事：
  1. 有坏 job 时，好 job 仍然被执行（改造前不会）；
  2. 坏 job 被点名并推进到未来，不再每轮重复抛；
  3. 反证：直接用 `schedule.run_pending()` 时好 job 一次都跑不到——证明这条不变量真的在防事。
不启动调度线程、不联网、不碰库。
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import schedule                                  # noqa: E402
from core import scheduler as SCH                # noqa: E402

PASS, FAIL = [], []


def ck(cond, msg):
    (PASS if cond else FAIL).append(msg)
    print(f"  [{'✓' if cond else '✗'}] {msg}")


def _setup():
    """注册一坏一好两个 job，并把 next_run 都压到过去，模拟"此刻同时到点"。

    排序按 next_run：坏 job 先建、时间更早 ⇒ 一定排在好 job 前面，正好复现现场。
    """
    schedule.clear()
    state = {'good': 0}

    def _bad():
        raise NameError("name 'os' is not defined")

    def _good():
        state['good'] += 1

    jb = schedule.every(1).seconds.do(_bad)
    jg = schedule.every(1).seconds.do(_good)
    past = datetime.now() - timedelta(seconds=30)
    jb.next_run = past
    jg.next_run = past + timedelta(milliseconds=1)   # 坏 job 排序在前
    return state, jb, jg


print('[1] 改造后：坏 job 抛异常，好 job 照常执行')
state, jb, jg = _setup()
SCH._run_pending_guarded()
ck(state['good'] >= 1, f'好 job 本轮被执行到（实得 {state["good"]} 次）——改造前这里是 0 次')
ck(jb.next_run > datetime.now(), f'坏 job 已被推进到未来（{jb.next_run:%H:%M:%S}），不再每轮重复抛')

print('\n[2] 第二轮：坏 job 不到期就不该再拖住别人')
state2, jb2, jg2 = _setup()
SCH._run_pending_guarded()          # 第一轮：坏 job 抛 + 被推进
jg2.next_run = datetime.now() - timedelta(seconds=1)   # 只让好 job 再次到点
SCH._run_pending_guarded()
ck(state2['good'] >= 2, f'第二轮好 job 仍执行（累计 {state2["good"]} 次）')
ck(jb2.last_run is None or jb2.last_run < jb2.next_run,
   '坏 job 没有 last_run（它确实每次都失败），推进只针对"到点没跑成"，不影响别人')

print('\n[3] 反证：直接用 schedule.run_pending() 时，好 job 一次都跑不到')
state3, jb3, jg3 = _setup()
raised = False
try:
    schedule.run_pending()
except Exception:
    raised = True
ck(raised, '异常确实从 run_pending 里逃出来（这就是当天 4115 条日志的来源）')
ck(state3['good'] == 0, f'好 job 一次都没执行（实得 {state3["good"]}）——证明旧写法真会冻结整条链')
ck(jb3.next_run < datetime.now(), '坏 job 的 next_run 仍停在今天 ⇒ 下一轮它又排第一（永久卡死的机制）')

print('\n[4] 没有坏 job 时，guarded 不得改变任何行为')
state4, jb4, jg4 = _setup()
schedule.clear()
cnt = {'n': 0}

def _only_good():
    cnt['n'] += 1

jo = schedule.every(1).seconds.do(_only_good)
jo.next_run = datetime.now() - timedelta(seconds=5)
SCH._run_pending_guarded()
ck(cnt['n'] == 1, f'正常路径：到点就跑一次（实得 {cnt["n"]}）')
ck(jo.next_run > datetime.now(), '正常路径：next_run 由库自己推进，guarded 不插手')

schedule.clear()
print()
print(f'===== 结果：{len(PASS)} 通过 / {len(FAIL)} 失败 =====')
if FAIL:
    for f in FAIL:
        print(f'  ✗ {f}')
    sys.exit(1)
print('全部通过 ✓')
