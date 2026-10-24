# CONTRIBUTING.md

这份是**给人看的协作规则**。给 AI Agent 的约束在 `AGENTS.md`，不依赖自觉的硬拦截在 `.githooks/`。
三者分工：规则写在这里说明"怎么做是对的"；`AGENTS.md` 约束 Agent 每次动手前后的动作；
`.githooks/` 负责"你没照做就提交不了"。规则本身拦不住任何东西——只有分支保护和 hook 拦得住。

## 1. 分支模型

```
main          唯一长期主干，受保护：禁止直接 push、禁止 force push、禁止删除，只能经 PR 合入
dev           集成分支，同样受保护：feat/fix 先合 dev，dev 阶段性合 main
feat/<描述>    功能开发，每人每任务一个，短命：合入即删
fix/<描述>     缺陷修复，同上
```

- 分支名只用小写英文 + 短横线，禁止中文与空格（`feat/hot-search-ths-backup` 这种，不要 `feat/同花顺备源`）。
- 现状说明（2026-10-24 更新）：`origin/dev` 已快进到与 `origin/main` 同点（`076a156`，`git rev-list --count origin/dev..origin/main` 实测 0），是干净的集成分支；
  `feat/action-desk` 与两条待审 docs 分支已于同日删除，当前远端只剩 `main` 与 `dev`。
- **`dev` 无独有提交时才允许直接快进对齐**（2026-10-24 实测过一次：`git push origin main:dev`，属快进、非强推，不产生新提交也不引入未审内容）；
  一旦 `dev` 上有独有提交，就必须回到本模型的 PR 流程（feat/fix → dev → main），不得直推。
- **本仓特有的硬规则：服务只在 `main` 上运行。** `:3003` 由仓库外的计划任务（SYSTEM 账户）以固定路径
  `D:\Desktop\guizhou\gupiao\server` 拉起，磁盘上换成哪个分支的代码，线上就立刻跑哪个分支。因此：
  - 盘中 **09:10~15:10 禁止 `git switch` / `git merge` / `git rebase`**（会打断 09:45 / 10:00 / 13:30 / 14:05 / 14:45 批次与情绪快循环）；
  - 要在不动线上的前提下长时间实验，用 `git worktree add` 另开一棵树，但那棵树**不得起 3003**，且 `server/data/`（数据库）不在版本控制里，需要显式指向同一数据目录或另建库——**双树并发写同一个 SQLite 的风险未验证，不作为默认做法**；
  - 改动合入 `main` 后要生效，走既有标志重启：写 `server/data/backend_restart_requested`，看护 ≤5 分钟自己重启。
    不要用 `taskkill` 杀那个进程（它属 SYSTEM，会话里杀不掉；你新起的进程只会因 `[Errno 10048]` 端口占用退出，而日志照样打"startup complete"，看着像重启成功其实还在跑旧代码）。
  - **合入后先确认 fetch 成功，再切回 `main`**。这台机器到 `github.com:443` 是间歇性不通（实测 2026-10-23 深夜：`api.github.com` 返回 200，但 `git ls-remote origin` 全部 `Failed to connect`），
    此时本地 `refs/remotes/origin/main` 还是**合并前的旧值**。在这种状态下 `git switch main` 会把工作树换成旧代码，而线上服务直接读这份工作树——等于把刚合入的东西从线上撤掉。
    判据：`git fetch --prune origin` 必须成功，且 `git log --oneline -3 origin/main` 里能看到预期的合并提交，才允许切回；fetch 不通就停在原分支等网络。

## 2. PR / MR 前置条件

- 提 PR 前必须执行：`git fetch origin && git rebase origin/<目标分支>`（`dev` 或 `main`），确保基于最新主线。
- 一个 PR 只做一件事：**不要把重构和修 bug 混在同一次提交**。本仓历史上单次提交 33~69 个文件，是出问题时报不出"这次改了什么"的直接原因。
- 提交信息沿用现有风格：`feat|fix|perf|chore|docs(中文scope): 中文描述`，例：`feat(热搜榜备源): 东财人气榜为空时回落同花顺`。
- 前端改动必须与构建产物同提交：`server/static/assets/` 的删、增成对，且带上 `server/static/index.html`
  （产物入库是有意策略，`client-vue/vite.config.js` 的 outDir 就是 `../server/static`，干净检出要能直接起服务）。
- 本仓没有 CI、没有第二个人：Review 由仓库所有者在网页点头，`.githooks` 是替代"机器审查"的那一层。

## 3. 冲突与覆盖的处理规范

- **禁止用 `git push --force` 解决冲突。** 个人分支 rebase 后确需强推时，只允许 `git push --force-with-lease`——它在远程有未知提交时会自己拒绝。`main` / `dev` 上不允许任何强推。
- 冲突由**分支作者**解决，不得整块丢弃他人改动；拿不准就逐处问，不要选"看起来更新的那份"。
- 判断"我的改动还在不在"，**不看行数增减，用关键串取证**：

  ```sh
  git grep -n "本节的独有结论短句" -- docs/
  ```

  命中 0 就等于被覆盖了（今晚实测：编辑报 success，几十秒后 grep 命中 0）。
- 动手前先留可回滚点：`git stash create`（不移动任何 ref），把 sha 记进当次报告。注意 `/server/data/`（数据库）和
  `server/analysis_out/`（自测脚本、`serve_3003.py`）都不在版本控制里，"每个 commit 就是一份备份"这句话**只对已跟踪代码成立**，别拿它当数据备份的理由。

## 4. 一次性启用本地拦截

```sh
git config --local core.hooksPath .githooks     # 启用（每台克隆一次）
git config --local --unset core.hooksPath      # 停用，回到改造前
```

会拦的四件事 + 一件提醒 + 推送非快进，逐条判据和独立开关见 [.githooks/README.md](.githooks/README.md)。
**禁止用 `git commit --no-verify` 绕过。**

## 5. GitHub 分支保护（只能网页开，本机没装 gh CLI）

Settings → Branches → Add branch protection rule，分支名 `main`（然后对 `dev` 重复一次）：

- [ ] Do not allow force pushes
- [ ] Do not allow deletions
- [ ] Require a pull request before merging（Required approving reviews = 1）
- [ ] Include administrators

开完把日期与结果写进 `docs/系统更新日志.md`。这一层命令行验证不了：hook 只能保证"这台机器推不出非快进"，
别人用别的克隆直连 API 仍能强推——所以**这两层都要有**，不是二选一。
