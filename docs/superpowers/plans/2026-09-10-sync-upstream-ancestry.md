# 上游同步提交关系修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 schedule、workflow_dispatch 和发布链都机械验证选定上游提交的 ancestry，并以一次真实 merge 修复当前缺失的上游父关系。原始事件涉及 `64b65839`；Task 4 执行时的 immutable replacement target 已获批准为 `09ad58f0d58decc9d30799ca83374ff2604ef16b`。

**Architecture:** `.github/workflows/sync-upstream.yml` 将同步拆成只读 `prepare-candidate` 和新 runner 上的 `verify-and-open-pr` 两个 job。准备 job 只运行固定 `opencode-ai@1.18.30`，验证 immutable base/target 后只上传 Git bundle 与三 SHA manifest；trusted job 只执行 base checkout 中的 helper，验证对象、祖先关系、protected gate 文件、版本形状和 main tip，最后一步才用 PAT 将明确 SHA 推到唯一分支并创建 PR。`.opencode/command/sync-upstream.md` 仍是模型提交关系程序的唯一所有者。

Task 5A reviewer 修复后的 trusted contract 还保护整个 `.github/workflows/**`、`.github/pull.yml` 和
所有 gate/helper/command 文件；`.github/workflows/opencode.yml` 保持触发语义，固定到
`anomalyco/opencode/github@77fc88c8ade8e5a620ebbe1197f3a572d29ae91a`，只保留 checkout 所需
`contents: read` 与 action OIDC 所需的 `id-token: write`。

**Tech Stack:** Git、GitHub Actions YAML、POSIX shell、pinned OpenCode CLI、GitHub CLI、Python 3.12 + pinned PyYAML/Ruff（只由 trusted runner 用于 candidate data gates）。

## Global Constraints

- 所有 GitHub 操作仅指向 `elvisw/ppt-master`，不得修改 `hugohe3/ppt-master`。
- Task 4 当前修复目标固定为 `09ad58f0d58decc9d30799ca83374ff2604ef16b`；原始事件中的 `64b65839c7f8096a534c872c03d688a2b2491c8f` 仅作为历史证据保留。
- 从 merge 开始到 merge commit 创建完成，禁止 reset、rebase、squash、cherry-pick、切换分支和清除 `MERGE_HEAD`；失败只允许 `git merge --abort` 后停止。
- `.github/upstream-main.sha` 只含一行 40 位小写 SHA 和结尾换行。
- schedule 和 workflow_dispatch 共用同一个候选准备路径；workflow_dispatch 仅允许
  `GITHUB_REF == refs/heads/main`，guard 缺失或不匹配必须 fail-closed。
- identity step 只能配置 `github-actions[bot]` 的 name/email，不得设置 token、remote
  或 push 凭据；workflow_dispatch 的模型不得获得 `PUSH_PAT` 或执行 push。
- schedule/manual 两个 OpenCode step 都必须通过 env 注入
  `EXPECTED_UPSTREAM_SHA=${{ steps.upstream.outputs.upstream_sha }}`；Actions 中变量缺失
  或与 fetch 后 `upstream/main` 不匹配必须立即失败，只有本地运行允许 fallback fetch。
- 无变化时模型、artifact、trusted job、分支和 PR 全部正常跳过；有变化时两个触发器必须走同一
  bundle → trusted verification → PR 路径。
- Checkout decision: `actions/checkout` requires a token; an empty token fails and `github.token`
  would violate the model boundary, so both jobs use an uncredentialed canonical-URL full-history
  fetch (no `--depth`, equivalent to `fetch-depth: 0`) plus detached immutable checkout.
- Verify 必须先以 `git diff --quiet && git diff --cached --quiet`，再以
  `git status --porcelain=v1 --untracked-files=all` 拒绝任何脏工作树，再以
  `git show HEAD:.github/upstream-main.sha | tr -d '\r\n'` 读取已提交 marker，不得读取工作树 marker。
- Verify 必须确认 `BASE_SHA` 与 expected 都是 HEAD 祖先，并找到恰好双父 merge：
  第一父严格等于 `BASE_SHA`，第二父严格等于 `EXPECTED_UPSTREAM_SHA`。
- `.github/upstream-main.sha` 只有在
  `git merge --no-ff --no-commit "$EXPECTED_UPSTREAM_SHA"` 成功后才能写入/暂存；冲突
  必须 `git merge --abort` 后停止，不能残留 marker 修改；成功或任何 abort 路径都要清理
  `.git/ppt-master-sync/` 独占状态目录。若 ownership 无法证明，必须保留现场而不得猜测
  abort 或恢复用户文件。
- `.opencode/command/sync-upstream.md` 的各代码块不得依赖跨 shell 普通变量；target 与
  原始 HEAD、marker 存在状态和字节快照必须通过 `git rev-parse --git-path` 定位的独占
  `.git` 状态目录跨块传递，后续块每次重新读取。状态目录以原子 `mkdir` 加锁，已存在
  必须 fail-closed；Actions 还要复核 env target 与持久化 target 相等。
- abort 前必须同时证明当前 `HEAD` 与 persisted original HEAD 相等；`ORIG_HEAD` 存在时也
  必须相等；`MERGE_HEAD` 存在时必须恰好一项且严格等于 persisted target。MERGE_HEAD 缺失
  时只有 ownership 完整且 HEAD/ORIG_HEAD 条件成立才可安全恢复 marker，否则保留现场。
- marker 原本 tracked 时必须保存字节级快照，并用 original HEAD/快照验证 index+worktree
  恢复；原本 absent 时只允许对 `.github/upstream-main.sha` 单一路径执行
  `git rm --cached --ignore-unmatch` 和删除，并确认路径与 index 都 absent。
- trusted publication 必须使用 `verified_sha:refs/heads/opencode/sync-<run-id>-<attempt>`；main
  前进或 push 非 fast-forward 时停止，禁止 rewrite、force-push 或任何直接 main push。
- fork 仓库设置仅允许 merge commit；关闭 squash merge 和 rebase merge。
- ancestry 门禁失败必须阻断 `Check UVX Migration → auto-tag → publish-pypi`，不得以内容相同或模型声明替代。
- 本次修复执行在 merge commit 后动态 bump 两处 fork 包版本至下一个未占用 patch 版本；本地不执行远端 push/PR。
  运行时契约是两个触发器都由 trusted job 使用 PAT 创建 PR，模型永远不接收 PAT。
  设计/实现提交只落在本地 `fix/sync-upstream-ancestry`。
- Windows 下所有 Python 命令使用 `python`，不用 `python3`。
- 不在 `skills/ppt-master/scripts/tests/` 之外创建测试；workflow 文本用沙盘和真实 Git 命令验证。

---

## Task 5A Final Contract

Task 5A 的最终架构覆盖下方早期 Task 1–2 记录中的旧单 job 发布示例；早期条目保留作为历史决策轨迹，不能作为当前 workflow 的实现指令。

### Job 1: `prepare-candidate`

- 只声明 `permissions: contents: read`，不声明 `id-token`、write 权限或任何写凭据。
- 从 `github.sha` 以无 `--depth` 的完整历史 fetch（语义等价 `fetch-depth: 0`）做无凭据
  detached checkout，并验证 `HEAD == github.sha`；workflow_dispatch 不是 `refs/heads/main` 时立即失败。
- 明确 fetch canonical `upstream/main`；fetch 失败立即停止，不使用 stale remote ref。
- 无变化只写 `has_changes=false`，不运行模型、不上传 artifact、不启动 trusted job。
- 独立无 secret step 安装一次 `opencode-ai@1.18.30`；后续唯一 `opencode run` step 的环境只有 API key、模型名、immutable target 和普通 Actions 元数据；不得注入 `github.token`、`GITHUB_TOKEN`、`GH_TOKEN` 或 `PUSH_PAT`。
- 模型退出后由 base checkout 快照的 checker 做防御性验证，验证 clean worktree、marker blob、唯一严格双父 merge、版本 bump 和 protected gate policy。
- 只生成 `candidate.bundle` 与严格三字段 `manifest.json`；bundle 使用 `git bundle create <candidate-ref> <candidate-ref> ^<base-sha>`，不归档 worktree 或 `.git/config`。

### Job 2: `verify-and-open-pr`

- 仅在 `has_changes == true` 时运行，使用新 runner，并只声明 `contents: read` 与 artifact download 所需的 `actions: read`。
- 从 base SHA 做无凭据 trusted checkout；下载的 bundle 和 manifest 只作为不可信 Git object data，
  绝不 checkout、import、执行 candidate 文件、hook、workflow 或 action。
- 将 candidate 以 `git worktree add --no-checkout` + `read-tree` materialize 到隔离临时目录，
  只作为数据交给 base 版本 `check_sync_candidate.py`：lstat regular files，拒绝 symlink，
  AST/text/digest 检查 CLI `COMMANDS`/`ALIASES`、deps/locks、attribution、manifest/notices、
  YAML、8 个 fork marker/import 和 trusted uvx diff；candidate 同名 checker 永不执行。
- trusted tool step 固定安装 PyYAML/Ruff；随后只对 8 个明确 regular absolute Python 文件执行
  `PYTHONPATH= python -I -m py_compile` 与 `PYTHONPATH= ruff check --isolated --select F821`，
  全部 gate 在 PAT step 前完成。
- 由 base helper 严格拒绝额外/缺失 manifest key、非法或不匹配 SHA、缺失 base/target/candidate object、candidate tip mismatch、protected gate diff、非严格 parent order、marker symlink 和版本形状错误。
- 复核 canonical upstream 和 `origin/main == base_sha`；发布前立即重新读取 candidate ref，要求等于 authoritative `verified_sha`。
- 只有最后的 publication step 获得 `PUSH_PAT`。该 step 使用 `GIT_CONFIG_GLOBAL=/dev/null`、`GIT_CONFIG_SYSTEM=/dev/null`、`GIT_TERMINAL_PROMPT=0`、安全 askpass、每条 Git 命令的空 `credential.helper` 和 `core.hooksPath=/dev/null`，推明确 verified SHA 到唯一 sync branch，再用相同 PAT 创建 `elvisw/ppt-master` → `main` PR。
- `gh pr create` 后立即通过 REST fork API 的 `--jq '.base.sha'` 读取 PR base SHA 并要求等于 base；创建失败、push/create 间 main 前进或 base SHA mismatch 由 EXIT trap best-effort 关闭 PR并删除刚推分支后失败。API 检查不原子，最终由 trusted `pull_request_target` strict first-parent/required checks 阻断错误 PR。

### Artifact/output semantics decision

Job output 只传递不敏感的 immutable SHA 和固定 artifact name；artifact 本身只传 bundle 与 manifest。trusted job 不依赖跨 job 的普通 shell 变量，也不把 `HEAD` 当作候选身份。若 artifact service materializes unexpected files、manifest parse、bundle import 或 output/ref equality 任一不满足，流程 fail-closed；不将其解释为“无变化”，不降级到同 runner 注入 PAT。

### Bootstrap boundary

首次把本 workflow/helper 部署到 main 的 PR，其 base 可能没有当前 trusted gate，因此不能声称该 PR 已由新 gate 自身保护。Task 5A 依靠人工 diff 审查、独立 unit/YAML/CLI/deps/attribution 验证和真实临时 Git/artifact 沙盘；部署到 main 后，protected gate 文件只能通过显式 trusted maintenance/bootstrap 流程修改，普通同步 PR 必须 fail-closed。

---

### Task 1: 固定上游 SHA 与真实 merge 契约

**Files:**
- Create: `.github/upstream-main.sha`
- Modify: `.opencode/command/sync-upstream.md:23-46,245-286,303-353`
- Modify: `.github/workflows/sync-upstream.yml:35-123`
- Modify: `docs/zh/upstream-sync.md`
- Modify: `.superpowers/sdd/sync-upstream-task-1-report.md`

**Interfaces:**
- Produces: `steps.upstream.outputs.upstream_sha`，值为 fetch 后的 40 位 upstream tip SHA。
- Produces: `.github/upstream-main.sha`，供 Task 2 的 PR/main checks 消费。
- Produces during one command run: Git 内部临时状态
  `.git/ppt-master-sync/` 独占目录（原子 `mkdir` lock），其中保存
  `expected-upstream-sha`、`original-head`、`marker-original-state`、tracked marker 的
  `marker-snapshot` 和 `merge-started`；成功或安全失败终止时清理整个目录，后续独立
  shell 通过 `git rev-parse --git-path` 重新定位并读取。
- Contract: OpenCode 创建真实 merge commit；两个触发器都由 fresh trusted job 推唯一分支并创建 PR。
- Contract: workflow_dispatch 只能从 `refs/heads/main` 运行；无变化时模型、artifact、trusted job
  和 PR 全部跳过。
- Contract: Verify 只接受一个恰好双父的 merge commit，且父序严格为
  `[BASE_SHA, EXPECTED_UPSTREAM_SHA]`，同时两个 SHA 都必须是 HEAD 祖先。

- [x] **Step 1: 记录当前失败基线**

Run:

```powershell
git merge-base --is-ancestor 64b65839c7f8096a534c872c03d688a2b2491c8f 726c386b16519973f28809e2ae2d78f6f2a2c303
```

Expected: exit 1。随后运行：

```powershell
git show -s --format=%P 726c386b16519973f28809e2ae2d78f6f2a2c303
```

Expected: 只输出 `8aee5cd0cc7a51080e0cd5a05ef95eacfebfa8a0`。

- [x] **Step 2: 创建初始目标文件**

创建 `.github/upstream-main.sha`，完整内容：

```text
64b65839c7f8096a534c872c03d688a2b2491c8f
```

- [x] **Step 3: 改写 sync-upstream.md 的 Step 1/2**

Step 1 在 fetch 后通过 `git rev-parse --git-path ppt-master-sync` 取得状态目录，并用
原子 `mkdir -- "$SYNC_STATE_DIR"` 建立本次同步独占 lock；已存在必须 fail-closed。目录
内写入 `expected-upstream-sha`；Actions 必须使用并复核 env 注入值，本地仅在 env 为空时
从 fetched `upstream/main` fallback。target、original HEAD、marker 原始存在状态/字节
快照和 merge ownership 都不得依赖跨 shell 普通变量：

```bash
if ! SYNC_STATE_DIR=$(git rev-parse --git-path ppt-master-sync); then
  echo "Unable to resolve the Git-internal sync state directory" >&2
  exit 1
fi
if ! SYNC_EXPECTED_STATE=$(git rev-parse --git-path ppt-master-sync/expected-upstream-sha); then
  echo "Unable to resolve the Git-internal expected-target state path" >&2
  exit 1
fi
if ! SYNC_ORIGINAL_HEAD_STATE=$(git rev-parse --git-path ppt-master-sync/original-head); then
  echo "Unable to resolve the Git-internal original-HEAD state path" >&2
  exit 1
fi
if ! SYNC_MARKER_STATE=$(git rev-parse --git-path ppt-master-sync/marker-original-state); then
  echo "Unable to resolve the Git-internal marker state path" >&2
  exit 1
fi
if ! SYNC_MARKER_SNAPSHOT=$(git rev-parse --git-path ppt-master-sync/marker-snapshot); then
  echo "Unable to resolve the Git-internal marker snapshot path" >&2
  exit 1
fi
if ! SYNC_MERGE_STARTED_STATE=$(git rev-parse --git-path ppt-master-sync/merge-started); then
  echo "Unable to resolve the Git-internal merge ownership path" >&2
  exit 1
fi
if ! mkdir -- "$SYNC_STATE_DIR"; then
  echo "A ppt-master sync is already active; refusing to share its state" >&2
  exit 1
fi
if ! printf '%s\n' "$CANDIDATE_UPSTREAM_SHA" > "$SYNC_EXPECTED_STATE"; then
  echo "Unable to persist the expected upstream SHA in Git-internal state" >&2
  rm -rf -- "$SYNC_STATE_DIR"
  exit 1
fi
```

Step 2 是独立 shell，必须重新取得并读取同一个 state directory；先记录 original HEAD，
marker tracked 时保存字节快照，marker absent 但路径（包括 ignored/untracked）存在时拒绝
启动。每次 merge 前写入 `merge-started` ownership state。在唯一的
`git merge --no-ff --no-commit "$EXPECTED_UPSTREAM_SHA"` 成功后，先确认 `MERGE_HEAD`
恰好一项且等于 target，再读取本次 merge 设置的 `ORIG_HEAD`，确认它等于写入
`original-head` 的原始 HEAD，最后才写入/暂存目标文件。Already-up-to-
date（merge 返回 0 但没有 `MERGE_HEAD`）必须 fail-closed。冲突或任何失败必须只 abort
本次 `merge-started` 且 HEAD/ORIG_HEAD/MERGE_HEAD target ownership 成立的 merge，恢复
merge 前 marker、清理整个 `.git/ppt-master-sync/` 后停止；ownership 不成立时不能 abort
或覆盖用户在本次运行前已有的 merge/内容，必须保留现场。

- [x] **Step 4: 在 Step 4e 加入提交前 merge-state 门禁**

现有六项门禁扩为七项；第 7 项是独立 shell 代码块，必须重新用 `git rev-parse
--git-path ppt-master-sync` 取得本次独占状态目录及所有状态文件，并用显式 `if`/`exit`
验证 ownership、merge 状态与已暂存目标文件（不使用裸 `test`）。Step 2 的
`abort_owned_merge`、marker 恢复和 cleanup helper 必须逐字同样内联到本代码块：

```bash
if ! SYNC_STATE_DIR=$(git rev-parse --git-path ppt-master-sync); then
  echo "Unable to resolve the Git-internal sync state directory" >&2
  exit 1
fi
if ! SYNC_EXPECTED_STATE=$(git rev-parse --git-path ppt-master-sync/expected-upstream-sha); then
  echo "Unable to resolve the Git-internal expected-target state path" >&2
  exit 1
fi
if ! SYNC_ORIGINAL_HEAD_STATE=$(git rev-parse --git-path ppt-master-sync/original-head); then
  echo "Unable to resolve the Git-internal original-HEAD state path" >&2
  exit 1
fi
if ! SYNC_MARKER_STATE=$(git rev-parse --git-path ppt-master-sync/marker-original-state); then
  echo "Unable to resolve the Git-internal marker state path" >&2
  exit 1
fi
if ! SYNC_MARKER_SNAPSHOT=$(git rev-parse --git-path ppt-master-sync/marker-snapshot); then
  echo "Unable to resolve the Git-internal marker snapshot path" >&2
  exit 1
fi
if ! SYNC_MERGE_STARTED_STATE=$(git rev-parse --git-path ppt-master-sync/merge-started); then
  echo "Unable to resolve the Git-internal merge ownership path" >&2
  exit 1
fi
if ! MERGE_HEAD_PATH=$(git rev-parse --git-path MERGE_HEAD); then
  echo "Unable to resolve the Git merge-state path" >&2
  exit 1
fi
if [ ! -d "$SYNC_STATE_DIR" ] || [ ! -f "$SYNC_EXPECTED_STATE" ] ||
   [ ! -f "$SYNC_ORIGINAL_HEAD_STATE" ] || [ ! -f "$SYNC_MARKER_STATE" ] ||
   [ ! -f "$SYNC_MERGE_STARTED_STATE" ]; then
  echo "Sync ownership state is incomplete" >&2
  exit 1
fi
if ! EXPECTED_UPSTREAM_SHA=$(tr -d '\r\n' < "$SYNC_EXPECTED_STATE"); then
  echo "Unable to read the Git-internal expected upstream state" >&2
  exit 1
fi
if ! ORIGINAL_HEAD_SHA=$(tr -d '\r\n' < "$SYNC_ORIGINAL_HEAD_STATE"); then
  echo "Unable to read the Git-internal original-HEAD state" >&2
  exit 1
fi
if ! MERGE_HEAD_SHA=$(tr -d '\r\n' < "$MERGE_HEAD_PATH"); then
  echo "MERGE_HEAD is missing" >&2
  exit 1
fi
if [ "$MERGE_HEAD_SHA" != "$EXPECTED_UPSTREAM_SHA" ]; then
  echo "MERGE_HEAD does not equal EXPECTED_UPSTREAM_SHA" >&2
  exit 1
fi
if ! INDEX_TARGET_SHA=$(git show :".github/upstream-main.sha" | tr -d '\r\n'); then
  echo "Unable to read the staged upstream marker" >&2
  exit 1
fi
if [ "$INDEX_TARGET_SHA" != "$EXPECTED_UPSTREAM_SHA" ]; then
  echo "The staged upstream marker does not equal EXPECTED_UPSTREAM_SHA" >&2
  exit 1
fi
```

任一非零都停止，不提交；工作树 marker 也必须与索引值相同。该独立代码块必须具备与
Step 2 相同的 ownership-aware `abort_owned_merge` 和 cleanup 逻辑：abort 前同时证明
当前 HEAD/可用 ORIG_HEAD 为 persisted original，且 MERGE_HEAD 存在时严格等于 persisted
target；缺失 MERGE_HEAD 只能在 ownership 完整且 HEAD/ORIG_HEAD 条件成立时恢复 marker。
安全恢复后删除整个状态目录，ownership 不成立时保留现场。

- [x] **Step 5: 改写 Step 6 的提交与发布边界**

Step 6 是另一个独立 shell，不能读取 Step 2/4e 的普通变量；必须重新取得并读取
`ppt-master-sync/expected-upstream-sha`、`original-head`、`marker-original-state`、
`marker-snapshot` 和 `merge-started`。提交前若任一 gate 失败，统一按 ownership 证明
结果 abort/恢复 marker 或保留现场；安全路径清理整个状态目录，提交成功后也清理状态。
然后用显式 `if`/`exit` 验证恰好两个父，且 `^1` 为从内部状态读取的原始 HEAD、`^2` 为
expected：

```bash
if ! SYNC_STATE_DIR=$(git rev-parse --git-path ppt-master-sync); then
  echo "Unable to resolve the Git-internal sync state directory" >&2
  exit 1
fi
if ! SYNC_EXPECTED_STATE=$(git rev-parse --git-path ppt-master-sync/expected-upstream-sha); then
  echo "Unable to resolve the Git-internal expected-target state path" >&2
  exit 1
fi
if ! SYNC_ORIGINAL_HEAD_STATE=$(git rev-parse --git-path ppt-master-sync/original-head); then
  echo "Unable to resolve the Git-internal original-HEAD state path" >&2
  exit 1
fi
if ! SYNC_MARKER_STATE=$(git rev-parse --git-path ppt-master-sync/marker-original-state); then
  echo "Unable to resolve the Git-internal marker state path" >&2
  exit 1
fi
if ! SYNC_MARKER_SNAPSHOT=$(git rev-parse --git-path ppt-master-sync/marker-snapshot); then
  echo "Unable to resolve the Git-internal marker snapshot path" >&2
  exit 1
fi
if ! SYNC_MERGE_STARTED_STATE=$(git rev-parse --git-path ppt-master-sync/merge-started); then
  echo "Unable to resolve the Git-internal merge ownership path" >&2
  exit 1
fi
if ! SYNC_MERGE_HEAD_PATH=$(git rev-parse --git-path MERGE_HEAD); then
  echo "Unable to resolve the Git merge-state path" >&2
  exit 1
fi
if ! EXPECTED_UPSTREAM_SHA=$(tr -d '\r\n' < "$SYNC_EXPECTED_STATE"); then
  echo "Unable to read the Git-internal expected upstream state" >&2
  exit 1
fi
if ! ORIGINAL_HEAD_SHA=$(tr -d '\r\n' < "$SYNC_ORIGINAL_HEAD_STATE"); then
  echo "Unable to read the Git-internal original-HEAD state" >&2
  exit 1
fi
# Define the same abort_owned_merge/fail_before_commit helper here; the state
# directory is the exclusive lock, and abort requires current HEAD/ORIG_HEAD
# to equal original-head plus MERGE_HEAD to equal expected-upstream-sha. The
# helper restores marker-original-state (tracked from marker-snapshot, or
# absent through marker-only git rm/delete) and removes the whole state dir
# only after safe recovery.
if ! git add -u; then
  echo "Unable to stage tracked sync changes" >&2
  fail_before_commit 1
fi
if ! git add .github/upstream-main.sha cli.py skills/ppt-master/cli.py pyproject.toml skills/ppt-master/pyproject.toml; then
  echo "Unable to stage sync files" >&2
  fail_before_commit 1
fi
if ! git commit -m "merge upstream/main: resolve conflicts, adapt to uvx, sync cli.py mappings"; then
  echo "Unable to create the upstream merge commit" >&2
  fail_before_commit 1
fi
if ! SYNC_MERGE_COMMIT=$(git rev-parse HEAD); then
  echo "Unable to resolve the new merge commit" >&2
  fail_after_commit 1
fi
if ! MERGE_PARENTS=$(git show -s --format=%P "$SYNC_MERGE_COMMIT"); then
  echo "Unable to inspect merge commit parents" >&2
  fail_after_commit 1
fi
if ! PARENT_COUNT=$(printf '%s\n' "$MERGE_PARENTS" | awk '{ print NF }'); then
  echo "Unable to count merge commit parents" >&2
  fail_after_commit 1
fi
if [ "$PARENT_COUNT" -ne 2 ]; then
  echo "The sync commit must have exactly two parents" >&2
  fail_after_commit 1
fi
if ! FIRST_PARENT=$(git rev-parse "$SYNC_MERGE_COMMIT^1"); then
  echo "Unable to resolve the first merge parent" >&2
  fail_after_commit 1
fi
if ! SECOND_PARENT=$(git rev-parse "$SYNC_MERGE_COMMIT^2"); then
  echo "Unable to resolve the second merge parent" >&2
  fail_after_commit 1
fi
if [ "$FIRST_PARENT" != "$ORIGINAL_HEAD_SHA" ]; then
  echo "The first parent is not the saved original HEAD" >&2
  fail_after_commit 1
fi
if [ "$SECOND_PARENT" != "$EXPECTED_UPSTREAM_SHA" ]; then
  echo "The second parent is not EXPECTED_UPSTREAM_SHA" >&2
  fail_after_commit 1
fi
if ! git merge-base --is-ancestor "$EXPECTED_UPSTREAM_SHA" "$SYNC_MERGE_COMMIT"; then
  echo "Expected upstream SHA is not an ancestor of the sync commit" >&2
  fail_after_commit 1
fi
if ! cleanup_sync_state; then
  echo "Unable to clean Git-internal sync state after success" >&2
  exit 1
fi
```

版本提交保持独立；完成后再次用显式 `if`/`exit` 运行：

```bash
if ! EXPECTED_UPSTREAM_SHA=$(git show HEAD:.github/upstream-main.sha | tr -d '\r\n'); then
  echo "Unable to read the committed upstream marker" >&2
  exit 1
fi
if ! git merge-base --is-ancestor "$EXPECTED_UPSTREAM_SHA" HEAD; then
  echo "Expected upstream SHA is not an ancestor of HEAD" >&2
  exit 1
fi
```

GitHub Actions 的两个 OpenCode 路径都写成“不由模型 push”：schedule 由 Action 基础设施
创建分支和 PR；workflow_dispatch 由 workflow 在 Verify 成功后注入 PAT 并 push。
本地路径保持现有显式 push/tag 指令。

- [x] **Step 6: 修改 workflow 的目标输出和两个 prompt**

`Check upstream changes` 在 `git fetch` 后新增：

```bash
if ! UPSTREAM_SHA=$(git rev-parse upstream/main); then
  echo "Unable to resolve upstream/main" >&2
  exit 1
fi
echo "upstream_sha=$UPSTREAM_SHA" >> "$GITHUB_OUTPUT"
```

在两个 OpenCode step 的 `env` 中都必须注入以下值，不得只写在 prompt：

```yaml
EXPECTED_UPSTREAM_SHA: ${{ steps.upstream.outputs.upstream_sha }}
```

另加一个只配置身份的 step（不得含 token、remote 或 push）：

```yaml
      - name: Configure git identity
        if: steps.upstream.outputs.has_changes == 'true'
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
```

schedule prompt 与 manual prompt 都传达以下同一事实，不复制完整程序：

```text
The immutable upstream commit for this run is `${{ steps.upstream.outputs.upstream_sha }}`.
Set `EXPECTED_UPSTREAM_SHA` to that exact value and follow `.opencode/command/sync-upstream.md`.
CRITICAL: preserve a real merge commit whose second parent is that SHA. Never reset, rebase,
squash, cherry-pick, switch branches, or clear MERGE_HEAD after merge starts. Do not report
success unless the recorded SHA is an ancestor of HEAD.
```

manual prompt 再明确 `Do not push; the workflow verifies and pushes after OpenCode exits.`；
移除 manual OpenCode step 的 `GH_TOKEN`，删除模型运行前的 `Configure git for push`。manual
CLI 的模型必须先经 env 注入，再使用带 shell quote 的 `-m "$OPENCODE_MODEL"`。

manual OpenCode 前必须新增 fail-closed ref guard：

```yaml
      - name: Guard manual sync ref
        if: github.event_name == 'workflow_dispatch'
        run: |
          if [ "${GITHUB_REF:-}" != "refs/heads/main" ]; then
            echo "::error::workflow_dispatch sync must run from refs/heads/main"
            exit 1
          fi
```

- [x] **Step 7: 增加 manual verify 与 push steps**

OpenCode manual step 后新增：

```yaml
      - name: Verify upstream ancestry
        if: github.event_name == 'workflow_dispatch' && steps.upstream.outputs.has_changes == 'true'
        env:
          BASE_SHA: ${{ github.sha }}
          EXPECTED_UPSTREAM_SHA: ${{ steps.upstream.outputs.upstream_sha }}
        run: |
          if ! (git diff --quiet && git diff --cached --quiet); then
            echo "::error::OpenCode left the worktree or index dirty"
            exit 1
          fi
          if ! WORKTREE_STATUS=$(git status --porcelain=v1 --untracked-files=all); then
            echo "::error::Unable to inspect the complete worktree status"
            exit 1
          fi
          if [ -n "$WORKTREE_STATUS" ]; then
            echo "::error::OpenCode left tracked or untracked worktree changes"
            printf '%s\n' "$WORKTREE_STATUS"
            exit 1
          fi
          if [ -z "${BASE_SHA:-}" ] || [ -z "${EXPECTED_UPSTREAM_SHA:-}" ]; then
            echo "::error::BASE_SHA and EXPECTED_UPSTREAM_SHA are required"
            exit 1
          fi
          if ! git cat-file -e "HEAD:.github/upstream-main.sha"; then
            echo "::error::HEAD does not contain .github/upstream-main.sha"
            exit 1
          fi
          if ! RECORDED_SHA=$(git show HEAD:.github/upstream-main.sha | tr -d '\r\n'); then
            echo "::error::Unable to read committed upstream marker"
            exit 1
          fi
          if [ "$RECORDED_SHA" != "$EXPECTED_UPSTREAM_SHA" ]; then
            echo "::error::Recorded SHA $RECORDED_SHA does not match expected $EXPECTED_UPSTREAM_SHA"
            exit 1
          fi
          if ! git merge-base --is-ancestor "$BASE_SHA" HEAD; then
            echo "::error::BASE_SHA is not an ancestor of HEAD"
            exit 1
          fi
          if ! git merge-base --is-ancestor "$EXPECTED_UPSTREAM_SHA" HEAD; then
            echo "::error::Expected upstream SHA is not an ancestor of HEAD"
            exit 1
          fi
          if ! MERGE_LINES=$(git rev-list --merges --parents "$BASE_SHA..HEAD"); then
            echo "::error::Unable to enumerate merge commits after BASE_SHA"
            exit 1
          fi
          if ! MATCHES=$(printf '%s\n' "$MERGE_LINES" |
            awk -v base="$BASE_SHA" -v expected="$EXPECTED_UPSTREAM_SHA" 'NF == 3 && $2 == base && $3 == expected { print $1 }'); then
            echo "::error::Unable to inspect merge commit parents"
            exit 1
          fi
          if ! MATCH_COUNT=$(printf '%s\n' "$MATCHES" | awk 'NF { count += 1 } END { print count + 0 }'); then
            echo "::error::Unable to count matching merge commits"
            exit 1
          fi
          if [ "$MATCH_COUNT" -ne 1 ]; then
            echo "::error::Expected exactly one two-parent merge with ^1=$BASE_SHA and ^2=$EXPECTED_UPSTREAM_SHA"
            git log --graph --oneline --decorate "$BASE_SHA..HEAD"
            exit 1
          fi
          echo "Verified two-parent merge commit $MATCHES with ^1=$BASE_SHA and ^2=$EXPECTED_UPSTREAM_SHA"

      - name: Publish verified candidate
        # Task 5A replaces the historical single-job manual publication sample:
        # only the fresh trusted runner receives PUSH_PAT, and it pushes the
        # authoritative verified SHA to a unique branch before creating a PR.
```

Task 5A 的 workflow 不再拆分 schedule/manual 的模型、verify、push step；两者共用同一个
`has_changes` 条件、bundle manifest 和 fresh trusted publication step。目标必须从 immutable
output 与 manifest 绑定；工作树和 index 必须干净；BASE/目标必须是 HEAD 祖先；candidate range
中必须存在且仅存在一个恰好双父、直接父序为 `^1=BASE_SHA`、`^2=EXPECTED_UPSTREAM_SHA` 的
merge commit。PAT 只在 trusted runner 的最后一个 step 注入。

- [x] **Step 8: 静态验证并提交**

Run:

```powershell
python -c "import pathlib,yaml; data=yaml.safe_load(pathlib.Path('.github/workflows/sync-upstream.yml').read_text(encoding='utf-8')); assert data['jobs']['sync-upstream']['steps']; print('sync-upstream.yml: OK')"
python -m unittest discover -s skills/ppt-master/scripts/tests -p "test_sync_upstream_ownership.py" -v
git diff --check
```

Expected: `sync-upstream.yml: OK`，契约回归测试通过，`git diff --check` 无错误；真实
Git 行为沙盘还必须覆盖：跨独立 shell 重读 target/original HEAD、already-up-to-date、
foreign target merge 不 abort、foreign original HEAD 不恢复、MERGE_HEAD 缺失时的安全
marker 恢复、marker 写入失败、git add 失败、ignored/untracked pre-existing marker 拒绝、
tracked marker 字节恢复、absent marker 恢复为 absent、状态目录 lock 并发拒绝和成功双父。
每个安全失败都要验证无 `MERGE_HEAD`、marker/index 恢复、整个状态目录清理；ownership 不
成立的 foreign 场景要验证现场和状态目录保留。

Commit:

```powershell
git add .opencode/command/sync-upstream.md .github/workflows/sync-upstream.yml docs/superpowers/plans/2026-09-10-sync-upstream-ancestry.md
git commit -m "fix(ci): preserve merge abort ownership"
```

### Task 1 扩展：fail-closed workflow 与 merge 验证（本轮修订）

- [x] 在 manual OpenCode 前加入 `GITHUB_REF == refs/heads/main` guard；identity step
  仅配置 `github-actions[bot]` name/email，不含 token 或 remote。
- [x] schedule/manual OpenCode 均通过 env 注入
  `EXPECTED_UPSTREAM_SHA=${{ steps.upstream.outputs.upstream_sha }}`；Actions 缺失或
  不匹配立即失败，本地仅在 Step 1 fallback 后持久化 target，后续代码块从 `.git` 内部
  状态重新读取。
- [x] manual OpenCode、Verify、Push 均使用
  `workflow_dispatch && steps.upstream.outputs.has_changes == 'true'`，no-change 正常跳过；
  manual CLI 使用 `OPENCODE_MODEL` 和 `-m "$OPENCODE_MODEL"`。
- [x] Verify 先检查 `git diff --quiet && git diff --cached --quiet`，再检查
  `git status --porcelain=v1 --untracked-files=all`，最后读取
  `git show HEAD:.github/upstream-main.sha | tr -d '\r\n'`；同时验证 BASE/expected
  ancestry 和唯一恰好双父 merge 的父序 `[BASE_SHA, EXPECTED_UPSTREAM_SHA]`。
- [x] merge 成功后先验证 `MERGE_HEAD` 恰好一项且等于固定 target，再读取
  `ORIG_HEAD` 作为原始第一父；marker 只在此后写入/暂存。already-up-to-date、冲突、
  marker 写入失败、git add 失败和提交前 gate 失败都统一按 ownership 结果处理：安全拥有
  时 abort/恢复 marker/清理整个 state dir，foreign 场景不 abort、不恢复并保留现场；提交
  后验证 `HEAD^1`/`HEAD^2`，仅成功路径清理状态，post-commit 校验失败保留 ownership 现场。
- [x] 追加独立 shell、state lock、already-up-to-date、foreign target/original、缺失
  MERGE_HEAD 安全恢复、tracked/absent marker、ignored marker、marker/add failure、
  untracked 和成功双父行为沙盘；记录到
  `.superpowers/sdd/sync-upstream-task-1-report.md`。临时测试验证后删除，不纳入提交。

### Task 1 复审 Minor 收口

- [x] Actions 环境复用 workflow 已 fetch 的 `upstream/main`，仅本地执行 `git fetch upstream`，并保留 `EXPECTED_UPSTREAM_SHA` 与 fetched tip 的相等校验。
- [x] Step 1、Step 2、Step 4e、Step 6 统一使用目录不存在即失败的 `cleanup_sync_state` 实现。
- [x] `fail_after_commit` 在 post-commit 校验失败时保留 ownership 状态目录，仅成功路径清理。
- [x] foreign `ORIG_HEAD` 沙盘同时覆盖 Step 4e 与 Step 6：拒绝 abort 并保留 `MERGE_HEAD`、marker、HEAD 与状态目录。

---

### Task 2: PR 与 main 的确定性 ancestry 门禁

**Files:**
- Create: `.github/workflows/check-upstream-ancestry.yml`
- Modify: `.github/workflows/check-uvx-migration.yml:10-25`
- Create: `.github/scripts/check_upstream_ancestry.py`
- Create: `skills/ppt-master/scripts/tests/test_check_upstream_ancestry.py`
- Modify: `.github/workflows/auto-tag.yml`
- Modify: `.github/workflows/publish-pypi.yml`

**Interfaces:**
- Consumes: `.github/upstream-main.sha` from Task 1。
- Produces: PR check `Check Upstream Ancestry / check`。
- Produces: main gate `Verify recorded upstream ancestry`，位于现有 uvx migration check 之前。

- [x] **Step 1: 写入 PR check workflow**

`.github/workflows/check-upstream-ancestry.yml` 完整内容：

```yaml
name: Check Upstream Ancestry

on:
  pull_request:

permissions:
  contents: read

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.head.sha }}
          fetch-depth: 0

      - name: Fetch upstream
        run: |
          git remote add upstream https://github.com/hugohe3/ppt-master.git
          git fetch --no-tags upstream main

      - name: Verify changed upstream target
        env:
          BASE_SHA: ${{ github.event.pull_request.base.sha }}
          HEAD_SHA: ${{ github.event.pull_request.head.sha }}
        run: |
          TARGET_FILE=.github/upstream-main.sha
          if [ ! -f "$TARGET_FILE" ]; then
            echo "::error::Missing $TARGET_FILE"
            exit 1
          fi
          TARGET=$(tr -d '\r\n' < "$TARGET_FILE")
          BASE_TARGET=$(git show "$BASE_SHA:$TARGET_FILE" 2>/dev/null | tr -d '\r\n' || true)
          if [ "$TARGET" = "$BASE_TARGET" ]; then
            echo "Recorded upstream SHA unchanged; this is not an upstream sync PR."
            exit 0
          fi
          if ! printf '%s\n' "$TARGET" | grep -Eq '^[0-9a-f]{40}$'; then
            echo "::error::Invalid recorded upstream SHA: $TARGET"
            exit 1
          fi
          if ! git merge-base --is-ancestor "$TARGET" upstream/main; then
            echo "::error::Recorded SHA is not in upstream/main history: $TARGET"
            exit 1
          fi
          if ! git merge-base --is-ancestor "$TARGET" "$HEAD_SHA"; then
            echo "::error::Recorded upstream SHA is not an ancestor of PR head"
            git log --graph --oneline --decorate "$BASE_SHA..$HEAD_SHA"
            exit 1
          fi
          if ! MERGE_LINES=$(git rev-list --merges --parents "$BASE_SHA..$HEAD_SHA"); then
            echo "::error::Unable to enumerate merge commits after PR base"
            git log --graph --oneline --decorate "$BASE_SHA..$HEAD_SHA"
            exit 1
          fi
          if ! MATCHES=$(printf '%s\n' "$MERGE_LINES" |
            awk -v base="$BASE_SHA" -v target="$TARGET" 'NF == 3 && $2 == base && $3 == target { print $1 }'); then
            echo "::error::Unable to inspect merge commit parents"
            git log --graph --oneline --decorate "$BASE_SHA..$HEAD_SHA"
            exit 1
          fi
          if ! MATCH_COUNT=$(printf '%s\n' "$MATCHES" | awk 'NF { count += 1 } END { print count + 0 }'); then
            echo "::error::Unable to count matching merge commits"
            git log --graph --oneline --decorate "$BASE_SHA..$HEAD_SHA"
            exit 1
          fi
          if [ "$MATCH_COUNT" -ne 1 ]; then
            echo "::error::Expected exactly one two-parent merge with ^1=$BASE_SHA and ^2=$TARGET"
            git log --graph --oneline --decorate "$BASE_SHA..$HEAD_SHA"
            exit 1
          fi
          echo "Verified two-parent merge commit $MATCHES with ^1=$BASE_SHA and ^2=$TARGET"
```

该 workflow 不带 `paths` filter，也不依赖同步分支命名；普通 PR 在目标文件相对 base 未变化时直接 skip。

- [x] **Step 2: 在 main 发布入口增加记录目标门禁**

`check-uvx-migration.yml` checkout 后、现有 Python check 前新增：

```yaml
      - name: Fetch upstream
        run: |
          git remote add upstream https://github.com/hugohe3/ppt-master.git
          git fetch --no-tags upstream main

      - name: Verify recorded upstream ancestry
        run: |
          TARGET_FILE=.github/upstream-main.sha
          if [ ! -f "$TARGET_FILE" ]; then
            echo "::error::Missing $TARGET_FILE"
            exit 1
          fi
          TARGET=$(tr -d '\r\n' < "$TARGET_FILE")
          if ! printf '%s\n' "$TARGET" | grep -Eq '^[0-9a-f]{40}$'; then
            echo "::error::Invalid recorded upstream SHA: $TARGET"
            exit 1
          fi
          if ! git merge-base --is-ancestor "$TARGET" upstream/main; then
            echo "::error::Recorded SHA is not in upstream/main history: $TARGET"
            exit 1
          fi
          if ! git merge-base --is-ancestor "$TARGET" HEAD; then
            echo "::error::Recorded upstream SHA is not an ancestor of main HEAD"
            git log --graph --oneline --decorate --max-count=30 HEAD upstream/main
            exit 1
          fi
          echo "Verified recorded upstream ancestor $TARGET"
```

不得改变现有 `check_uvx_migration.py` 的 exit 0/1/2 处理。

- [x] **Step 3: YAML 与结构验证**

Run:

```powershell
python -c "import pathlib,yaml; [yaml.safe_load(pathlib.Path(p).read_text(encoding='utf-8')) for p in ['.github/workflows/check-upstream-ancestry.yml','.github/workflows/check-uvx-migration.yml']]; print('ancestry workflows: OK')"
git diff --check
```

Expected: `ancestry workflows: OK`，无 diff whitespace 错误。

- [x] **Step 4: 沙盘验证 PR check 核心逻辑**

在 `C:\Users\elvis\AppData\Local\Temp\opencode` 创建临时 bare/clone 沙盘，构造：

- 目标文件不变的普通提交：check 成功并输出 skip。
- 目标文件更新 + `git merge --no-ff --no-commit` + commit：check 成功并找到 merge commit。
- 目标文件更新 + 普通单父 commit：check exit 1。
- 目标位于第一父或多父 merge：check exit 1（严格父序与恰好双父）。
- 非 40 位或不属于 upstream 历史的目标：check exit 1。

每种情况记录命令、exit code、parents 和输出；不得只做 YAML 文本匹配。

- [x] **Step 5: 提交**

```powershell
git add .github/workflows/check-upstream-ancestry.yml .github/workflows/check-uvx-migration.yml docs/superpowers/plans/2026-09-10-sync-upstream-ancestry.md
git commit -m "ci: gate upstream ancestry before publish"
```

### Task 2 reviewer repair：trusted ancestry 与不可变发布身份

**Exact contract:**

- PR gate 使用 `pull_request_target`：只将 base SHA checkout 到 `trusted/`，使用
  `persist-credentials: false`；先校验 PR number/base/head，再从 base repo 的
  `refs/pull/<number>/head` 精确 fetch PR head object 到同一 trusted object store，
  并机械确认 `FETCH_HEAD == event HEAD_SHA`。不 checkout candidate、不执行任何
  candidate 内容；所有 shell、helper 和解析逻辑从 trusted base 执行，helper 使用
  `--repo trusted`。base/head 必须显式非空、40 位小写 hex，并逐一等于事件对象。
- trusted helper 的 PR 模式先机械拒绝 base..head 修改五个 protected gate 文件：
  `.github/scripts/check_upstream_ancestry.py`、
  `.github/workflows/check-upstream-ancestry.yml`、
  `.github/workflows/check-uvx-migration.yml`、`.github/workflows/auto-tag.yml`、
  `.github/workflows/publish-pypi.yml`。本次首次部署的 base 尚无 trusted workflow
  是 bootstrap 缺口，必须由人工 review + main gate 证明；部署后只能通过受信任维护
  流程修改，普通 PR 不得静默自修改。
- helper 的 `--base-sha` 必须区分 omitted 与 explicit empty；explicit empty fail，
  不得降级到单 commit 模式。marker entry 必须是普通 `100644 blob`，内容严格为
  40 位小写 hex 加一个 LF；无 LF、CRLF、多行和空内容均 fail 且不回显原文。
  PR 状态保持 absent/absent skip、deleted fail、added/changed 完整验证、相同普通
  blob skip；changed 仍要求 upstream history、head ancestry 和唯一严格双父 merge。
- `.github/scripts/check_upstream_ancestry.py` 是唯一 ancestry owner，支持 `--repo`
  供 trusted script 检查同一 trusted object store；不加入用户 CLI。
- auto-tag 在 tag 前最后一步再次 fetch `origin/main`，确认 `HEAD == origin/main`，
  再将 tag 指向该 HEAD。发布 owner 选择 **tag-push**：使用现有 `PUSH_PAT` 让 tag
  push 触发 publish；移除 auto-tag 的显式 publish dispatch，避免 tag push 与 dispatch
  双发布。publish 的 workflow_dispatch 保留为显式人工恢复入口，但必须提供并校验
  `release_sha`/`release_tag`，从 tag ref checkout 同一不可变身份。
- publish 的 tag push/manual 两入口都校验 release SHA/tag、remote tag 恰好指向 SHA、
  HEAD 等于 SHA、tag 与 pyproject 版本一致，然后运行 ancestry gate 和 migration
  check，最后才 build、attribution guard、publish；tag 后 main 前进仍构建 tag SHA，
  tag 前 main 前进则 auto-tag fail-closed。所有 checkout 使用 `persist-credentials:
  false`，main gate 显式 `contents: read`。

**Files:**

- [x] `.github/workflows/check-upstream-ancestry.yml`：trusted-only checkout、PR head
  object fetch/identity、trusted helper 与 protected diff gate。
- [x] `.github/workflows/check-uvx-migration.yml`：object-only main gate、权限与 checkout
  hardening。
- [x] `.github/workflows/auto-tag.yml`：immutable tag 前复核、PUSH_PAT tag owner、移除
  explicit publish dispatch。
- [x] `.github/workflows/publish-pypi.yml`：required release inputs、tag/manual identity
  binding、ancestry/migration-before-build gate。
- [x] `.github/scripts/check_upstream_ancestry.py`：`--repo`、protected diff、严格 marker
  LF 与 explicit empty base 防降级。
- [x] `skills/ppt-master/scripts/tests/test_check_upstream_ancestry.py`：trusted object
  store fetch、candidate refs 缺 base、protected tamper、空 base、严格 marker 内容回归。
- [x] `docs/superpowers/specs/2026-09-10-sync-upstream-ancestry-design.md`：trusted
  bootstrap 边界与维护流程。
- [x] `.superpowers/sdd/sync-upstream-task-2-report.md`：追加本轮测试、owner 决策与残余
  bootstrap 风险（gitignored）。

**Verification:**

- [x] 四个 workflow YAML parse、`git diff --check`、trusted-only workflow 不执行
  candidate 内容的静态审计。
- [x] helper/ownership unit tests：candidate helper/workflow 篡改 fail、trusted base
  helper 实际执行、empty `--base-sha` fail、无 LF/CRLF/多行 fail、正确 LF pass。
- [x] 真实 Git 沙盘：candidate refs 缺 base 但 head fetch 到 trusted 后通过、FETCH_HEAD
  与 event HEAD_SHA 不一致 fail、tag 前 main 前进 fail、tag 后 main 前进仍发布 tag SHA、
  tag/SHA mismatch fail、重复自动发布入口不可达。
- [x] 新提交 `fix(ci): verify fork prs in trusted object store`；不 amend、不 push、
  不建 PR、不做 Task 3/4、不 bump 版本、不改 progress.md。

---

### Task 3: Fork 合并方式设置

**Files:**
- Modify external repository settings only: `elvisw/ppt-master`

**Interfaces:**
- Consumes: user approval to change fork merge settings。
- Produces: `allow_merge_commit=true`、`allow_squash_merge=false`、`allow_rebase_merge=false`。

- [ ] **Step 1: 读取并保存修改前设置**

```powershell
gh api repos/elvisw/ppt-master --jq '{allow_merge_commit,allow_squash_merge,allow_rebase_merge}'
```

Expected current evidence: 三项均为 `true`。

- [ ] **Step 2: 修改 fork 设置**

```powershell
gh api -X PATCH repos/elvisw/ppt-master -F allow_merge_commit=true -F allow_squash_merge=false -F allow_rebase_merge=false --jq '{allow_merge_commit,allow_squash_merge,allow_rebase_merge}'
```

Expected: `true/false/false`。命令中 repo 必须是 `elvisw/ppt-master`。

- [ ] **Step 3: 独立复读验证**

```powershell
gh api repos/elvisw/ppt-master --jq '{allow_merge_commit,allow_squash_merge,allow_rebase_merge}'
```

Expected: `true/false/false`。

---

### Task 4: 当前 ancestry repair merge 与完整验证

**Files:**
- Git history only: local branch `fix/sync-upstream-ancestry`
- Verify: all files from Tasks 1-2 and existing fork gates

**Interfaces:**
- Consumes: implementation branch HEAD、upstream target `09ad58f0d58decc9d30799ca83374ff2604ef16b`。
- Produces: a real merge commit with parents `[pre-merge implementation HEAD, 09ad58f0d58decc9d30799ca83374ff2604ef16b]`，以及其后的独立动态版本 bump commit。

- [ ] **Step 1: 提交前仓库和远端确认**

Run separately and inspect full output:

```powershell
git status --short
git log --oneline -10
git fetch --all --prune
git rev-parse upstream/main
```

Expected: worktree clean；upstream/main 为目标 SHA；无未知改动。

- [ ] **Step 2: 开始真实 repair merge**

在 `git rev-parse --git-path ppt-master-sync/original-head` 返回的 Git 内部临时路径记录
merge 前 HEAD；然后执行：

```powershell
git merge --no-ff --no-commit 09ad58f0d58decc9d30799ca83374ff2604ef16b
```

Expected: merge 成功并保留 `MERGE_HEAD=09ad58f0...`。若冲突，逐文件保留 fork uvx
适配并合入上游功能；无法证明正确则 `git merge --abort` 并停止。

- [ ] **Step 3: merge-state 与树审查**

```powershell
git rev-parse MERGE_HEAD
git status --short
git diff --cached --check
git diff --cached --stat
```

Expected: `MERGE_HEAD` 等于目标；无无关文件和 whitespace 错误。即使文件树无变化，
仍保留 merge state 并继续创建 ancestry commit。

- [ ] **Step 4: 创建 repair merge commit**

```powershell
git commit -m "merge upstream/main: resolve conflicts, adapt to uvx, sync cli.py mappings"
```

禁止 reset/rebase/squash/cherry-pick。记录新 commit SHA。

- [ ] **Step 5: 正反 ancestry 和父节点验证**

```powershell
git merge-base --is-ancestor 09ad58f0d58decc9d30799ca83374ff2604ef16b HEAD
git log HEAD..09ad58f0d58decc9d30799ca83374ff2604ef16b --oneline
git show -s --format=%P HEAD
git log --graph --oneline --decorate --max-count=15
```

Expected: 第一条 exit 0；第二条无输出；parents 第一项严格等于 Git 内部临时状态中的
merge 前 HEAD、第二项为 `09ad58f0...`；提交图显示真实双父 merge。随后两个
`pyproject.toml` 版本和必要的 `uv.lock` 变更单独提交，提交消息为
`chore: bump version to <computed-version>`。

- [ ] **Step 6: 运行 workflow 与 fork 门禁**

Run from repository root:

```powershell
python -c "import pathlib,yaml; [yaml.safe_load(pathlib.Path(p).read_text(encoding='utf-8')) for p in ['.github/workflows/sync-upstream.yml','.github/workflows/check-upstream-ancestry.yml','.github/workflows/check-uvx-migration.yml']]; print('workflow YAML: OK')"
python skills/ppt-master/scripts/check_cli_sync.py
python skills/ppt-master/scripts/attribution_guard.py
python skills/ppt-master/scripts/check_deps_sync.py
python -m py_compile skills/ppt-master/scripts/confirm_ui/server.py skills/ppt-master/scripts/svg_editor/server.py skills/ppt-master/scripts/visual_review.py skills/ppt-master/scripts/server_common.py skills/ppt-master/scripts/config.py skills/ppt-master/scripts/project_management/paths.py skills/ppt-master/scripts/project_manager.py skills/ppt-master/scripts/register_template.py
uvx ruff check --select F821 skills/ppt-master/scripts/confirm_ui/server.py skills/ppt-master/scripts/svg_editor/server.py skills/ppt-master/scripts/visual_review.py skills/ppt-master/scripts/server_common.py skills/ppt-master/scripts/config.py skills/ppt-master/scripts/project_management/paths.py skills/ppt-master/scripts/project_manager.py skills/ppt-master/scripts/register_template.py
```

Expected: YAML OK；所有脚本 exit 0；attribution guard 无输出；Ruff 输出
`All checks passed!`。

- [ ] **Step 7: 最终边界审查**

```powershell
git status --short
git diff origin/main...HEAD --check
git diff --stat origin/main...HEAD
git log --oneline origin/main..HEAD
gh api repos/elvisw/ppt-master --jq '{allow_merge_commit,allow_squash_merge,allow_rebase_merge}'
```

Expected: worktree clean；仅计划内文件；本地提交含两份设计提交、实现提交、CI 提交和
repair merge；GitHub 设置为 `true/false/false`；没有 push 或远端 PR。

---

## Task 5B：不可变 release gate 与发布隔离（实现记录）

Task 5B 在 Task 5A 的 trusted ancestry 边界之后增加一条只读 release chain；它不改写
Task 4 ancestry/history、版本或 `progress.md`，也不执行远端 tag/push/PR/publish。

### 实现合同

- [x] `.github/scripts/check_release_gates.py` 成为 auto-tag 与 publish 共用的唯一 release
  gate owner；workflow-run API evidence 在所有本地 gate 前 fail-closed，`check_uvx_migration.py`
  exit 2 只输出 notice。
- [x] 版本通过 `tomllib` 从两份 pyproject 读取，严格 canonical grammar 绑定 `v<version>`；
  release SHA、exact tag ref、origin/main tip/history 和 upstream marker 都按 object/ref 验证。
- [x] `check_cli_sync.py` 比较 root/Skill 每个 command/alias 的完整映射值，规范化路径并
  保持 aliases 位于 `COMMANDS` 之外；新增 `check-uvx-repository` 为用户可调用的只读扫描器。
- [x] scanner 只从 `git ls-files -z` 读取 tracked 文档/workflow/prompt，覆盖 root、docs、
  examples、`.github`、`.opencode`、skills；allowlist 为精确规范化路径+规则，`auto_fix_uvx.py`
  只指向 scanner-owned policy，不再维护 content-keyword exclusions。
- [x] auto-tag 仅由成功的 `Check UVX Migration` `workflow_run` 触发，checkout immutable
  `head_sha`，最后一步才注入 PAT，只推 exact tag；publish 的 tag/manual 两入口都重新查询
  exact successful push run 并重新运行完整 gate。
- [x] publish 拆为无 OIDC build/gate、无 OIDC fresh artifact verifier、`environment: pypi`
  OIDC publish 三个 job；artifact 只含一个 wheel、一个 sdist 和 strict SHA-256 manifest。
  verifier 输出文件名与 SHA-256 摘要；OIDC job 不 checkout/导入/执行 wheel，只在 fresh runner
  重新核对完整 artifact 文件集合和这些摘要后发布确切路径。
- [x] privileged workflow actions 固定为 GitHub API 解析出的 immutable commit SHA，并在行尾
  标注 upstream release；新 release helpers/tests 加入 ancestry protected set。

### 同步后的验证与 residual

运行清单、命令和结果写入 `.superpowers/sdd/sync-upstream-task-5b-report.md`。上游 SSRF
redirect/DNS-rebinding 与 SVG SMIL sanitizer 仍明确列为 upstream residual，Task 5B 不修复、
不改写、不宣称已解决。

---

## Task 5B 复审修复：闭合残余旁路（实现记录）

复审 findings 已在 `fix/sync-upstream-ancestry` 之上的 TDD 变更中逐条闭合；本节记录最终实现，
不改变 Task 4 ancestry/history、包版本和 Task 5A 的发布链架构。

### Import 闭包与 trusted 目录

- `.github/scripts/check_release_gates.py` 在 import 任何第三方/标准库模块前把自身
  `.github/scripts` 目录从 `sys.path` 移除，并新增 `_validate_trusted_script_directory`：该目录
  只允许 `check_release_gates.py`、`check_sync_candidate.py`、`check_upstream_ancestry.py` 和
  `__pycache__`；`yaml.py` 等 shadow 即使以 `SystemExit(0)` 退出也会被拒绝。
- 三个 privileged workflow 全部改为 `python -I <script>`；release policy 拒绝裸 `python3`、
  非隔离调用和只在行内出现 `check_release_gates.py` 的命令。
- `check_uvx_repository.py` 把 `console_encoding` import 延迟到 `main()`。
- ancestry `PROTECTED_PATHSPECS` 覆盖 `.github/scripts/**` 与 `.github/workflows/**`；
  `PROTECTED_PATHS` 增加 `.github/scripts/yaml.py`、`console_encoding.py`、
  `workflow_transcript.py`、根与 Skill 的 `cli.py`。

### Release policy 与 scanner

- `_check_workflow_policy` 验证 check-uvx-migration.yml 的精确 job/step 集合、无 step-level
  `if`/`continue-on-error`、逐字 `python -I` 的 ancestry/migration 命令；no-op 替换、`exit 2`、
  重命名和额外步骤都 fail-closed。
- auto-tag 不再要求 `origin/main` tip 等于 release SHA：gate step 移除 `--require-origin-main-tip`，
  final push step 重新 fetch main、要求 release SHA 仍是 main 祖先并记录远端 tip，再 tag exact SHA。
- scanner 新增 `scan_text`：合并 shell 反斜杠续行并保留原始首行行号；删除
  `.github/workflows/check-uvx-migration.yml` 的旧 allowlist 旁路。
- wheel/sdist metadata 改用 `_validate_core_metadata` header parser，`Name`/`Version` 各恰好一个。

### 依赖与构建固定

- auto-tag/publish 的每个 `setup-uv` step 固定 uv `0.12.13` 和 GitHub release asset
  `uv-x86_64-unknown-linux-gnu.tar.gz` 的 SHA-256；policy 与测试同时强制。
- publish build job 以 exact pin 安装 PyYAML/Ruff/`setuptools`，并执行
  `uv build --no-build-isolation`，不解析 latest/range。
- publish concurrency group 只含 canonical tag/release SHA，不含 event name。

### 测试

- TDD 变更 + 实现后：focused 49 tests OK（1 expected Windows symlink skip）；Task 5A
  ownership/workflow/candidate 39 tests OK（1 skip）；完整记录见
  `.superpowers/sdd/sync-upstream-task-5b-report.md`。

---

## Task 5B 复审修复第二轮：exact 合同与闭包收口（实现记录）

reviewer 判定 Needs fixes 后，在 HEAD `9f8250a5` 上继续闭合 F1–F8；不改变版本、历史、
Task 5A 隔离、concurrency、uv pin/checksum 或上游 out-of-scope residual。

### F1/F2/F3：完整 step inventory 与 run 模板

- `check_release_gates.py` 为 auto-tag、Check UVX Migration 和 publish OIDC job 定义
  `WorkflowStepContract` 列表：step 数量、名称、顺序、key 集合、`uses`/`with`/`env` 和
  run block 全部 exact。所有关键 run 以规范化模板逐字比较（`_normalize_run` / `_require_run`），
  任何 no-op、前置 `exit 0`/`true`/`set +e`、`|| EXIT=0`、删除/重排/额外 step、
  `if`/`continue-on-error` 都会失败。
- 三个 privileged workflow 的 run block 整体扫描 `_check_git_write_surface`：禁止
  `git add|am|apply|cherry-pick|commit|merge|rebase|reset|update-ref`，push 数量按 workflow
  精确约束（auto-tag 唯一 1 个 approved tag push，publish/migration 0 个）；`git -c x=y commit`、
  `git --git-dir=.git commit`、`git push ...refs/heads/main`、`--force` 变体都由 token 扫描和
  exact push 模板拒绝。
- publish OIDC job 增加完整 step 白名单与 `_check_oidc_run_safety`：OIDC run 中禁止 pip/python/
  import/exec/eval/uvx/`.whl`，`WHEEL_PATH`/`SDIST_PATH` 只能出现在 approved bind/publish step；
  额外 step 或代码执行探针 fail-closed。

### F4/F5：Core Metadata 与 wheel 路径

- `_validate_core_metadata` 以 ASCII case-insensitive 归并字段：`Name`/`Version` 折叠后各恰好
  一个，非 canonical 大小写、重复大小写变体、body 中同名字段和非 ASCII 字段名都拒绝。
- wheel metadata 必须精确为 `ppt_master-{version}.dist-info/METADATA`，同目录 `WHEEL`/`RECORD`
  必须存在，其他 `*/METADATA` 拒绝；真实 setuptools wheel 布局兼容并在本地真实产物上验证。

### F6：CLI AST 闭包

- `parse_literal_mapping` 扫描全部 Store/Del 绑定，拒绝 For/AsyncFor/With/ExceptHandler/
  comprehension/NamedExpr/arg/global/nonlocal/match 以及 `exec`/`eval`/`globals`/`locals`/
  `setattr`/`vars`/`compile`/`__import__` 等动态逃逸。
- `check_uvx_repository` 删除自己的 AST 副本，改为在调用时加载同一个 trusted
  `check_cli_sync.parse_literal_mapping`，不再有漂移的 exclusion grammar。

### F7：scanner grammar 与扫描根

- 解释器 grammar 覆盖 `python.exe`、`python3.12`、`python3.12.exe`、`python ./scripts/...`
  以及 `uv run` 中间 flags（`--no-sync`、`--python 3.12`）；反斜杠续行合并后仍报告原始首行号。
- `DOCUMENT_ROOT_PREFIXES` 增加 `.claude-plugin/`、`projects/`；删除迁移 workflow 的孤立
  allowlist 注释；allowlist 仍是精确 path+rule（新增文档根上的文件不会继承旧豁免）。

### F8：文本 subprocess 解码

- release gate、candidate checker 与 migration checker 的所有 `text=True` 调用显式
  `encoding="utf-8", errors="replace"`，并以 mock 测试固定；candidate 的 Git 文本调用统一
  经过 `_run_git_text`。

### Auto-tag tip 语义

维护者确认：发布 exact successful immutable SHA；验证后 main 普通前进不改变已发布产物，
并触发独立新 run；不再要求不可能的原子 latest-tip 相等。brief/plan/design/report 已统一。

---

## Task 5B Approved 后尾项：M1–M4 环境与参数逃逸收口（实现记录）

在 Approved 判定后的维护轮，对三个 privileged workflow 的 job 信封、publish build/verify
step 合同、CLI Call 参数与 Core Metadata 字段名做最后收口；不改变已批准语义。

### M1：workflow/job key 白名单

- 每个 privileged workflow 的顶层 key 只允许 `name/on/permissions/concurrency/jobs`；workflow 层
  显式拒绝 `env`（含 `BASH_ENV`）、`defaults`、`container`、`services`。
- 每个 job 使用 key 白名单与 exact 值：auto-tag 唯一 job 保留 exact condition、`ubuntu-latest`
  与 30 分钟 timeout；migration job 只允许 `runs-on/steps`；publish 三个 job 分别固定
  runner/timeout/needs/outputs/environment。未批准的 job-level `env`/`defaults`/`container`/
  `services` 与多余 `if` 一律 fail-closed。
- 探针：migration 的 BASH_ENV/defaults/`if: false`，auto-tag 的 BASH_ENV 与条件改写，
  publish 的 container/services/workflow-level env 都拒绝。

### M4：publish build-and-gate 与 verify-artifact exact step 合同

- 两个 job 的 step 数量、名称、顺序、key 集合、`uses`/`with`/`env`、run 模板和 step id 全部
  exact；额外/重排/rename/`with`/env/run 变形都失败，无凭据 job 也自证只执行 approved 步骤。

### M2：CLI Call 参数与间接调用

- `parse_literal_mapping` 拒绝把 COMMANDS/ALIASES 作为未批准 Call 的 positional/keyword/
  `*args`/`**kwargs` 参数，拒绝 `getattr`、`operator.setitem`、`builtins.__dict__[...]` 等间接
  调用与 subscript callable；只读白名单（`sorted`/`len`/`.get()`/`.keys()` 等）不误杀。
- 探针：`unknown_helper(COMMANDS)`、`dict.update(COMMANDS)`、`operator.setitem`、`getattr exec`、
  `builtins.__dict__['exec']`；正例覆盖 `sorted(COMMANDS)` 与 `.get()` 读取。

### M3：Core Metadata 字段名空白

- 字段名必须满足 `field == field.strip()`，colon 前不得有 space/tab；wheel 与 sdist 测试都覆盖
  `Name :`/`Name\t:` 形态。

### 验证

第二轮全量记录见 `.superpowers/sdd/sync-upstream-task-5b-report.md` 的 M1–M4 章节。
