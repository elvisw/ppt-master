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
- schedule 的 OpenCode Action 保留建分支/PR职责；workflow_dispatch 的模型不得获得 push 凭据或执行 push。
- manual push 必须使用 `git push origin HEAD:main`；non-fast-forward 时停止并从最新 main 重跑，禁止 force-push。
- fork 仓库设置仅允许 merge commit；关闭 squash merge 和 rebase merge。
- ancestry 门禁失败必须阻断 `Check UVX Migration → auto-tag → publish-pypi`，不得以内容相同或模型声明替代。
- 不 bump 版本，不自动 push，不创建远端 PR；设计/实现/repair 只提交到本地 `fix/sync-upstream-ancestry`。
- Windows 下所有 Python 命令使用 `python`，不用 `python3`。
- 不在 `skills/ppt-master/scripts/tests/` 之外创建测试；workflow 文本用沙盘和真实 Git 命令验证。

---

### Task 1: 固定上游 SHA 与真实 merge 契约

**Files:**
- Create: `.github/upstream-main.sha`
- Modify: `.opencode/command/sync-upstream.md:23-46,245-286,303-353`
- Modify: `.github/workflows/sync-upstream.yml:35-123`

**Interfaces:**
- Produces: `steps.upstream.outputs.upstream_sha`，值为 fetch 后的 40 位 upstream tip SHA。
- Produces: `.github/upstream-main.sha`，供 Task 2 的 PR/main checks 消费。
- Contract: OpenCode 创建真实 merge commit；workflow_dispatch 只由 workflow push。

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

在 Step 1 的 fetch 后建立目标：GitHub Actions 使用 prompt 提供的
`EXPECTED_UPSTREAM_SHA` 并要求它等于 fetch tip；本地运行在变量为空时取
`git rev-parse upstream/main`。随后验证 40 位格式并写入目标文件：

```bash
FETCHED_UPSTREAM_SHA=$(git rev-parse upstream/main)
if [ -z "${EXPECTED_UPSTREAM_SHA:-}" ]; then
  EXPECTED_UPSTREAM_SHA="$FETCHED_UPSTREAM_SHA"
fi
if [ "$EXPECTED_UPSTREAM_SHA" != "$FETCHED_UPSTREAM_SHA" ]; then
  echo "Expected upstream SHA does not match fetched upstream/main" >&2
  exit 1
fi
printf '%s\n' "$EXPECTED_UPSTREAM_SHA" > .github/upstream-main.sha
```

Step 2 的唯一 merge 命令改为：

```bash
git merge --no-ff --no-commit "$EXPECTED_UPSTREAM_SHA"
```

紧随其后写明 Global Constraints 中的历史改写禁令和允许的 abort 路径。

- [x] **Step 4: 在 Step 4e 加入提交前 merge-state 门禁**

现有六项门禁扩为七项；第 7 项在 merge commit 前验证：

```bash
test "$(git rev-parse -q --verify MERGE_HEAD)" = "$EXPECTED_UPSTREAM_SHA"
test "$(tr -d '\r\n' < .github/upstream-main.sha)" = "$EXPECTED_UPSTREAM_SHA"
```

任一非零都停止，不提交。

- [x] **Step 5: 改写 Step 6 的提交与发布边界**

merge 提交显式 stage 目标文件：

```bash
PRE_MERGE_HEAD=$(git rev-parse HEAD)
git add -u
git add .github/upstream-main.sha cli.py skills/ppt-master/cli.py pyproject.toml skills/ppt-master/pyproject.toml
git commit -m "merge upstream/main: resolve conflicts, adapt to uvx, sync cli.py mappings"
SYNC_MERGE_COMMIT=$(git rev-parse HEAD)
test "$(git rev-parse "$SYNC_MERGE_COMMIT^1")" = "$PRE_MERGE_HEAD"
test "$(git rev-parse "$SYNC_MERGE_COMMIT^2")" = "$EXPECTED_UPSTREAM_SHA"
git merge-base --is-ancestor "$EXPECTED_UPSTREAM_SHA" "$SYNC_MERGE_COMMIT"
```

版本提交保持独立；完成后再次运行：

```bash
git merge-base --is-ancestor "$EXPECTED_UPSTREAM_SHA" HEAD
```

GitHub Actions 的两个路径都写成“不 push，由 workflow/action 基础设施负责”；
本地路径保持现有显式 push/tag 指令。

- [x] **Step 6: 修改 workflow 的目标输出和两个 prompt**

`Check upstream changes` 在 `git fetch` 后新增：

```bash
UPSTREAM_SHA=$(git rev-parse upstream/main)
echo "upstream_sha=$UPSTREAM_SHA" >> "$GITHUB_OUTPUT"
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
移除 manual OpenCode step 的 `GH_TOKEN`，删除模型运行前的 `Configure git for push`。

- [x] **Step 7: 增加 manual verify 与 push steps**

OpenCode manual step 后新增：

```yaml
      - name: Verify upstream ancestry
        if: github.event_name == 'workflow_dispatch'
        env:
          BASE_SHA: ${{ github.sha }}
          EXPECTED_UPSTREAM_SHA: ${{ steps.upstream.outputs.upstream_sha }}
        run: |
          RECORDED_SHA=$(tr -d '\r\n' < .github/upstream-main.sha)
          if [ "$RECORDED_SHA" != "$EXPECTED_UPSTREAM_SHA" ]; then
            echo "::error::Recorded SHA $RECORDED_SHA does not match expected $EXPECTED_UPSTREAM_SHA"
            exit 1
          fi
          if ! git merge-base --is-ancestor "$EXPECTED_UPSTREAM_SHA" HEAD; then
            echo "::error::Expected upstream SHA is not an ancestor of HEAD"
            git log --graph --oneline --decorate "$BASE_SHA..HEAD"
            exit 1
          fi
          MERGE_COMMIT=$(git rev-list --merges --parents "$BASE_SHA..HEAD" |
            awk -v target="$EXPECTED_UPSTREAM_SHA" '{ for (i = 2; i <= NF; i++) if ($i == target) { print $1; exit } }')
          if [ -z "$MERGE_COMMIT" ]; then
            echo "::error::No new merge commit has expected upstream SHA as a direct parent"
            git log --graph --oneline --decorate "$BASE_SHA..HEAD"
            exit 1
          fi
          echo "Verified merge commit $MERGE_COMMIT with upstream parent $EXPECTED_UPSTREAM_SHA"

      - name: Push verified manual sync
        if: github.event_name == 'workflow_dispatch'
        env:
          PUSH_PAT: ${{ secrets.PUSH_PAT }}
        run: |
          git remote set-url origin "https://x-access-token:${PUSH_PAT}@github.com/elvisw/ppt-master.git"
          git push origin HEAD:main
```

目标文件必须等于 output SHA、目标必须是 HEAD 祖先、`${{ github.sha }}..HEAD` 中
必须存在直接父等于目标的 merge commit。PAT 只在最后一个 step 注入。

- [x] **Step 8: 静态验证并提交**

Run:

```powershell
python -c "import pathlib,yaml; yaml.safe_load(pathlib.Path('.github/workflows/sync-upstream.yml').read_text(encoding='utf-8')); print('sync-upstream.yml: OK')"
git diff --check
```

Expected: `sync-upstream.yml: OK`，`git diff --check` 无错误。

Commit:

```powershell
git add .github/upstream-main.sha .opencode/command/sync-upstream.md .github/workflows/sync-upstream.yml docs/superpowers/plans/2026-09-10-sync-upstream-ancestry.md
git commit -m "ci: preserve upstream merge ancestry"
```

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
