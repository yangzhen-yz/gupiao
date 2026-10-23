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
| B4 凭据/绝对路径 | 本次**新增行**里出现 `D:\` `C:\Users` `corpsecret` `webhook_url` `wecom_webhook`（扫 .py/.js/.ts/.vue/.json/.yaml，排除 `server/static`） | **阻断** | 路径走配置项；webhook 留在已被 `.gitignore` 排除的 `server/wecom_webhook.json` |
| B5 文档同步 | 改了 `server/**` 或 `client-vue/src/**` 但没暂存任何文档 | **只提醒**（不阻断） | 按 AGENTS.md 清单同步 5 份文档 |
| pre-push | 远程 sha 不在本地历史里（非快进） | **阻断** | `git fetch` + `git rebase` 后正常 push；个人分支最多用 `--force-with-lease` |

B1 特别说明：`fetch` 失败（离线/代理异常）时**放行并打印警告**。这台机器常在代理异常状态，
把网络问题变成"提交不了"会让人直接停用整套机制，那比偶发漏检更糟。

## 单独关某一项（每项独立开关，关掉必须完全等于没这项检查）

```sh
git config --local hooks.behind.enabled    false   # B1
git config --local hooks.bulkadd.enabled   false   # B2
git config --local hooks.artifacts.enabled false   # B3
git config --local hooks.secret.enabled    false   # B4
git config --local hooks.docsync.enabled   false   # B5
git config --local hooks.pushforce.enabled false   # pre-push
```

恢复：`git config --local --unset hooks.<名>.enabled`（缺省即启用）。

**不要用 `git commit --no-verify` 绕过。**绕过拦下来的东西，正是当初要装它的原因。

## 不产生提交地验收

```sh
git hook run pre-commit                      # 只跑 hook，不提交（git 2.36+，本机 2.55）
git push --dry-run --force origin <旧sha>:main   # dry-run 也会跑 pre-push，且不写远程
```

## 还缺的一层（只能人工在网页做）

GitHub → Settings → Branches → Add branch protection rule：`main`（再对 `dev` 重复）
→ Do not allow force pushes、Do not allow deletions、Require a pull request before merging
（1 人审批，单人项目由你自己批）、Include administrators。
本机没有 `gh` CLI，所以这一层命令行验证不了；hook 只保证"我自己这台机器推不出非快进"。
