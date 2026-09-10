# 上游同步提交关系修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 schedule、workflow_dispatch 和发布链都机械验证选定上游提交的 ancestry，并以一次真实 merge 修复当前缺失的 `64b65839` 父关系。

**Architecture:** `.github/upstream-main.sha` 持久化每次同步选定的 immutable upstream SHA；`.opencode/command/sync-upstream.md` 是提交关系程序的唯一所有者，两个 OpenCode prompt 只传值和强调不可绕过门禁。PR check 验证目标 SHA、直接 merge parent 与 head ancestry，`check-uvx-migration.yml` 再在 `main` 发布边界验证登记目标，manual push 由 workflow 在验证后执行。

**Tech Stack:** Git、GitHub Actions YAML、POSIX shell、OpenCode Action、GitHub CLI、Python 3.12 + PyYAML（仅做 YAML 语法验证）。

## Global Constraints

- 所有 GitHub 操作仅指向 `elvisw/ppt-master`，不得修改 `hugohe3/ppt-master`。
- 当前修复目标固定为 `64b65839c7f8096a534c872c03d688a2b2491c8f`。
- 从 merge 开始到 merge commit 创建完成，禁止 reset、rebase、squash、cherry-pick、切换分支和清除 `MERGE_HEAD`；失败只允许 `git merge --abort` 后停止。
- `.github/upstream-main.sha` 只含一行 40 位小写 SHA 和结尾换行。
- schedule 的 OpenCode Action 保留建分支/PR职责；workflow_dispatch 仅允许
  `GITHUB_REF == refs/heads/main`，guard 缺失或不匹配必须 fail-closed。
- identity step 只能配置 `github-actions[bot]` 的 name/email，不得设置 token、remote
  或 push 凭据；workflow_dispatch 的模型不得获得 `PUSH_PAT` 或执行 push。
- schedule/manual 两个 OpenCode step 都必须通过 env 注入
  `EXPECTED_UPSTREAM_SHA=${{ steps.upstream.outputs.upstream_sha }}`；Actions 中变量缺失
  或与 fetch 后 `upstream/main` 不匹配必须立即失败，只有本地运行允许 fallback fetch。
- manual OpenCode、Verify、Push 的 if 必须同时满足
  `workflow_dispatch && steps.upstream.outputs.has_changes == 'true'`；无变化必须正常跳过。
- Verify 必须先以 `git diff --quiet && git diff --cached --quiet` 拒绝脏工作树，再以
  `git show HEAD:.github/upstream-main.sha | tr -d '\r\n'` 读取已提交 marker，不得读取工作树 marker。
- Verify 必须确认 `BASE_SHA` 与 expected 都是 HEAD 祖先，并找到恰好双父 merge：
  第一父严格等于 `BASE_SHA`，第二父严格等于 `EXPECTED_UPSTREAM_SHA`。
- `.github/upstream-main.sha` 只有在
  `git merge --no-ff --no-commit "$EXPECTED_UPSTREAM_SHA"` 成功后才能写入/暂存；冲突
  必须 `git merge --abort` 后停止，不能残留 marker 修改。
- manual push 必须使用 `git push origin HEAD:main`；non-fast-forward 时停止并从最新 main 重跑，禁止 force-push。
- fork 仓库设置仅允许 merge commit；关闭 squash merge 和 rebase merge。
- ancestry 门禁失败必须阻断 `Check UVX Migration → auto-tag → publish-pypi`，不得以内容相同或模型声明替代。
- 本次修复执行不 bump 版本、不执行远端 push/PR、也不做 repair；运行时契约仍是
  schedule 由 Action 创建 PR，workflow_dispatch 仅在 Verify 成功后由 workflow push。
  设计/实现提交只落在本地 `fix/sync-upstream-ancestry`。
- Windows 下所有 Python 命令使用 `python`，不用 `python3`。
- 不在 `skills/ppt-master/scripts/tests/` 之外创建测试；workflow 文本用沙盘和真实 Git 命令验证。

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
- Contract: OpenCode 创建真实 merge commit；workflow_dispatch 只由 workflow push。
- Contract: manual workflow 只能从 `refs/heads/main` 运行；无变化时 manual OpenCode、
  Verify、Push 全部跳过。
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

在 Step 1 的 fetch 后建立目标：GitHub Actions 使用 workflow env 注入的
`EXPECTED_UPSTREAM_SHA` 并要求它等于 fetch tip；Actions 中变量为空立即失败；本地运行
在变量为空时才取 `git rev-parse upstream/main`。随后显式验证 40 位小写格式。目标文件
不得在 merge 前写入：

```bash
FETCHED_UPSTREAM_SHA=$(git rev-parse upstream/main)
if [ "${GITHUB_ACTIONS:-}" = "true" ] && [ -z "${EXPECTED_UPSTREAM_SHA:-}" ]; then
  echo "EXPECTED_UPSTREAM_SHA is required in GitHub Actions" >&2
  exit 1
fi
if [ -z "${EXPECTED_UPSTREAM_SHA:-}" ]; then
  EXPECTED_UPSTREAM_SHA="$FETCHED_UPSTREAM_SHA"
fi
if ! printf '%s\n' "$EXPECTED_UPSTREAM_SHA" | grep -Eq '^[0-9a-f]{40}$'; then
  echo "Expected upstream SHA is not a 40-character lowercase SHA" >&2
  exit 1
fi
if [ "$EXPECTED_UPSTREAM_SHA" != "$FETCHED_UPSTREAM_SHA" ]; then
  echo "Expected upstream SHA does not match fetched upstream/main" >&2
  exit 1
fi
```

Step 2 在保存原始 HEAD 后执行唯一 merge 命令；仅当 merge 成功才写入并暂存目标文件：

```bash
PRE_MERGE_HEAD=$(git rev-parse HEAD)
if git merge --no-ff --no-commit "$EXPECTED_UPSTREAM_SHA"; then
  :
else
  MERGE_EXIT=$?
  git merge --abort
  exit "$MERGE_EXIT"
fi
printf '%s\n' "$EXPECTED_UPSTREAM_SHA" > .github/upstream-main.sha
git add .github/upstream-main.sha
```

merge 失败（包括冲突）必须 `git merge --abort` 后立即停止；任何 abort 失败也必须
fail-closed。紧随其后写明 Global Constraints 中的历史改写禁令和允许的 abort 路径。

- [x] **Step 4: 在 Step 4e 加入提交前 merge-state 门禁**

现有六项门禁扩为七项；第 7 项在 merge commit 前用显式 `if`/`exit` 验证 merge 状态
与已暂存目标文件（不使用裸 `test`）：

```bash
if ! MERGE_HEAD_SHA=$(git rev-parse -q --verify MERGE_HEAD); then
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

任一非零都停止，不提交；工作树 marker 也必须与索引值相同。

- [x] **Step 5: 改写 Step 6 的提交与发布边界**

`PRE_MERGE_HEAD` 必须来自 Step 2 的 merge 前；提交后必须用显式 `if`/`exit` 验证
恰好两个父，且 `^1` 为原始 HEAD、`^2` 为 expected：

```bash
if [ -z "${PRE_MERGE_HEAD:-}" ]; then
  echo "PRE_MERGE_HEAD was not recorded before merge" >&2
  exit 1
fi
git add -u
git add .github/upstream-main.sha cli.py skills/ppt-master/cli.py pyproject.toml skills/ppt-master/pyproject.toml
git commit -m "merge upstream/main: resolve conflicts, adapt to uvx, sync cli.py mappings"
SYNC_MERGE_COMMIT=$(git rev-parse HEAD)
MERGE_PARENTS=$(git show -s --format=%P "$SYNC_MERGE_COMMIT")
PARENT_COUNT=$(printf '%s\n' "$MERGE_PARENTS" | awk '{ print NF }')
if [ "$PARENT_COUNT" -ne 2 ]; then
  echo "The sync commit must have exactly two parents" >&2
  exit 1
fi
if [ "$(git rev-parse "$SYNC_MERGE_COMMIT^1")" != "$PRE_MERGE_HEAD" ]; then
  echo "The first parent is not PRE_MERGE_HEAD" >&2
  exit 1
fi
if [ "$(git rev-parse "$SYNC_MERGE_COMMIT^2")" != "$EXPECTED_UPSTREAM_SHA" ]; then
  echo "The second parent is not EXPECTED_UPSTREAM_SHA" >&2
  exit 1
fi
if ! git merge-base --is-ancestor "$EXPECTED_UPSTREAM_SHA" "$SYNC_MERGE_COMMIT"; then
  echo "Expected upstream SHA is not an ancestor of the sync commit" >&2
  exit 1
fi
```

版本提交保持独立；完成后再次用显式 `if`/`exit` 运行：

```bash
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
          if [ -z "${BASE_SHA:-}" ] || [ -z "${EXPECTED_UPSTREAM_SHA:-}" ]; then
            echo "::error::BASE_SHA and EXPECTED_UPSTREAM_SHA are required"
            exit 1
          fi
          if ! git cat-file -e "HEAD:.github/upstream-main.sha"; then
            echo "::error::HEAD does not contain .github/upstream-main.sha"
            exit 1
          fi
          RECORDED_SHA=$(git show HEAD:.github/upstream-main.sha | tr -d '\r\n')
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
          MERGE_LINES=$(git rev-list --merges --parents "$BASE_SHA..HEAD")
          MATCHES=$(printf '%s\n' "$MERGE_LINES" |
            awk -v base="$BASE_SHA" -v expected="$EXPECTED_UPSTREAM_SHA" 'NF == 3 && $2 == base && $3 == expected { print $1 }')
          MATCH_COUNT=$(printf '%s\n' "$MATCHES" | awk 'NF { count += 1 } END { print count + 0 }')
          if [ "$MATCH_COUNT" -ne 1 ]; then
            echo "::error::Expected exactly one two-parent merge with ^1=$BASE_SHA and ^2=$EXPECTED_UPSTREAM_SHA"
            git log --graph --oneline --decorate "$BASE_SHA..HEAD"
            exit 1
          fi
          echo "Verified two-parent merge commit $MATCHES with ^1=$BASE_SHA and ^2=$EXPECTED_UPSTREAM_SHA"

      - name: Push verified manual sync
        if: github.event_name == 'workflow_dispatch' && steps.upstream.outputs.has_changes == 'true'
        env:
          PUSH_PAT: ${{ secrets.PUSH_PAT }}
        run: |
          if [ -z "${PUSH_PAT:-}" ]; then
            echo "::error::PUSH_PAT is required for the verified manual push"
            exit 1
          fi
          git remote set-url origin "https://x-access-token:${PUSH_PAT}@github.com/elvisw/ppt-master.git"
          git push origin HEAD:main
```

workflow 的 manual OpenCode、Verify、Push 三个 step 的 if 必须逐字满足
`github.event_name == 'workflow_dispatch' && steps.upstream.outputs.has_changes == 'true'`。
目标文件必须从 `HEAD` 读取并等于 output SHA；工作树和 index 必须干净；BASE/目标必须
是 HEAD 祖先；`${{ github.sha }}..HEAD` 中必须存在且仅存在一个恰好双父、直接父序为
`^1=BASE_SHA`、`^2=EXPECTED_UPSTREAM_SHA` 的 merge commit。PAT 只在最后一个 step 注入。

- [x] **Step 8: 静态验证并提交**

Run:

```powershell
python -c "import pathlib,yaml; data=yaml.safe_load(pathlib.Path('.github/workflows/sync-upstream.yml').read_text(encoding='utf-8')); assert data['jobs']['sync-upstream']['steps']; print('sync-upstream.yml: OK')"
python -m unittest discover -s skills/ppt-master/scripts/tests -p "test_sync_upstream_task1_extension.py" -v
git diff --check
```

Expected: `sync-upstream.yml: OK`，契约回归测试通过，`git diff --check` 无错误；另须
运行真实 Git 行为沙盘覆盖 main/non-main、has_changes true/false、正确双父、目标在第一
父/多父、工作树脏、marker 未提交，并记录每个 exit code、parents 和输出。

Commit:

```powershell
git add .github/upstream-main.sha .opencode/command/sync-upstream.md .github/workflows/sync-upstream.yml docs/superpowers/plans/2026-09-10-sync-upstream-ancestry.md
git commit -m "ci: preserve upstream merge ancestry"
```

### Task 1 扩展：fail-closed workflow 与 merge 验证（本轮修订）

- [x] 在 manual OpenCode 前加入 `GITHUB_REF == refs/heads/main` guard；identity step
  仅配置 `github-actions[bot]` name/email，不含 token 或 remote。
- [x] schedule/manual OpenCode 均通过 env 注入
  `EXPECTED_UPSTREAM_SHA=${{ steps.upstream.outputs.upstream_sha }}`；Actions 缺失或
  不匹配立即失败，本地才允许 fallback fetch。
- [x] manual OpenCode、Verify、Push 均使用
  `workflow_dispatch && steps.upstream.outputs.has_changes == 'true'`，no-change 正常跳过；
  manual CLI 使用 `OPENCODE_MODEL` 和 `-m "$OPENCODE_MODEL"`。
- [x] Verify 先检查 `git diff --quiet && git diff --cached --quiet`，再读取
  `git show HEAD:.github/upstream-main.sha | tr -d '\r\n'`；同时验证 BASE/expected
  ancestry 和唯一恰好双父 merge 的父序 `[BASE_SHA, EXPECTED_UPSTREAM_SHA]`。
- [x] marker 只在 merge 成功后写入/暂存；冲突先 `git merge --abort` 后停止；`.opencode`
  中关键校验均为显式 `if`/`exit`。
- [x] 追加 PyYAML、静态契约、ref/no-change 和真实 Git 行为沙盘验证；记录到
  `.superpowers/sdd/sync-upstream-task-1-report.md`。临时测试验证后删除，不纳入提交。

---

### Task 2: PR 与 main 的确定性 ancestry 门禁

**Files:**
- Create: `.github/workflows/check-upstream-ancestry.yml`
- Modify: `.github/workflows/check-uvx-migration.yml:10-25`

**Interfaces:**
- Consumes: `.github/upstream-main.sha` from Task 1。
- Produces: PR check `Check Upstream Ancestry / check`。
- Produces: main gate `Verify recorded upstream ancestry`，位于现有 uvx migration check 之前。

- [ ] **Step 1: 写入 PR check workflow**

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
      - uses: actions/checkout@v6
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
          MERGE_COMMIT=$(git rev-list --merges --parents "$BASE_SHA..$HEAD_SHA" |
            awk -v target="$TARGET" '{ for (i = 2; i <= NF; i++) if ($i == target) { print $1; exit } }')
          if [ -z "$MERGE_COMMIT" ]; then
            echo "::error::No new merge commit has recorded upstream SHA as a direct parent"
            git log --graph --oneline --decorate "$BASE_SHA..$HEAD_SHA"
            exit 1
          fi
          echo "Verified merge commit $MERGE_COMMIT with upstream parent $TARGET"
```

该 workflow 不带 `paths` filter，也不依赖同步分支命名。

- [ ] **Step 2: 在 main 发布入口增加记录目标门禁**

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

- [ ] **Step 3: YAML 与结构验证**

Run:

```powershell
python -c "import pathlib,yaml; [yaml.safe_load(pathlib.Path(p).read_text(encoding='utf-8')) for p in ['.github/workflows/check-upstream-ancestry.yml','.github/workflows/check-uvx-migration.yml']]; print('ancestry workflows: OK')"
git diff --check
```

Expected: `ancestry workflows: OK`，无 diff whitespace 错误。

- [ ] **Step 4: 沙盘验证 PR check 核心逻辑**

在 `C:\Users\elvis\AppData\Local\Temp\opencode` 创建临时 bare/clone 沙盘，构造：

- 目标文件不变的普通提交：check 成功并输出 skip。
- 目标文件更新 + `git merge --no-ff --no-commit` + commit：check 成功并找到 merge commit。
- 目标文件更新 + 普通单父 commit：check exit 1。
- 非 40 位或不属于 upstream 历史的目标：check exit 1。

每种情况记录命令、exit code、parents 和输出；不得只做 YAML 文本匹配。

- [ ] **Step 5: 提交**

```powershell
git add .github/workflows/check-upstream-ancestry.yml .github/workflows/check-uvx-migration.yml
git commit -m "ci: gate upstream ancestry before publish"
```

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
- Consumes: implementation branch HEAD、upstream target `64b65839...`。
- Produces: a real merge commit with parents `[pre-merge implementation HEAD, 64b65839...]`。

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

记录 `PRE_MERGE_HEAD=$(git rev-parse HEAD)`，然后执行：

```powershell
git merge --no-ff --no-commit 64b65839c7f8096a534c872c03d688a2b2491c8f
```

Expected: merge 成功并保留 `MERGE_HEAD=64b65839...`。若冲突，逐文件保留 fork uvx
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
git commit -m "merge upstream/main: repair preserved ancestry"
```

禁止 reset/rebase/squash/cherry-pick。记录新 commit SHA。

- [ ] **Step 5: 正反 ancestry 和父节点验证**

```powershell
git merge-base --is-ancestor 64b65839c7f8096a534c872c03d688a2b2491c8f HEAD
git log HEAD..64b65839c7f8096a534c872c03d688a2b2491c8f --oneline
git show -s --format=%P HEAD
git log --graph --oneline --decorate --max-count=15
```

Expected: 第一条 exit 0；第二条无输出；parents 第一项为 `PRE_MERGE_HEAD`、第二项为
`64b65839...`；提交图显示真实双父 merge。

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
