# .githooks —— 提交/推送前的硬拦截（零第三方依赖）

`AGENTS.md`、`CONTRIBUTING.md` 是指令，不是安全控制。写"禁止 `git add .`""禁止强推 main"
并不能阻止它发生；真正拦得住的是这里的两个 hook。它们只用 Git 自带的 `sh`，
不需要 `pre-commit` 框架、不需要 `gh`、不需要 pip 装任何东西。

## 启用（一次）

```sh
git config --local core.hooksPath .githooks
```

停用（回到改造前，逐字节等价）：

```sh
git config --local --unset core.hooksPath
```

## 拦什么

| 项 | 判据 | 拦截 | 失败时给出的做法 |
|---|---|---|---|
| B1 落后 | `git fetch origin <当前分支>` 成功，且 `! git merge-base --is-ancestor FETCH_HEAD HEAD` | **阻断** | `git pull --rebase origin <分支>` |
| B2 批量混提 | 暂存的新增（`--diff-filter=A`）非产物文件 > 8，或**非产物**暂存文件总数 > 20（构建产物不计入，否则正常前端提交必被拦） | **阻断** | `git reset` 后逐路径 `git add`；按主题拆提交 |
| B3 产物成对 | `server/static/assets/` 新增数 ≠ 删除数，或换了产物没带 `server/static/index.html` | **阻断** | 删增成对同提交 + 带上 index.html |
| B4 凭据/绝对路径 | 本次**新增行**里出现本机绝对路径（`D:\` `d:\` `C:\Users` `c:\users`）、带 key 的 webhook URL（`webhook/send?key=` 后跟 ≥8 位）、或 `corpsecret` 紧跟 ≥12 位字面量（扫 .py/.js/.ts/.vue/.json/.yaml，排除 `server/static`） | **阻断** | 路径走配置项；webhook 留在已被 `.gitignore` 排除的 `server/wecom_webhook.json` |
| B5 文档同步 | 改了 `server/**` 或 `client-vue/src/**` 但没暂存任何文档 | **只提醒**（不阻断） | 按 AGENTS.md 清单同步 5 份文档 |
| B6 事务持锁 | 本次**新增行**落在"同一函数体内第一条 DML 之后、下一个 `commit()` 之前"的窗口里，且窗口内出现 `await` / `requests` / `fetch_*` / `sleep` 等外部 IO（判据在 `server/scripts/tx_holding_scan.py`，白名单式） | **只提醒**（不阻断） | 外部拉数挪到事务之外，或逐条写完就 `conn.commit()` 把写锁交还 |
| B7 自测换库 | 暂存的 `server/scripts/*selftest*.py` 里「会写库却没有任何隔离（没 `use_db_path`、没把 `get_db_conn` 换成桩、也不是全临时路径）」，或给 `database`/`config`/`dal_state` 的 `SQLITE_DB_PATH` 赋值（11-05 拆分后静默无效），或直连 `gupiao.db`/`history_kline.db` 且没写 `mode=ro`（判据在 `server/scripts/db_hygiene_check.py`，规则 R1/R2/R3） | **阻断**（`analysis_out/` 里的同名一次性脚本只提示） | 顶部加 `import db.database as dbmod; dbmod.use_db_path(临时库)`；只读复核就写 `sqlite3.connect("file:…?mode=ro", uri=True)` |
| B8 日志编码 | 两条都**阻断**：**L1** = `server/app/main.py` 那段 stdout 编码兜底缺要件（没有 `reconfigure` 字样／有字样但没真调用／GBK 家族编码探测被摘／调用没包在 `try` 里／函数定义着但顶层那行调用被注释掉）；**L2** = 暂存的 `app/ core/ services/` 里有 GBK 编不出的运行期 `print` 语句（判据在 `server/scripts/log_encoding_guard.py`，用 AST 取整条 print，不看注释、不看送企微/前端的业务文本） | **阻断** | L1：把 `_make_log_utf8_safe()` 那块恢复回来（探到 gbk/cp936/gb2312/gb18030 才动手、收 UTF-8 + `errors='replace'`、包在 `try` 里、顶层真调用）。L2：符号换成 GBK 编得出的等价文本——`⚠️`→`[警告]`、`▸`→`>`、`◂`→`<`；别删说明文字 |
| pre-push | 远程 sha 不在本地历史里（非快进） | **阻断** | `git fetch` + `git rebase` 后正常 push；个人分支最多用 `--force-with-lease` |
| pre-push 全树 | push 时工作树里仍有**已跟踪但未提交**的改动（`M`/`A`/`D`/`R`；未跟踪 `??` 不拦） | **阻断** | 按 CONTRIBUTING 第 6 节的清单把这些路径也逐路径提交（别人的就在提交说明里标归属），或与用户确认后 `git stash push` 这批路径 |

B1 特别说明：`fetch` 失败（离线/代理异常）时**放行并打印警告**。这台机器常在代理异常状态，
把网络问题变成"提交不了"会让人直接停用整套机制，那比偶发漏检更糟。

B4 特别说明（v2，2026-11-03 收紧）：v1 用 `grep -F` 按**词面**匹配 `wecom_webhook` / `webhook_url`，
于是 `from services.support.push import push_wecom_webhook`、`_resolve_webhook_url` 这类正当写法
都被当成凭据（实测阻断了一次正常提交）。凭据不在函数名里，在**值**里，所以 v2 只认真凭据的三种形态。
收紧后当场做过双向验证：真凭据探针（带 key 的 URL、`corpsecret` 长字面量、绝对路径）三行全拦、退出码 1；
正当写法探针零拦截。它本来就拦不住"运行时从配置读出来的凭据"——那种东西不该出现在 diff 里。

B6 特别说明（2026-11-03 新增）：这一项**只提醒不阻断**，因为判据是白名单，宁可漏报也不误拦——
拦错一次的代价是逼人 `--no-verify`，那比不拦更糟。它依赖 `python`/`python3`/`py` 里任一个解释器
（脚本只用标准库），找不到就打印跳过、放行本次提交。已知漏报面：跨函数把 `conn` 传出去、
`with conn:` 上下文、显式 `BEGIN` 都检不到，新写的重函数不在白名单里也不认得。
上线时当场验过三件事：造一个"写完不提交就 `time.sleep`"的探针文件必须提醒；
`hooks.txlock.enabled false` 后必须零输出；探针提醒的中文输出必须是 UTF-8
（首跑就是栽在 Windows python 按 GBK 写 stdout、以及 `subprocess` 按 GBK 解码 git 输出，
后者会让行号区间取空、判据永不命中——两处都已在脚本里修掉并留了注释）。

pre-push 全树 特别说明（2026-11-05 新增，规矩出处见 AGENTS.md「推送是全树事务」与 CONTRIBUTING 第 6 节 ② 的前置清单）：
本项目多人／多会话共写同一个目录、同一个分支，"只推自己那部分"留下的**已跟踪**改动会在下一次
`git switch` / `git checkout` 时被静默收回——11-05 实测：模拟盘修复 21:41 提交、21:47 由看护加载上线，另一路
21:54~21:58 把 HEAD 切回 `main` 提交 routes 拆分，磁盘上的 `server/services/portfolio/trade_sim.py` 与那批文档行当场换版，
只有分支里还留着那份，线上内存与磁盘从此不一致。**上线时当场反证过**：工作树存在 `M server/db/database.py` 时脚本退出码 1
并列出全部残留路径；`hooks.pushwhole.enabled false` 之后同一条命令退出码 0、零输出。未跟踪（`??`）故意不拦——
切分支不会带走未跟踪文件，而本仓常年有几十个在建/产物文件，拦它们会天天误伤到逼人停用整套机制（那比漏检更糟）。

B7 特别说明（2026-11-07 新增）：这一项**阻断**，和 B5/B6 只提醒不同，理由是它的失效方式是「不报错」——写错库的自测照样打印全绿，假数据却进了生产库。本仓实拍两次：10-19 把 `2099-` 测试日期写进 `westock_snapshot`/`data_cross_check`，11-06 又一次污染；第二次的处置是「退出前连真库 DELETE 擦干净」，那等于拿生产库当擦布（11-07 已把 `fundflow_selftest`/`westock_crosscheck_selftest` 两处改成只读核对 + 漏了判 FAIL）。判据两次踩坑都写在检查器文件头：整行正则会被「函数名长名单字符串」骗（误报 `database_split_selftest`），整段调用原文会被「坏样本源码字符串」骗（误报它自己的长期自测）；现在只看 AST 的 callee 名判写、只看 `connect`/`execute` 的参数判路径与 SQL。双向验证：`python server/scripts/selftest_db_hygiene_selftest.py` 28/28（含「合法写法必须放行」那一侧），`db_hygiene_check.py --all` 对 `server/scripts/` 报 0 处阻断。

B8 特别说明（2026-11-07 新增；同日用户拍板后 L2 由"只报数"升级成阻断）：这一项**两条都拦**，理由是它们的失效方式都是"不报错"。L1 拦的是**保护消失**：看护把 stdout 重定向进 GBK 管道，10-26 起 `[推荐模式]` 那条告警就抛 `UnicodeEncodeError` 崩了 16 次、连带吞掉后面的汇总日志，全靠入口那段 `reconfigure` 治住。风险面读数（`--all` 实测）：升级前 **25 处 GBK 编不出的运行期 print / 扫 140 个文件**（11-02 文档写 17 处——担心的「计数随新增日志无感增长」确实发生了）；11-07 按用户决定把这 25 处换成 GBK 编得出的等价文本（`⚠️`→`[警告]`、`▸`→`>`、`◂`→`<`，8 个文件 25 行，只动 print 语句内部），**存量为 0 之后才把 L2 升级成阻断**——顺序很重要，反过来做会让当天所有提交都被拦死。入口兜底**保留不动**：它护的是 L2 管不到的那一类（第三方库自己打印、日志里拼接的库内业务文本——实测 `daily_recommendations.reason` 252 行里 251 行带 `📊`、15 行带 `⚠`），删它只有风险没有收益。附带后果，翻日志时要记得：从现在起运行期日志里的警告标记是 `[警告]`，旧日志仍是 `⚠️`，两者都 grep 一下才全。判据踩过的坑（写在检查器与自测里）：整行 grep 会漏多行 f-string（`print(` 在第一行、`⚠` 在第三行，两行单独都能编进 GBK），也会误报注释里的 `⚠`；改用 AST 取整条 print 语句。而 L1 的第一版更险——`ast.walk(stmt)` 会钻进函数体，把定义里的 `reconfigure` 当成「顶层调用过」，于是「函数还定义着、调用那行被注释掉」这种最像人犯的错**报不出红**；补了 `_top_level_call_names()` 显式跳过 def/class 子树，并把这个形态固化成反证②/⑤。

## 单独关某一项（每项独立开关，关掉必须完全等于没这项检查）

```sh
git config --local hooks.behind.enabled    false   # B1
git config --local hooks.bulkadd.enabled   false   # B2
git config --local hooks.artifacts.enabled false   # B3
git config --local hooks.secret.enabled    false   # B4
git config --local hooks.docsync.enabled   false   # B5
git config --local hooks.txlock.enabled    false   # B6
git config --local hooks.dbhygiene.enabled false   # B7
git config --local hooks.logencoding.enabled false  # B8
git config --local hooks.pushforce.enabled false   # pre-push
git config --local hooks.pushwhole.enabled false   # pre-push 全树
```

恢复：`git config --local --unset hooks.<名>.enabled`（缺省即启用）。

**不要用 `git commit --no-verify` 绕过。**绕过拦下来的东西，正是当初要装它的原因。

## 不产生提交地验收

```sh
git hook run pre-commit                      # 只跑 hook，不提交（git 2.36+，本机 2.55）
git push --dry-run --force origin <旧sha>:main   # dry-run 也会跑 pre-push，且不写远程

# B6 判据反证（扫描器自己也要被验一次，否则"0 命中"没有意义）：
#   已知有 bug 的旧版必须命中，当前版必须为 0——v1 就是因为按单行匹配，
#   对旧版也报 0，差点拿这个假结论去说"已根治"。
git show 92c732d:server/services/strategy/strategy.py > /tmp/strategy_old.py
python server/scripts/tx_holding_scan.py --selfcheck /tmp/strategy_old.py   # 期望：命中 1（行 870）
python server/scripts/tx_holding_scan.py --selfcheck server/services/strategy/strategy.py  # 期望：0
python server/scripts/tx_holding_scan.py                                    # 全仓扫描（人工排查）

# B7 判据双向验证（同样要拿坏样本反证，否则"报 0"没有意义）：
python server/scripts/selftest_db_hygiene_selftest.py   # 期望：28/28（含必须放行那一侧）
python server/scripts/db_hygiene_check.py --all         # 期望：阻断级 0（analysis_out 只提示）
# 现场演一次"真能拦住"（造一个坏自测、暂存、跑 hook、再撤掉，不产生提交）：
printf %s\n 'import db.database as d' 'd.SQLITE_DB_PATH="/tmp/x.db"' \
  'd.get_db_conn().execute("INSERT INTO t VALUES (1)")' > server/scripts/zz_probe_selftest.py
git add server/scripts/zz_probe_selftest.py
git hook run pre-commit; echo $?    # 期望：打印 R2/R1 并非 0（拦住了）
git reset -q server/scripts/zz_probe_selftest.py && rm server/scripts/zz_probe_selftest.py

# B8 判据双向验证（同一条规矩：先拿坏样本反证，再说它拦得住）
python server/scripts/log_encoding_guard_selftest.py   # 期望：25/25（L1 五种坏法 + L2 现场反证 + 注释/业务文本不误伤 + 合法写法放行那一侧）
python server/scripts/log_encoding_guard.py --all      # 期望：退出码 0，且 L2 读数已经是 0 处 / 扫 141 个文件（11-07 复量；文件数会随新增模块浮动，别把它当判据，判据是 0 处）（25 处存量已于 11-07 清零）
# 现场演一次"真能拦住"（只注释入口那行调用、不产生提交，跑完原样还原）：
python - <<'EOF'
import io
p='server/app/main.py'; s=io.open(p,encoding='utf-8').read()
io.open(p,'w',encoding='utf-8',newline='').write(s.replace('    _make_log_utf8_safe()\n','    # _make_log_utf8_safe()\n'))
EOF
git add server/app/main.py
python server/scripts/log_encoding_guard.py --staged; echo $?   # 期望：打印 L1「顶层没有调用它」并返回 1
# 还原（顺序要对：先把 index 退回 HEAD，再从 index 取回磁盘那份；反了会把坏版本又拿回来）：
git reset -q server/app/main.py && git checkout -- server/app/main.py
python server/scripts/log_encoding_guard.py --all | tail -1     # 期望：放行（退出码 0），且 `git status` 里 main.py 干净
```

## 还缺的一层（只能人工在网页做）

GitHub → Settings → Branches → Add branch protection rule：`main`（再对 `dev` 重复）
→ Do not allow force pushes、Do not allow deletions、Require a pull request before merging
（1 人审批，单人项目由你自己批）、Include administrators。
本机没有 `gh` CLI，所以这一层命令行验证不了；hook 只保证"我自己这台机器推不出非快进"。
