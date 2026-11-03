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
| pre-push | 远程 sha 不在本地历史里（非快进） | **阻断** | `git fetch` + `git rebase` 后正常 push；个人分支最多用 `--force-with-lease` |

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

## 单独关某一项（每项独立开关，关掉必须完全等于没这项检查）

```sh
git config --local hooks.behind.enabled    false   # B1
git config --local hooks.bulkadd.enabled   false   # B2
git config --local hooks.artifacts.enabled false   # B3
git config --local hooks.secret.enabled    false   # B4
git config --local hooks.docsync.enabled   false   # B5
git config --local hooks.txlock.enabled    false   # B6
git config --local hooks.pushforce.enabled false   # pre-push
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
```

## 还缺的一层（只能人工在网页做）

GitHub → Settings → Branches → Add branch protection rule：`main`（再对 `dev` 重复）
→ Do not allow force pushes、Do not allow deletions、Require a pull request before merging
（1 人审批，单人项目由你自己批）、Include administrators。
本机没有 `gh` CLI，所以这一层命令行验证不了；hook 只保证"我自己这台机器推不出非快进"。
