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

## 6. 日常改动的标准流程（提交 → 推送 → PR → 合并 → 清理）

> 2026-10-26 按实际走过的几次 PR 固化。命令都在仓库根目录执行；盘中 **09:10～15:10 只做第 ① 步**，切分支、合并、rebase 一律放到收盘后。

**① 改与自测**

- 改前先读文件；改后用本次独有的关键串 grep 回查，命中 0 视为被并行会话覆盖——停下如实报告，不整节重写回去。
- 自测脚本放 `server/analysis_out/`（该目录不入库）；失败要显式报错，不静默降级。
- 同步受影响文档（至少 README、工程知识库、系统更新日志、优化实施计划、选股策略取长补短）。
- 改完后端代码要生效：写 `server/data/backend_restart_requested` 让看护重启，不要 taskkill。

**② 建分支、逐路径暂存、提交**

```sh
git fetch origin main
git switch -c docs/<描述>          # 或 feat/ fix/
git add <逐个文件路径>              # 禁止 git add . / -A
git commit -m "docs(scope): 描述"
```

提交时 hook 会拦：落后 origin、疑似批量暂存、前端产物删增不成对、新增行含本机路径或凭据；文档没同步只提醒不阻断。被拦就按提示改对，禁止 `--no-verify`。

**③ 推送**

```sh
git push -u origin <分支>
```

**④ 网页开 PR（本机没装 gh CLI）**

打开 `https://github.com/yangzhen-yz/zzlt/pull/new/<分支>`，base 选 `main`；一个 PR 只做一件事，标题与提交信息同风格。
**标题与描述按第 7 节的模板由 Agent 推送后直接生成并贴回会话**，不等用户来问"这里写啥"。

**⑤ 合并后清理**

```sh
git fetch --prune origin
git log --oneline -1 origin/main     # 必须能看到 Merge pull request #N
git merge --ff-only origin/main
git branch -d <分支>                  # GitHub 通常已在合并时删掉远程分支；本地 -d 属正常删除
git remote prune origin               # 远程已删时清掉本地跟踪引用
git status -sb                        # 期望 ## main...origin/main，无脏文件
```

- GitHub 没自动删远程分支：`git push origin --delete <分支>`。
- 远程已删却报 `remote ref does not exist`：说明合并时已删，`git remote prune origin` 即可。
- 合并提交在 `origin/main` 上还看不到时**先别删分支**，fetch 成功再判断。

**⑥ dev 对齐**

```sh
git rev-list --count origin/dev..origin/main          # 落后几个提交
git merge-base --is-ancestor origin/dev origin/main   # 退出码 0 = 纯祖先
git push origin main:dev                              # 仅纯祖先时允许快进
```

`dev` 上有独有提交时禁止最后一条，必须走 feat/fix → dev → main 的 PR。

**⑦ 合并后校验**

- 纯文档：核对关键文件行数、新增关键串命中数；md 全 CRLF、`**` 粗体成对；数值区间用全角 ～ 或"至"。
- 动了后端：`/api/health` 200，并在日志里找到本次新增的启动行或字段，确认真的换了新进程。
- 失败了先查 `git status` 与 `git log origin/main`，别用 reset/force 掩盖问题。

**失败处理**

- `fetch` 不通：停在当前分支等网络，**不要切回 `main`**——工作树就是线上代码，切回旧提交等于把刚合入的内容撤掉。
- 本地分支还指着旧的 `origin/main`：先 `git fetch --prune`，确认 `git log --oneline -3 origin/main` 里有合并提交，再继续。

## 7. PR 标题与描述模板（**由 Agent 推送后自动生成，不要等人来问**）

网页表单只有两个空格：标题一行、描述一段。默认标题是 GitHub 从分支名硬翻的（`Feat/price display and polling fix` 这种），
看不出改了什么——所以**推送分支的同时就把标题与描述生成好贴回会话**，用户复制粘贴即可，不需要再问一次"这里写啥"。

### 7.1 标题

- 中文、一行、≤ 60 字，沿用提交信息风格：`feat|fix|perf|chore(scope): 具体做了什么`。
- 一个 PR 里有多个改动时，用最重要的那个动词开头，scope 写页面/模块，冒号后面把改动点列全：
  例 `feat(前端+后端): 显示现价、超额超时修复、只数影子标注、轮询兜底`。
- 不写"更新代码 / 若干修复 / 优化"这类零信息标题；分支名不算描述。

### 7.2 描述骨架（五段，缺一段就说明有事没做）

```markdown
## 这个 PR 做什么
<一句话定性 + 提交清单：`git log --oneline origin/<base>..<分支>`，每条一个短说明>

## 逐项说明
<每个改动 2～4 行：用户原话/现象 → 根因（带实测数字）→ 改法 → 顺带修掉的坑>

## 验证
<自测脚本名 + 通过数（如 `analysis_out/xxx_check.py` 14/14）+ 关键实测数字 +
 py_compile / eslint / vite build 结果 + 截图或日志判据>

## 文件构成
<N files = 源码/文档 X 个 + 构建产物 Y 个（server/static 删增成对、含 index.html）>

## 合并后还需注意
<哪些要重启才生效、判据是哪行日志/哪个字段；哪些仍是影子/未做；本 PR 明确不含什么>
```

### 7.3 硬性要求

- **数字必须实测**：耗时、样本数、通过项数、行数都从当次运行里取，不写"约""大概"；没测的就写"未验证"。
- **状态不许美化**：影子、默认关、待重启、只跑了几天的东西，描述里必须照实写，不得读成"已上线"。
- **文件构成要对账**：本仓产物入库，`51 files changed` 里绝大多数是 `server/static` 的哈希 chunk，
  主动写清"15 源码/文档 + 36 产物"，否则用户每次都要问"怎么改了这么多文件"。
  对账命令：`git diff --name-only origin/<base>..<分支> | grep -vc '^server/static/'`。
- **"不含什么"要写**：明确划界（例："本 PR 不含选股口径改动，没动评分/门槛"），
  避免合并后被误认为某个想法已经落地。
- 描述里不写本机绝对路径、不写凭据（与 B4 拦截同源，只是文档不会自动扫，靠自觉）。
- 生成后**贴回会话**给用户复制，同时给出 PR 新建链接；用户合并之前的清理动作（快进 `main`、删分支、对齐 `dev`）
  等用户回来说"合并了"再做，见第 6 节 ⑤⑥。
