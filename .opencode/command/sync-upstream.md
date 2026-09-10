---
description: 合并上游更新，解决冲突，适配uvx命令，补全cli.py映射，发布新版本
agent: general
---

# 上游同步命令

执行上游项目 (hugohe3/ppt-master) 的完整合并 → uvx 适配 → 版本发布流程。

## 前置条件

- 工作区干净 (`git status` 无变更)
- `rg` (ripgrep) 已安装（Windows: `winget install BurntSushi.ripgrep.MSVC`，macOS: `brew install ripgrep`）
- 已配置 `upstream` remote: `https://github.com/hugohe3/ppt-master.git`
- 已配置 `origin` remote: 本 fork
- GitHub Actions 中必须由 workflow 注入 `EXPECTED_UPSTREAM_SHA`；变量缺失或不匹配时
  fail-closed。只有本地运行才允许从 fetch 后的 `upstream/main` 回退推导目标。

## 执行步骤

严格按照以下顺序执行，每步完成后确认无误再继续。

---

### Step 1: 拉取上游

```bash
if ! UPSTREAM_URL=$(git config --get remote.upstream.url); then
  echo "Unable to read the upstream remote URL" >&2
  exit 1
fi
case "$UPSTREAM_URL" in
  https://github.com/hugohe3/ppt-master.git|\
  https://github.com/hugohe3/ppt-master|\
  git@github.com:hugohe3/ppt-master.git|\
  ssh://git@github.com/hugohe3/ppt-master.git)
    ;;
  *)
    echo "The upstream remote is not the canonical hugohe3/ppt-master repository" >&2
    exit 1
    ;;
esac
if [ "${GITHUB_ACTIONS:-}" = "true" ]; then
  echo "GitHub Actions: reusing upstream/main already fetched by the workflow"
else
  if ! git fetch upstream main; then
    echo "Unable to fetch canonical upstream/main; refusing to use a stale ref" >&2
    exit 1
  fi
fi
if ! git rev-parse --verify refs/remotes/upstream/main^{commit} >/dev/null; then
  echo "Fetched upstream/main is unavailable" >&2
  exit 1
fi
git log HEAD..upstream/main --oneline
```

记录上游新增的提交数量和主题。

**建立本次同步的 immutable upstream 目标。** GitHub Actions 环境中，workflow 注入的
`EXPECTED_UPSTREAM_SHA` 就是本次运行必须合并的确切提交；Actions 中变量为空时立即
停止。本地运行中该变量为空时，才允许取 fetch 后的 upstream tip。两者都必须等于
fetch 后的 `upstream/main`，否则立即停止。目标值和后续代码块需要的原始 HEAD 都必须
写入 Git 内部临时状态，不能依赖普通 shell 变量跨越代码块：

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

if ! mkdir -- "$SYNC_STATE_DIR"; then
  echo "A ppt-master sync is already active; refusing to share its state" >&2
  exit 1
fi

cleanup_sync_state() {
  if [ ! -d "$SYNC_STATE_DIR" ]; then
    echo "Git-internal sync state directory is missing" >&2
    return 1
  fi
  if ! rm -rf -- "$SYNC_STATE_DIR"; then
    echo "Unable to clean the Git-internal sync state directory" >&2
    return 1
  fi
}

fail_sync() {
  FAILURE_STATUS="${1:-1}"
  if ! cleanup_sync_state; then
    FAILURE_STATUS=1
  fi
  exit "$FAILURE_STATUS"
}

if [ -e "$SYNC_MERGE_HEAD_PATH" ]; then
  echo "A merge already exists before this sync run; refusing to abort it" >&2
  if ! cleanup_sync_state; then
    exit 1
  fi
  exit 1
fi

if ! FETCHED_UPSTREAM_SHA=$(git rev-parse upstream/main); then
  echo "Unable to resolve fetched upstream/main" >&2
  fail_sync 1
fi
if ! printf '%s\n' "$FETCHED_UPSTREAM_SHA" | grep -Eq '^[0-9a-f]{40}$'; then
  echo "Fetched upstream tip is not a 40-character lowercase SHA" >&2
  fail_sync 1
fi
if [ "${GITHUB_ACTIONS:-}" = "true" ]; then
  if [ -z "${EXPECTED_UPSTREAM_SHA:-}" ]; then
    echo "EXPECTED_UPSTREAM_SHA is required in GitHub Actions" >&2
    fail_sync 1
  fi
  CANDIDATE_UPSTREAM_SHA="$EXPECTED_UPSTREAM_SHA"
elif [ -z "${EXPECTED_UPSTREAM_SHA:-}" ]; then
  CANDIDATE_UPSTREAM_SHA="$FETCHED_UPSTREAM_SHA"
else
  CANDIDATE_UPSTREAM_SHA="$EXPECTED_UPSTREAM_SHA"
fi
if ! printf '%s\n' "$CANDIDATE_UPSTREAM_SHA" | grep -Eq '^[0-9a-f]{40}$'; then
  echo "Expected upstream SHA is not a 40-character lowercase SHA" >&2
  fail_sync 1
fi
if [ "$CANDIDATE_UPSTREAM_SHA" != "$FETCHED_UPSTREAM_SHA" ]; then
  echo "Expected upstream SHA does not match fetched upstream/main" >&2
  fail_sync 1
fi
if ! printf '%s\n' "$CANDIDATE_UPSTREAM_SHA" > "$SYNC_EXPECTED_STATE"; then
  echo "Unable to persist the expected upstream SHA in Git-internal state" >&2
  fail_sync 1
fi
echo "Persisted expected upstream SHA in Git-internal state"
```

目标值必须是 40 位小写十六进制 SHA。目标文件只能在 Step 2 的 merge 成功并验证
`MERGE_HEAD` 后写入并暂存；Step 4e 门禁 7 与 Step 6 的提交程序从同一个 Git 内部状态
重新读取目标值。Step 1 失败时只清理本次创建的临时状态，不 abort 用户已有的 merge。

---

### Step 2: 合并上游

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

cleanup_sync_state() {
  if [ ! -d "$SYNC_STATE_DIR" ]; then
    echo "Git-internal sync state directory is missing" >&2
    return 1
  fi
  if ! rm -rf -- "$SYNC_STATE_DIR"; then
    echo "Unable to clean the Git-internal sync state directory" >&2
    return 1
  fi
}

sync_state_complete() {
  if [ ! -d "$SYNC_STATE_DIR" ] ||
     [ ! -f "$SYNC_EXPECTED_STATE" ] ||
     [ ! -f "$SYNC_ORIGINAL_HEAD_STATE" ] ||
     [ ! -f "$SYNC_MARKER_STATE" ] ||
     [ ! -f "$SYNC_MERGE_STARTED_STATE" ]; then
    return 1
  fi
  if ! MARKER_ORIGINAL_STATE=$(tr -d '\r\n' < "$SYNC_MARKER_STATE"); then
    return 1
  fi
  if [ "$MARKER_ORIGINAL_STATE" = "tracked" ] && [ ! -f "$SYNC_MARKER_SNAPSHOT" ]; then
    return 1
  fi
  if [ "$MARKER_ORIGINAL_STATE" != "tracked" ] &&
     [ "$MARKER_ORIGINAL_STATE" != "absent" ]; then
    return 1
  fi
  return 0
}

restore_marker_from_state() {
  if ! ORIGINAL_HEAD_SHA=$(tr -d '\r\n' < "$SYNC_ORIGINAL_HEAD_STATE"); then
    echo "Unable to read the persisted original HEAD for marker restore" >&2
    return 1
  fi
  if ! MARKER_ORIGINAL_STATE=$(tr -d '\r\n' < "$SYNC_MARKER_STATE"); then
    echo "Unable to read the persisted marker existence state" >&2
    return 1
  fi
  if [ "$MARKER_ORIGINAL_STATE" = "tracked" ]; then
    if ! git restore --source="$ORIGINAL_HEAD_SHA" --staged --worktree -- .github/upstream-main.sha; then
      echo "Unable to restore the tracked upstream marker" >&2
      return 1
    fi
    if ! SNAPSHOT_SHA=$(git hash-object -- "$SYNC_MARKER_SNAPSHOT"); then
      echo "Unable to hash the saved upstream marker snapshot" >&2
      return 1
    fi
    if ! WORKTREE_MARKER_SHA=$(git hash-object -- .github/upstream-main.sha); then
      echo "Unable to hash the restored upstream marker" >&2
      return 1
    fi
    if ! INDEX_MARKER_SHA=$(git rev-parse :".github/upstream-main.sha"); then
      echo "Unable to hash the restored index marker" >&2
      return 1
    fi
    if [ "$WORKTREE_MARKER_SHA" != "$SNAPSHOT_SHA" ] ||
       [ "$INDEX_MARKER_SHA" != "$SNAPSHOT_SHA" ]; then
      echo "Restored marker bytes do not match the pre-merge snapshot" >&2
      return 1
    fi
  elif [ "$MARKER_ORIGINAL_STATE" = "absent" ]; then
    if ! git rm --cached --ignore-unmatch -- .github/upstream-main.sha; then
      echo "Unable to remove the newly tracked upstream marker" >&2
      return 1
    fi
    if { [ -e .github/upstream-main.sha ] || [ -L .github/upstream-main.sha ]; } &&
       ! rm -f -- .github/upstream-main.sha; then
      echo "Unable to remove the newly created upstream marker" >&2
      return 1
    fi
    if [ -e .github/upstream-main.sha ] || [ -L .github/upstream-main.sha ] ||
       git ls-files --error-unmatch -- .github/upstream-main.sha >/dev/null 2>&1; then
      echo "The absent upstream marker was not fully restored" >&2
      return 1
    fi
  else
    echo "Unknown persisted marker existence state" >&2
    return 1
  fi
  return 0
}

abort_owned_merge() {
  if [ ! -f "$SYNC_MERGE_STARTED_STATE" ]; then
    if [ -e "$SYNC_MERGE_HEAD_PATH" ]; then
      echo "Refusing to abort a merge without this run's ownership state" >&2
      return 1
    fi
    return 0
  fi
  if ! sync_state_complete; then
    echo "Sync ownership state is incomplete; refusing abort or marker restore" >&2
    return 1
  fi
  if ! SAVED_ORIGINAL_HEAD_SHA=$(tr -d '\r\n' < "$SYNC_ORIGINAL_HEAD_STATE"); then
    echo "Unable to read the persisted original-HEAD state" >&2
    return 1
  fi
  if ! CURRENT_HEAD_SHA=$(git rev-parse --verify HEAD); then
    echo "Unable to verify current HEAD ownership before abort" >&2
    return 1
  fi
  if [ "$CURRENT_HEAD_SHA" != "$SAVED_ORIGINAL_HEAD_SHA" ]; then
    echo "Refusing to restore or abort a foreign original HEAD" >&2
    return 1
  fi
  if [ -e "$SYNC_MERGE_HEAD_PATH" ]; then
    if ! MERGE_HEAD_LINE_COUNT=$(awk 'NF { count += 1 } END { print count + 0 }' "$SYNC_MERGE_HEAD_PATH"); then
      echo "Unable to count MERGE_HEAD entries before abort" >&2
      return 1
    fi
    if [ "$MERGE_HEAD_LINE_COUNT" -ne 1 ]; then
      echo "Refusing to abort a merge with multiple MERGE_HEAD targets" >&2
      return 1
    fi
    if ! CURRENT_MERGE_HEAD_SHA=$(tr -d '\r\n' < "$SYNC_MERGE_HEAD_PATH"); then
      echo "Unable to read MERGE_HEAD ownership before abort" >&2
      return 1
    fi
    if ! EXPECTED_UPSTREAM_SHA=$(tr -d '\r\n' < "$SYNC_EXPECTED_STATE"); then
      echo "Unable to read the persisted target before abort" >&2
      return 1
    fi
    if [ "$CURRENT_MERGE_HEAD_SHA" != "$EXPECTED_UPSTREAM_SHA" ]; then
      echo "Refusing to abort a foreign MERGE_HEAD target" >&2
      return 1
    fi
    if ! CURRENT_ORIG_HEAD_SHA=$(git rev-parse --verify ORIG_HEAD); then
      echo "Unable to verify ORIG_HEAD ownership before abort" >&2
      return 1
    fi
    if [ "$CURRENT_ORIG_HEAD_SHA" != "$SAVED_ORIGINAL_HEAD_SHA" ]; then
      echo "Refusing to abort a merge with a foreign ORIG_HEAD" >&2
      return 1
    fi
    if ! git merge --abort; then
      echo "git merge --abort failed" >&2
      return 1
    fi
    if [ -e "$SYNC_MERGE_HEAD_PATH" ]; then
      echo "git merge --abort left MERGE_HEAD in place" >&2
      return 1
    fi
  else
    if CURRENT_ORIG_HEAD_SHA=$(git rev-parse --verify ORIG_HEAD 2>/dev/null); then
      if [ "$CURRENT_ORIG_HEAD_SHA" != "$SAVED_ORIGINAL_HEAD_SHA" ]; then
        echo "Refusing to restore marker with a foreign ORIG_HEAD" >&2
        return 1
      fi
    fi
  fi
  if ! CURRENT_HEAD_SHA=$(git rev-parse --verify HEAD); then
    echo "Unable to verify HEAD after merge abort/recovery" >&2
    return 1
  fi
  if [ "$CURRENT_HEAD_SHA" != "$SAVED_ORIGINAL_HEAD_SHA" ]; then
    echo "HEAD changed during merge abort/recovery; preserving the scene" >&2
    return 1
  fi
  if ! restore_marker_from_state; then
    echo "Unable to safely restore the pre-merge marker" >&2
    return 1
  fi
}

fail_sync() {
  FAILURE_STATUS="${1:-1}"
  if abort_owned_merge; then
    if ! cleanup_sync_state; then
      FAILURE_STATUS=1
    fi
  else
    echo "Ownership was not proven; preserving merge scene and Git-internal state" >&2
    FAILURE_STATUS=1
  fi
  exit "$FAILURE_STATUS"
}

fail_without_abort() {
  FAILURE_STATUS="${1:-1}"
  if ! cleanup_sync_state; then
    FAILURE_STATUS=1
  fi
  exit "$FAILURE_STATUS"
}

if [ ! -f "$SYNC_EXPECTED_STATE" ]; then
  echo "Expected upstream state is missing; rerun Step 1" >&2
  fail_sync 1
fi
if ! STORED_TARGET_SHA=$(tr -d '\r\n' < "$SYNC_EXPECTED_STATE"); then
  echo "Unable to read the Git-internal expected upstream state" >&2
  fail_sync 1
fi
if ! printf '%s\n' "$STORED_TARGET_SHA" | grep -Eq '^[0-9a-f]{40}$'; then
  echo "Stored expected upstream SHA is invalid" >&2
  fail_sync 1
fi
if [ "${GITHUB_ACTIONS:-}" = "true" ]; then
  if [ -z "${EXPECTED_UPSTREAM_SHA:-}" ] || [ "$EXPECTED_UPSTREAM_SHA" != "$STORED_TARGET_SHA" ]; then
    echo "Actions EXPECTED_UPSTREAM_SHA is missing or differs from Git-internal state" >&2
    fail_sync 1
  fi
fi
EXPECTED_UPSTREAM_SHA="$STORED_TARGET_SHA"

if [ -e "$SYNC_ORIGINAL_HEAD_STATE" ]; then
  echo "A stale original-HEAD state file exists; refusing to overwrite it" >&2
  fail_without_abort 1
fi
if [ -e "$SYNC_MERGE_HEAD_PATH" ]; then
  echo "A merge already exists before this sync run; refusing to abort it" >&2
  fail_without_abort 1
fi
if ! ORIGINAL_HEAD_SHA=$(git rev-parse --verify HEAD); then
  echo "Unable to record the pre-merge HEAD" >&2
  fail_without_abort 1
fi
if ! printf '%s\n' "$ORIGINAL_HEAD_SHA" > "$SYNC_ORIGINAL_HEAD_STATE"; then
  echo "Unable to persist the pre-merge HEAD in Git-internal state" >&2
  fail_without_abort 1
fi
if git cat-file -e "$ORIGINAL_HEAD_SHA:.github/upstream-main.sha" 2>/dev/null; then
  if ! MARKER_MODE=$(git ls-tree "$ORIGINAL_HEAD_SHA" -- .github/upstream-main.sha | awk 'NF { print $1 }'); then
    echo "Unable to inspect the original upstream marker mode" >&2
    fail_without_abort 1
  fi
  if [ "$MARKER_MODE" != "100644" ]; then
    echo "The original upstream marker must be a regular file" >&2
    fail_without_abort 1
  fi
  if ! printf '%s\n' tracked > "$SYNC_MARKER_STATE"; then
    echo "Unable to persist the tracked marker state" >&2
    fail_without_abort 1
  fi
  if ! git show "$ORIGINAL_HEAD_SHA:.github/upstream-main.sha" > "$SYNC_MARKER_SNAPSHOT"; then
    echo "Unable to save the byte-level upstream marker snapshot" >&2
    fail_without_abort 1
  fi
else
  if ! printf '%s\n' absent > "$SYNC_MARKER_STATE"; then
    echo "Unable to persist the absent marker state" >&2
    fail_without_abort 1
  fi
  if [ -e .github/upstream-main.sha ] || [ -L .github/upstream-main.sha ]; then
    echo "HEAD does not track .github/upstream-main.sha, but the path already exists" >&2
    fail_without_abort 1
  fi
fi
if ! PRE_MERGE_STATUS=$(git status --porcelain=v1 --untracked-files=all); then
  echo "Unable to verify a clean worktree before starting the merge" >&2
  fail_without_abort 1
fi
if [ -n "$PRE_MERGE_STATUS" ]; then
  echo "Worktree must be clean before starting the sync-owned merge" >&2
  fail_without_abort 1
fi
if ! printf '%s\n' started > "$SYNC_MERGE_STARTED_STATE"; then
  echo "Unable to persist merge ownership state" >&2
  fail_without_abort 1
fi

if git merge --no-ff --no-commit "$EXPECTED_UPSTREAM_SHA"; then
  :
else
  MERGE_EXIT=$?
  echo "Merge failed; aborting this sync-owned merge." >&2
  fail_sync "$MERGE_EXIT"
fi

if [ ! -e "$SYNC_MERGE_HEAD_PATH" ]; then
  echo "git merge returned 0 without creating MERGE_HEAD; refusing already-up-to-date merge" >&2
  fail_sync 1
fi
if ! MERGE_HEAD_LINE_COUNT=$(awk 'NF { count += 1 } END { print count + 0 }' "$SYNC_MERGE_HEAD_PATH"); then
  echo "Unable to count MERGE_HEAD entries" >&2
  fail_sync 1
fi
if [ "$MERGE_HEAD_LINE_COUNT" -ne 1 ]; then
  echo "MERGE_HEAD must contain exactly one target" >&2
  fail_sync 1
fi
if ! MERGE_HEAD_SHA=$(tr -d '\r\n' < "$SYNC_MERGE_HEAD_PATH"); then
  echo "Unable to read MERGE_HEAD" >&2
  fail_sync 1
fi
if [ "$MERGE_HEAD_SHA" != "$EXPECTED_UPSTREAM_SHA" ]; then
  echo "MERGE_HEAD does not equal the fixed expected upstream SHA" >&2
  fail_sync 1
fi
if ! ORIG_HEAD_SHA=$(git rev-parse --verify ORIG_HEAD); then
  echo "Successful merge did not set ORIG_HEAD" >&2
  fail_sync 1
fi
if [ "$ORIG_HEAD_SHA" != "$ORIGINAL_HEAD_SHA" ]; then
  echo "ORIG_HEAD does not equal the recorded pre-merge HEAD" >&2
  fail_sync 1
fi

if [ -L .github/upstream-main.sha ]; then
  echo "The merged upstream marker must not be a symbolic link" >&2
  fail_sync 1
fi
if ! printf '%s\n' "$EXPECTED_UPSTREAM_SHA" > .github/upstream-main.sha; then
  echo "Unable to write the upstream marker after merge validation" >&2
  fail_sync 1
fi
if ! git add .github/upstream-main.sha; then
  echo "Unable to stage the upstream marker" >&2
  fail_sync 1
fi
```

**历史改写禁令（不可绕过）**：从 merge 开始到 merge commit 创建完成，禁止
reset、rebase、squash、cherry-pick、切换分支和清除 `MERGE_HEAD`。失败时唯一允许的
中止路径是 `git merge --abort`，中止后立即停止整个流程。

**如果合并失败（包括冲突）**，上面的程序会先执行 `git merge --abort`，然后立即
停止；不得继续适配、提交或修改 marker。向用户报告失败原因和具体冲突范围，由用户
决定下一步。merge 未提交前
`MERGE_HEAD` 必须始终等于 `"$EXPECTED_UPSTREAM_SHA"`。

---

### Step 3: 适配与检查

Step 2 成功才允许进入本步；失败（包括冲突）已经 abort 并停止，禁止手动解冲突后
继续本次运行。成功后，核心原则是保留 fork 的 uvx 适配，合入上游的新功能。
Python 文件按 hunk 和契约审查，不按整文件 allowlist 审查。保留下方清单中的 fork
标记和必要 import，并合入其他上游功能与安全修复；用对应的聚焦测试验证行为。

本步若需要启动独立 shell 代码块，不能引用前一代码块的普通变量。每个代码块都必须
用 `git rev-parse --git-path ppt-master-sync` 重新取得状态目录及其状态文件，再读取并
校验这些文件；Actions 中还要重新校验环境里的 `EXPECTED_UPSTREAM_SHA` 与持久化 target
相等。任何失败都必须走本次运行的 abort/cleanup 失败路径后退出。

**fork 修改文件清单**（这些文件含 fork 独有的 Windows/uvx 适配，上游更新时**保留 fork 适配标记、合入上游功能改动**，不得整文件回退）：

| 文件 | 关键适配标记 |
|------|-------------|
| `confirm_ui/server.py` | `PPT_MASTER_LAUNCH_TOKEN`（launch token 校验）、`normalized_project_key`（Windows casefold 路径比较） |
| `svg_editor/server.py` | `PPT_MASTER_LAUNCH_TOKEN`、`normalized_project_key` |
| `visual_review.py` | `normalized_project_key` |
| `server_common.py` | `normalized_project_key` 函数本身 |
| `config.py` | `projects_root()`（`PPT_MASTER_PROJECTS`） |
| `project_management/paths.py` | `projects_root()` 函数 |
| `project_manager.py` | `projects_root()` 接入 |
| `register_template.py` | `PPT_MASTER_TEMPLATES_DIR`（uvx 库根解析：env > cwd 检出 > wheel 内置） |

合并后必须逐文件 grep 验证适配标记仍在（见 Step 4e 门禁 4）。

| 冲突类型 | 解决策略 |
|----------|----------|
| `python3 scripts/xxx.py` vs `uvx ppt-master xxx` | 保留 uvx 格式 |
| `python3 skills/ppt-master/scripts/xxx.py` vs `uvx ppt-master xxx` | 保留 uvx 格式 |
| 上游新增文件中的 `python3` 命令 | 保持原样，Step 4 统一替换 |
| `AGENTS.md` / `CLAUDE.md` 命令参考 | 接受上游内容后，将 `python3` 替换为 `uvx` |
| `README.md` / `README_CN.md` | 接受上游内容后，在语言切换行与赞助商 `<details>` 块之间**重新插入 fork 声明块**（`> **Fork notice** / `> **Fork 声明**），声明赞助商与捐赠信息属于原作者、与本 fork 无关；声明块含 fork 的 `uvx` 安装说明（`uvx ppt-master <command>`），并在 `### 3. Set Up` / `### 3. 配置项目` 标题后重新插入 fork 提示行（`> **Fork users**` / `> **Fork 用户**） |
| `docs/faq.md` / `docs/zh/faq.md` | 接受上游内容后，更新方式表格首行重新插入 `uvx`（PyPI）行 |
| `docs/windows-installation.md` / `docs/zh/windows-installation.md` | 接受上游内容后，标题下方重新插入 fork `uvx` 注记块（`> **Fork users**` / `> **Fork 用户**） |
| `docs/roadmap.md` / `docs/zh/roadmap.md` | 接受上游内容后，uv 段落末尾重新插入 fork 注记（`> **Fork note**` / `> **Fork 注记**） |
| `CONTRIBUTING.md` | 接受上游内容后，Setup 代码块后重新插入 fork 注记（`> **Fork note**`） |
| `pyproject.toml` 依赖变更 | 手动审查，同步到两个 `pyproject.toml` |
| `update_repo.py` | 保留 fork 的 uv 功能（`ensure_uv_available`、`uv sync`、`--skip-deps`），合入上游新功能 |
| `generate_examples_index.py` | 确保内部字符串已替换为 `uvx` |
| `attribution_guard.py` 冲突 | **保留 fork 的 `_SKILL_GATE_MARKER`（uvx 形式）**，合入上游其他改动；合并后必须运行 guard 验证（见 Step 4e 门禁 3） |

**冲突文件速查：**

| 文件 | 策略 |
|------|------|
| `*.md` workflow/reference | 接受上游内容，将所有 `python3` → `uvx` |
| `cli.py` (根 & skills) | 无冲突（上游无此文件）；检查新脚本映射 |
| `pyproject.toml` | 手动同步依赖；保留 version/tool.uv/tool.setuptools 段 |
| `skills/ppt-master/scripts/*.py` | 按 hunk 审查：保留 fork 标记和 import，合入上游功能与安全修复；`attribution_guard.py` 的 `_SKILL_GATE_MARKER` 必须保持 `uvx ppt-master attribution-guard`，`register_template.py` 的 `PPT_MASTER_TEMPLATES_DIR` 库根解析必须保留 |

---

### Step 4: 适配 uvx 命令

#### 4a. 扫描所有 `python3` / `uv run` 残留

扫描**全仓库**所有 `.md` 文件（排除豁免目录 `.opencode/`、`docs/superpowers/`、`docs/zh/upstream-sync.md`）：

```bash
# 扫描 python3 命令残留（全仓库 .md 文件）
rg -g '*.md' -e 'python3 (scripts/|skills/)' -e 'python3 \$\{SKILL_DIR\}/scripts/' . \
  --glob '!.opencode/**' \
  --glob '!docs/superpowers/**' \
  --glob '!docs/zh/upstream-sync.md'

# 扫描 uv run 残留
rg -g '*.md' 'uv run skills/ppt-master/scripts/' . \
  --glob '!.opencode/**' \
  --glob '!docs/superpowers/**'
```

**重要：** 扫描范围必须覆盖全仓库，不得遗漏任何目录。上游随时可能新增文件到任意位置。记录所有匹配的文件和行。

---

#### 4b. 补全 cli.py 映射

运行检测脚本：

```bash
python skills/ppt-master/scripts/check_cli_sync.py
```

对于输出的每个缺失脚本：
1. 按 **kebab-case 命名**（下划线 `_` → 连字符 `-`，子目录只取文件名）
2. 同时添加到 **两个** `cli.py`（根目录 + `skills/ppt-master/`）：
   - `COMMANDS` 字典中按字母序插入
   - `COMMAND_DESCRIPTIONS` 字典中添加一行中文描述
3. 重新运行检测脚本确认同步

**kebab-case 命名示例：**

| 脚本文件 | 命令名 |
|----------|--------|
| `native_enhance_pptx.py` | `native-enhance-pptx` |
| `confirm_ui/server.py` | `confirm-ui` |
| `extract_svg_assets.py` | `extract-svg-assets` |
| `icon_sync.py` | `icon-sync` |
| `beautify_inventory.py` | `beautify-inventory` |
| `source_to_md/pdf_to_md.py` | `pdf-to-md` |
| `svg_editor/server.py` | `svg-editor` |

---

#### 4c. 批量替换 — 使用 Python 脚本自动化

编写并执行以下 Python 脚本，自动解析 cli.py 的 `COMMANDS` 映射并将所有 `python3` / `uv run` 调用替换为 `uvx ppt-master <command>`：

```python
import re, ast, pathlib

# 解析 cli.py 的 COMMANDS 字典（AST 方式，不执行代码避免导入失败）
tree = ast.parse(pathlib.Path('cli.py').read_text(encoding='utf-8'))
commands = {}
for node in ast.walk(tree):
    if isinstance(node, ast.Dict):
        keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
        vals = [v.value for v in node.values if isinstance(v, ast.Constant)]
        if 'project' in keys and 'project_manager.py' in vals:
            commands = dict(zip(keys, vals))
            break

# 构建脚本路径 → uvx 命令名 的映射（用完整相对路径作 key，避免
# confirm_ui/server.py 与 svg_editor/server.py 等 basename 冲突）
script_to_cmd = {}
for cmd_name, script_rel in commands.items():
    script_name = script_rel.rsplit('/', 1)[-1]
    if '/' in script_rel:
        script_to_cmd[script_rel] = cmd_name
    else:
        script_to_cmd[script_name] = cmd_name

# 需要扫描的豁免目录（这些目录下的文件不参与替换）
EXCLUDE_DIRS = ['.opencode', 'docs/superpowers', 'docs/zh']

# 收集全仓库所有 .md 文件（排除豁免目录）
files = []
for f in pathlib.Path('.').rglob('*.md'):
    if any(str(f).startswith(d + '/') or str(f).startswith(d + '\\') for d in EXCLUDE_DIRS):
        continue
    files.append(f)

# 统计
total_replacements = 0
for filepath in sorted(set(files)):
    content = filepath.read_text(encoding='utf-8')
    original = content
    for script_name, cmd_name in sorted(script_to_cmd.items()):
        # python3 skills/ppt-master/scripts/xxx.py → uvx ppt-master xxx
        content = re.sub(
            rf'python3\s+skills/ppt-master/scripts/(\S*/)?{re.escape(script_name)}',
            f'uvx ppt-master {cmd_name}', content
        )
        # python3 scripts/xxx.py → uvx ppt-master xxx
        content = re.sub(
            rf'(?<!\w)python3\s+scripts/(\S*/)?{re.escape(script_name)}',
            f'uvx ppt-master {cmd_name}', content
        )
        # python3 ${SKILL_DIR}/scripts/xxx.py → uvx ppt-master xxx
        content = re.sub(
            rf'python3\s+\$\{{SKILL_DIR\}}/scripts/(\S*/)?{re.escape(script_name)}',
            f'uvx ppt-master {cmd_name}', content
        )
        # uv run skills/ppt-master/scripts/xxx.py → uvx ppt-master xxx
        content = re.sub(
            rf'uv\s+run\s+skills/ppt-master/scripts/(\S*/)?{re.escape(script_name)}',
            f'uvx ppt-master {cmd_name}', content
        )
    if content != original:
        filepath.write_text(content, encoding='utf-8')
        total_replacements += 1
        print(f'Updated: {filepath}')

print(f'\nDone: {total_replacements} files updated.')
```

**重要：** 脚本使用 AST 解析 `cli.py` 而非 `exec()` 执行，避免顶层导入失败。所有替换使用正则精确匹配。豁免目录（`.opencode/`、`docs/superpowers/`、`docs/zh/`）不参与替换。

---

#### 4d. 全仓库验证

```bash
# 确认全仓库 .md 文件无 python3 残留（排除豁免目录）
rg -g '*.md' -e 'python3 (scripts/|skills/)' -e 'python3 \$\{SKILL_DIR\}/scripts/' . \
  --glob '!.opencode/**' \
  --glob '!docs/superpowers/**' \
  --glob '!docs/zh/upstream-sync.md'
```

如果仍有输出，**必须回到 Step 4c 重新处理**，直到无残留为止。

```bash
# 确认 cli.py 映射完整 + 双文件同步（一次调用检查两者）
python skills/ppt-master/scripts/check_cli_sync.py
```

`check_cli_sync.py` 一次运行会同时检查映射完整性和双 cli.py 同步。如果报错，回到 Step 4b 补全映射。

---

#### 4e. 提交前门禁（必须通过）

在 `git commit` 之前，**必须**确认以下七项全部通过：

1. **全仓库扫描零残留**：重复 Step 4d 的 `rg` 命令，确认输出为空
2. **cli.py 同步**：`python skills/ppt-master/scripts/check_cli_sync.py` 确认 OK
3. **Skill 完整性 guard**：运行 `python skills/ppt-master/scripts/attribution_guard.py`，**必须 exit 0**（无输出）。如果失败（exit 78），说明上游的 attribution/完整性约束与 fork 的 uvx 适配冲突，必须修复后再验证：
   - 检查 `skills/ppt-master/SKILL.md` 是否包含且仅包含一次 `uvx ppt-master attribution-guard`（marker 被上游恢复为 `python3` 时,Step 4c 的批量替换会处理,但如果 guard 换用了新 marker 字符串,需同步更新 `attribution_guard.py` 的 `_SKILL_GATE_MARKER` 与 SKILL.md）
   - 检查 `skills/ppt-master/LICENSE`、`SPONSORS.md`、`SPONSORS_CN.md` 是否存在
   - 检查 `MANIFEST.in`（根 与 `skills/ppt-master/`）是否仍包含 `SKILL.md`/`LICENSE`/`SPONSORS.md`/`SPONSORS_CN.md`（上游若调整文件布局可能导致 wheel 打包缺失）
   - 检查上游是否在 `attribution_guard.py` 中新增了 `_REQUIRED_GATE_FILES`/`_REQUIRED_ATTRIBUTION_FILES` 条目,对应文件必须存在
4. **fork 适配完整性**：对「fork 修改文件清单」的每个文件 grep 验证其关键适配标记仍在（`PPT_MASTER_LAUNCH_TOKEN`、`normalized_project_key`、`projects_root`、`PPT_MASTER_TEMPLATES_DIR`），任一缺失必须修复后再提交
5. **fork 声明块**：`README.md` 与 `README_CN.md` 在语言切换行之后必须保留 fork 声明块（`Fork notice` / `Fork 声明`，含 `uvx ppt-master` 安装说明），`PYPI_README.md` 顶部引用块必须保留 fork 维护者与赞助归属声明；`docs/faq.md` / `docs/zh/faq.md` 更新表格首行必须保留 `uvx`（PyPI）行；`docs/windows-installation.md` / `docs/zh/windows-installation.md` 标题下方必须保留 fork `uvx` 注记块；`docs/roadmap.md` / `docs/zh/roadmap.md` uv 段落末尾必须保留 fork 注记；`CONTRIBUTING.md` Setup 后必须保留 fork 注记；上游合并覆盖后必须恢复
6. **fork 修改文件可运行性**：对「fork 修改文件清单」的每个 `.py` 文件运行语法与未定义名检查，两者必须全部通过：

   ```bash
   # 语法级检查（8 个 fork 修改文件）
   python -m py_compile \
     skills/ppt-master/scripts/confirm_ui/server.py \
     skills/ppt-master/scripts/svg_editor/server.py \
     skills/ppt-master/scripts/visual_review.py \
     skills/ppt-master/scripts/server_common.py \
     skills/ppt-master/scripts/config.py \
     skills/ppt-master/scripts/project_management/paths.py \
     skills/ppt-master/scripts/project_manager.py \
     skills/ppt-master/scripts/register_template.py

   # 未定义名检查（F821，捕获"marker 在但 import 缺失"类错误，如 uuid 未导入）
   uvx ruff check --select F821 \
     skills/ppt-master/scripts/confirm_ui/server.py \
     skills/ppt-master/scripts/svg_editor/server.py \
     skills/ppt-master/scripts/visual_review.py \
     skills/ppt-master/scripts/server_common.py \
     skills/ppt-master/scripts/config.py \
     skills/ppt-master/scripts/project_management/paths.py \
     skills/ppt-master/scripts/project_manager.py \
     skills/ppt-master/scripts/register_template.py
   ```

   门禁 4 的 grep 只验证适配标记存在，无法发现"标记在但代码坏"（如 `launch_token = uuid.uuid4().hex` 缺 `import uuid`）。F821 静态分析不执行导入，可捕获此类错误。任一失败必须修复后再提交。
7. **merge-state 门禁**：在 `git commit` 创建 merge commit 之前验证 merge 状态与目标文件仍然成立：

   ```bash
    # This is an independent shell: acquire every path and reload every field from the state lock.
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

    cleanup_sync_state() {
      if [ ! -d "$SYNC_STATE_DIR" ]; then
        echo "Git-internal sync state directory is missing" >&2
        return 1
      fi
      if ! rm -rf -- "$SYNC_STATE_DIR"; then
        echo "Unable to clean the Git-internal sync state directory" >&2
        return 1
      fi
    }

    sync_state_complete() {
      if [ ! -d "$SYNC_STATE_DIR" ] ||
         [ ! -f "$SYNC_EXPECTED_STATE" ] ||
         [ ! -f "$SYNC_ORIGINAL_HEAD_STATE" ] ||
         [ ! -f "$SYNC_MARKER_STATE" ] ||
         [ ! -f "$SYNC_MERGE_STARTED_STATE" ]; then
        return 1
      fi
      if ! MARKER_ORIGINAL_STATE=$(tr -d '\r\n' < "$SYNC_MARKER_STATE"); then
        return 1
      fi
      if [ "$MARKER_ORIGINAL_STATE" = "tracked" ] && [ ! -f "$SYNC_MARKER_SNAPSHOT" ]; then
        return 1
      fi
      if [ "$MARKER_ORIGINAL_STATE" != "tracked" ] &&
         [ "$MARKER_ORIGINAL_STATE" != "absent" ]; then
        return 1
      fi
      return 0
    }

    restore_marker_from_state() {
      if ! ORIGINAL_HEAD_SHA=$(tr -d '\r\n' < "$SYNC_ORIGINAL_HEAD_STATE"); then
        echo "Unable to read the persisted original HEAD for marker restore" >&2
        return 1
      fi
      if ! MARKER_ORIGINAL_STATE=$(tr -d '\r\n' < "$SYNC_MARKER_STATE"); then
        echo "Unable to read the persisted marker existence state" >&2
        return 1
      fi
      if [ "$MARKER_ORIGINAL_STATE" = "tracked" ]; then
        if ! git restore --source="$ORIGINAL_HEAD_SHA" --staged --worktree -- .github/upstream-main.sha; then
          echo "Unable to restore the tracked upstream marker" >&2
          return 1
        fi
        if ! SNAPSHOT_SHA=$(git hash-object -- "$SYNC_MARKER_SNAPSHOT"); then
          echo "Unable to hash the saved upstream marker snapshot" >&2
          return 1
        fi
        if ! WORKTREE_MARKER_SHA=$(git hash-object -- .github/upstream-main.sha); then
          echo "Unable to hash the restored upstream marker" >&2
          return 1
        fi
        if ! INDEX_MARKER_SHA=$(git rev-parse :".github/upstream-main.sha"); then
          echo "Unable to hash the restored index marker" >&2
          return 1
        fi
        if [ "$WORKTREE_MARKER_SHA" != "$SNAPSHOT_SHA" ] ||
           [ "$INDEX_MARKER_SHA" != "$SNAPSHOT_SHA" ]; then
          echo "Restored marker bytes do not match the pre-merge snapshot" >&2
          return 1
        fi
      elif [ "$MARKER_ORIGINAL_STATE" = "absent" ]; then
        if ! git rm --cached --ignore-unmatch -- .github/upstream-main.sha; then
          echo "Unable to remove the newly tracked upstream marker" >&2
          return 1
        fi
        if { [ -e .github/upstream-main.sha ] || [ -L .github/upstream-main.sha ]; } &&
           ! rm -f -- .github/upstream-main.sha; then
          echo "Unable to remove the newly created upstream marker" >&2
          return 1
        fi
        if [ -e .github/upstream-main.sha ] || [ -L .github/upstream-main.sha ] ||
           git ls-files --error-unmatch -- .github/upstream-main.sha >/dev/null 2>&1; then
          echo "The absent upstream marker was not fully restored" >&2
          return 1
        fi
      else
        echo "Unknown persisted marker existence state" >&2
        return 1
      fi
      return 0
    }

    abort_owned_merge() {
      if [ ! -f "$SYNC_MERGE_STARTED_STATE" ]; then
        if [ -e "$SYNC_MERGE_HEAD_PATH" ]; then
          echo "Refusing to abort a merge without this run's ownership state" >&2
          return 1
        fi
        return 0
      fi
      if ! sync_state_complete; then
        echo "Sync ownership state is incomplete; refusing abort or marker restore" >&2
        return 1
      fi
      if ! SAVED_ORIGINAL_HEAD_SHA=$(tr -d '\r\n' < "$SYNC_ORIGINAL_HEAD_STATE"); then
        echo "Unable to read the persisted original-HEAD state" >&2
        return 1
      fi
      if ! CURRENT_HEAD_SHA=$(git rev-parse --verify HEAD); then
        echo "Unable to verify current HEAD ownership before abort" >&2
        return 1
      fi
      if [ "$CURRENT_HEAD_SHA" != "$SAVED_ORIGINAL_HEAD_SHA" ]; then
        echo "Refusing to restore or abort a foreign original HEAD" >&2
        return 1
      fi
      if [ -e "$SYNC_MERGE_HEAD_PATH" ]; then
        if ! MERGE_HEAD_LINE_COUNT=$(awk 'NF { count += 1 } END { print count + 0 }' "$SYNC_MERGE_HEAD_PATH"); then
          echo "Unable to count MERGE_HEAD entries before abort" >&2
          return 1
        fi
        if [ "$MERGE_HEAD_LINE_COUNT" -ne 1 ]; then
          echo "Refusing to abort a merge with multiple MERGE_HEAD targets" >&2
          return 1
        fi
        if ! CURRENT_MERGE_HEAD_SHA=$(tr -d '\r\n' < "$SYNC_MERGE_HEAD_PATH"); then
          echo "Unable to read MERGE_HEAD ownership before abort" >&2
          return 1
        fi
        if ! EXPECTED_UPSTREAM_SHA=$(tr -d '\r\n' < "$SYNC_EXPECTED_STATE"); then
          echo "Unable to read the persisted target before abort" >&2
          return 1
        fi
        if [ "$CURRENT_MERGE_HEAD_SHA" != "$EXPECTED_UPSTREAM_SHA" ]; then
          echo "Refusing to abort a foreign MERGE_HEAD target" >&2
          return 1
        fi
        if ! CURRENT_ORIG_HEAD_SHA=$(git rev-parse --verify ORIG_HEAD); then
          echo "Unable to verify ORIG_HEAD ownership before abort" >&2
          return 1
        fi
        if [ "$CURRENT_ORIG_HEAD_SHA" != "$SAVED_ORIGINAL_HEAD_SHA" ]; then
          echo "Refusing to abort a merge with a foreign ORIG_HEAD" >&2
          return 1
        fi
        if ! git merge --abort; then
          echo "git merge --abort failed" >&2
          return 1
        fi
        if [ -e "$SYNC_MERGE_HEAD_PATH" ]; then
          echo "git merge --abort left MERGE_HEAD in place" >&2
          return 1
        fi
      else
        if CURRENT_ORIG_HEAD_SHA=$(git rev-parse --verify ORIG_HEAD 2>/dev/null); then
          if [ "$CURRENT_ORIG_HEAD_SHA" != "$SAVED_ORIGINAL_HEAD_SHA" ]; then
            echo "Refusing to restore marker with a foreign ORIG_HEAD" >&2
            return 1
          fi
        fi
      fi
      if ! CURRENT_HEAD_SHA=$(git rev-parse --verify HEAD); then
        echo "Unable to verify HEAD after merge abort/recovery" >&2
        return 1
      fi
      if [ "$CURRENT_HEAD_SHA" != "$SAVED_ORIGINAL_HEAD_SHA" ]; then
        echo "HEAD changed during merge abort/recovery; preserving the scene" >&2
        return 1
      fi
      if ! restore_marker_from_state; then
        echo "Unable to safely restore the pre-merge marker" >&2
        return 1
      fi
    }

    fail_sync() {
      FAILURE_STATUS="${1:-1}"
      if abort_owned_merge; then
        if ! cleanup_sync_state; then
          FAILURE_STATUS=1
        fi
      else
        echo "Ownership was not proven; preserving merge scene and Git-internal state" >&2
        FAILURE_STATUS=1
      fi
      exit "$FAILURE_STATUS"
    }

    if ! sync_state_complete; then
      echo "Sync ownership state is incomplete; refusing to commit" >&2
      fail_sync 1
    fi
    if ! STORED_TARGET_SHA=$(tr -d '\r\n' < "$SYNC_EXPECTED_STATE"); then
      echo "Unable to read the Git-internal expected upstream state" >&2
      fail_sync 1
    fi
    if ! printf '%s\n' "$STORED_TARGET_SHA" | grep -Eq '^[0-9a-f]{40}$'; then
      echo "Stored expected upstream SHA is invalid" >&2
      fail_sync 1
    fi
    if [ "${GITHUB_ACTIONS:-}" = "true" ]; then
      if [ -z "${EXPECTED_UPSTREAM_SHA:-}" ] || [ "$EXPECTED_UPSTREAM_SHA" != "$STORED_TARGET_SHA" ]; then
        echo "Actions EXPECTED_UPSTREAM_SHA is missing or differs from Git-internal state" >&2
        fail_sync 1
      fi
    fi
    EXPECTED_UPSTREAM_SHA="$STORED_TARGET_SHA"
    if ! ORIGINAL_HEAD_SHA=$(tr -d '\r\n' < "$SYNC_ORIGINAL_HEAD_STATE"); then
      echo "Unable to read the Git-internal original-HEAD state" >&2
      fail_sync 1
    fi
    if [ ! -e "$SYNC_MERGE_HEAD_PATH" ]; then
      echo "MERGE_HEAD is missing" >&2
      fail_sync 1
    fi
    if ! MERGE_HEAD_LINE_COUNT=$(awk 'NF { count += 1 } END { print count + 0 }' "$SYNC_MERGE_HEAD_PATH"); then
      echo "Unable to count MERGE_HEAD entries" >&2
      fail_sync 1
    fi
    if [ "$MERGE_HEAD_LINE_COUNT" -ne 1 ]; then
      echo "MERGE_HEAD must contain exactly one target" >&2
      fail_sync 1
    fi
    if ! MERGE_HEAD_SHA=$(tr -d '\r\n' < "$SYNC_MERGE_HEAD_PATH"); then
      echo "Unable to read MERGE_HEAD" >&2
      fail_sync 1
    fi
    if [ "$MERGE_HEAD_SHA" != "$EXPECTED_UPSTREAM_SHA" ]; then
      echo "MERGE_HEAD does not equal EXPECTED_UPSTREAM_SHA" >&2
      fail_sync 1
    fi
    if ! ORIG_HEAD_SHA=$(git rev-parse --verify ORIG_HEAD); then
      echo "ORIG_HEAD is missing" >&2
      fail_sync 1
    fi
    if [ "$ORIG_HEAD_SHA" != "$ORIGINAL_HEAD_SHA" ]; then
      echo "ORIG_HEAD does not equal the saved original HEAD" >&2
      fail_sync 1
    fi
    if [ -L .github/upstream-main.sha ] || [ ! -e .github/upstream-main.sha ]; then
      echo "The upstream marker is missing or is a symbolic link" >&2
      fail_sync 1
    fi
    if ! WORKTREE_TARGET_SHA=$(tr -d '\r\n' < .github/upstream-main.sha); then
      echo "Unable to read .github/upstream-main.sha" >&2
      fail_sync 1
    fi
    if ! INDEX_TARGET_SHA=$(git show :".github/upstream-main.sha" | tr -d '\r\n'); then
      echo "Unable to read the staged upstream marker" >&2
      fail_sync 1
    fi
    if [ "$WORKTREE_TARGET_SHA" != "$EXPECTED_UPSTREAM_SHA" ] ||
       [ "$INDEX_TARGET_SHA" != "$EXPECTED_UPSTREAM_SHA" ]; then
      echo "The upstream marker is not staged with EXPECTED_UPSTREAM_SHA" >&2
      fail_sync 1
    fi
   ```

   任一命令非零（包括状态文件、`ORIG_HEAD`、`MERGE_HEAD` 或 marker 不一致）都必须
   先 abort 本次拥有的 merge，再清理 `.git` 临时状态并停止；成功通过本门禁后不得清理
   状态，Step 6 仍需从文件重新读取。

**七项有任何一项不通过，禁止提交。** 回到对应步骤修复后重新验证。

### Step 5: 依赖同步

无论上游 requirements.txt 是否有变更，都运行验证确保三份清单一致：

```bash
uv lock && cd skills/ppt-master && uv lock && cd ..
python skills/ppt-master/scripts/check_deps_sync.py
```

如果上游 `requirements.txt` 新增/删除了依赖，手动同步到两个 `pyproject.toml` 的 `[project] dependencies` 后重新运行以上命令。

**pyyaml 依赖保护**：`pyyaml>=6.0` 是 `register-template`（design_spec.md YAML frontmatter 解析）所需依赖。上游已修复 Issue #269 并在 `skills/ppt-master/requirements.txt` 顶部声明 `PyYAML>=6.0`，因此该依赖不再是 fork 独有。合并上游依赖变更时**必须保留 pyyaml 且只保留一份**（上游条目在文件顶部，fork 旧条目在文件底部——若合并后出现两份，删除底部 fork 旧条目，保留上游顶部条目）；`check_deps_sync.py` 只校验三份清单互相一致，不校验上游，因此合并后需人工确认 pyyaml 仍在三份清单中且无重复。

---

### Step 6: 提交、打版本号、推送

**提交前确认 Step 4e 门禁已通过（七项全部通过，含 merge-state 门禁）。**

```bash
# This is an independent shell: acquire the state lock paths and reload every field from .git.
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

cleanup_sync_state() {
  if [ ! -d "$SYNC_STATE_DIR" ]; then
    echo "Git-internal sync state directory is missing" >&2
    return 1
  fi
  if ! rm -rf -- "$SYNC_STATE_DIR"; then
    echo "Unable to clean the Git-internal sync state directory" >&2
    return 1
  fi
}

sync_state_complete() {
  if [ ! -d "$SYNC_STATE_DIR" ] ||
     [ ! -f "$SYNC_EXPECTED_STATE" ] ||
     [ ! -f "$SYNC_ORIGINAL_HEAD_STATE" ] ||
     [ ! -f "$SYNC_MARKER_STATE" ] ||
     [ ! -f "$SYNC_MERGE_STARTED_STATE" ]; then
    return 1
  fi
  if ! MARKER_ORIGINAL_STATE=$(tr -d '\r\n' < "$SYNC_MARKER_STATE"); then
    return 1
  fi
  if [ "$MARKER_ORIGINAL_STATE" = "tracked" ] && [ ! -f "$SYNC_MARKER_SNAPSHOT" ]; then
    return 1
  fi
  if [ "$MARKER_ORIGINAL_STATE" != "tracked" ] &&
     [ "$MARKER_ORIGINAL_STATE" != "absent" ]; then
    return 1
  fi
  return 0
}

restore_marker_from_state() {
  if ! ORIGINAL_HEAD_SHA=$(tr -d '\r\n' < "$SYNC_ORIGINAL_HEAD_STATE"); then
    echo "Unable to read the persisted original HEAD for marker restore" >&2
    return 1
  fi
  if ! MARKER_ORIGINAL_STATE=$(tr -d '\r\n' < "$SYNC_MARKER_STATE"); then
    echo "Unable to read the persisted marker existence state" >&2
    return 1
  fi
  if [ "$MARKER_ORIGINAL_STATE" = "tracked" ]; then
    if ! git restore --source="$ORIGINAL_HEAD_SHA" --staged --worktree -- .github/upstream-main.sha; then
      echo "Unable to restore the tracked upstream marker" >&2
      return 1
    fi
    if ! SNAPSHOT_SHA=$(git hash-object -- "$SYNC_MARKER_SNAPSHOT"); then
      echo "Unable to hash the saved upstream marker snapshot" >&2
      return 1
    fi
    if ! WORKTREE_MARKER_SHA=$(git hash-object -- .github/upstream-main.sha); then
      echo "Unable to hash the restored upstream marker" >&2
      return 1
    fi
    if ! INDEX_MARKER_SHA=$(git rev-parse :".github/upstream-main.sha"); then
      echo "Unable to hash the restored index marker" >&2
      return 1
    fi
    if [ "$WORKTREE_MARKER_SHA" != "$SNAPSHOT_SHA" ] ||
       [ "$INDEX_MARKER_SHA" != "$SNAPSHOT_SHA" ]; then
      echo "Restored marker bytes do not match the pre-merge snapshot" >&2
      return 1
    fi
  elif [ "$MARKER_ORIGINAL_STATE" = "absent" ]; then
    if ! git rm --cached --ignore-unmatch -- .github/upstream-main.sha; then
      echo "Unable to remove the newly tracked upstream marker" >&2
      return 1
    fi
    if { [ -e .github/upstream-main.sha ] || [ -L .github/upstream-main.sha ]; } &&
       ! rm -f -- .github/upstream-main.sha; then
      echo "Unable to remove the newly created upstream marker" >&2
      return 1
    fi
    if [ -e .github/upstream-main.sha ] || [ -L .github/upstream-main.sha ] ||
       git ls-files --error-unmatch -- .github/upstream-main.sha >/dev/null 2>&1; then
      echo "The absent upstream marker was not fully restored" >&2
      return 1
    fi
  else
    echo "Unknown persisted marker existence state" >&2
    return 1
  fi
  return 0
}

abort_owned_merge() {
  if [ ! -f "$SYNC_MERGE_STARTED_STATE" ]; then
    if [ -e "$SYNC_MERGE_HEAD_PATH" ]; then
      echo "Refusing to abort a merge without this run's ownership state" >&2
      return 1
    fi
    return 0
  fi
  if ! sync_state_complete; then
    echo "Sync ownership state is incomplete; refusing abort or marker restore" >&2
    return 1
  fi
  if ! SAVED_ORIGINAL_HEAD_SHA=$(tr -d '\r\n' < "$SYNC_ORIGINAL_HEAD_STATE"); then
    echo "Unable to read the persisted original-HEAD state" >&2
    return 1
  fi
  if ! CURRENT_HEAD_SHA=$(git rev-parse --verify HEAD); then
    echo "Unable to verify current HEAD ownership before abort" >&2
    return 1
  fi
  if [ "$CURRENT_HEAD_SHA" != "$SAVED_ORIGINAL_HEAD_SHA" ]; then
    echo "Refusing to restore or abort a foreign original HEAD" >&2
    return 1
  fi
  if [ -e "$SYNC_MERGE_HEAD_PATH" ]; then
    if ! MERGE_HEAD_LINE_COUNT=$(awk 'NF { count += 1 } END { print count + 0 }' "$SYNC_MERGE_HEAD_PATH"); then
      echo "Unable to count MERGE_HEAD entries before abort" >&2
      return 1
    fi
    if [ "$MERGE_HEAD_LINE_COUNT" -ne 1 ]; then
      echo "Refusing to abort a merge with multiple MERGE_HEAD targets" >&2
      return 1
    fi
    if ! CURRENT_MERGE_HEAD_SHA=$(tr -d '\r\n' < "$SYNC_MERGE_HEAD_PATH"); then
      echo "Unable to read MERGE_HEAD ownership before abort" >&2
      return 1
    fi
    if ! EXPECTED_UPSTREAM_SHA=$(tr -d '\r\n' < "$SYNC_EXPECTED_STATE"); then
      echo "Unable to read the persisted target before abort" >&2
      return 1
    fi
    if [ "$CURRENT_MERGE_HEAD_SHA" != "$EXPECTED_UPSTREAM_SHA" ]; then
      echo "Refusing to abort a foreign MERGE_HEAD target" >&2
      return 1
    fi
    if ! CURRENT_ORIG_HEAD_SHA=$(git rev-parse --verify ORIG_HEAD); then
      echo "Unable to verify ORIG_HEAD ownership before abort" >&2
      return 1
    fi
    if [ "$CURRENT_ORIG_HEAD_SHA" != "$SAVED_ORIGINAL_HEAD_SHA" ]; then
      echo "Refusing to abort a merge with a foreign ORIG_HEAD" >&2
      return 1
    fi
    if ! git merge --abort; then
      echo "git merge --abort failed" >&2
      return 1
    fi
    if [ -e "$SYNC_MERGE_HEAD_PATH" ]; then
      echo "git merge --abort left MERGE_HEAD in place" >&2
      return 1
    fi
  else
    if CURRENT_ORIG_HEAD_SHA=$(git rev-parse --verify ORIG_HEAD 2>/dev/null); then
      if [ "$CURRENT_ORIG_HEAD_SHA" != "$SAVED_ORIGINAL_HEAD_SHA" ]; then
        echo "Refusing to restore marker with a foreign ORIG_HEAD" >&2
        return 1
      fi
    fi
  fi
  if ! CURRENT_HEAD_SHA=$(git rev-parse --verify HEAD); then
    echo "Unable to verify HEAD after merge abort/recovery" >&2
    return 1
  fi
  if [ "$CURRENT_HEAD_SHA" != "$SAVED_ORIGINAL_HEAD_SHA" ]; then
    echo "HEAD changed during merge abort/recovery; preserving the scene" >&2
    return 1
  fi
  if ! restore_marker_from_state; then
    echo "Unable to safely restore the pre-merge marker" >&2
    return 1
  fi
}

fail_before_commit() {
  FAILURE_STATUS="${1:-1}"
  if abort_owned_merge; then
    if ! cleanup_sync_state; then
      FAILURE_STATUS=1
    fi
  else
    echo "Ownership was not proven; preserving merge scene and Git-internal state" >&2
    FAILURE_STATUS=1
  fi
  exit "$FAILURE_STATUS"
}

if ! sync_state_complete; then
  echo "Sync ownership state is incomplete; refusing to commit" >&2
  fail_before_commit 1
fi
if [ ! -f "$SYNC_EXPECTED_STATE" ]; then
  echo "Expected upstream state is missing; rerun Step 1" >&2
  fail_before_commit 1
fi
if ! STORED_TARGET_SHA=$(tr -d '\r\n' < "$SYNC_EXPECTED_STATE"); then
  echo "Unable to read the Git-internal expected upstream state" >&2
  fail_before_commit 1
fi
if ! printf '%s\n' "$STORED_TARGET_SHA" | grep -Eq '^[0-9a-f]{40}$'; then
  echo "Stored expected upstream SHA is invalid" >&2
  fail_before_commit 1
fi
if [ "${GITHUB_ACTIONS:-}" = "true" ]; then
  if [ -z "${EXPECTED_UPSTREAM_SHA:-}" ] || [ "$EXPECTED_UPSTREAM_SHA" != "$STORED_TARGET_SHA" ]; then
    echo "Actions EXPECTED_UPSTREAM_SHA is missing or differs from Git-internal state" >&2
    fail_before_commit 1
  fi
fi
EXPECTED_UPSTREAM_SHA="$STORED_TARGET_SHA"
if [ ! -f "$SYNC_ORIGINAL_HEAD_STATE" ]; then
  echo "Original-HEAD state is missing; refusing to commit" >&2
  fail_before_commit 1
fi
if ! ORIGINAL_HEAD_SHA=$(tr -d '\r\n' < "$SYNC_ORIGINAL_HEAD_STATE"); then
  echo "Unable to read the Git-internal original-HEAD state" >&2
  fail_before_commit 1
fi
if [ ! -e "$SYNC_MERGE_HEAD_PATH" ]; then
  echo "MERGE_HEAD is missing before commit" >&2
  fail_before_commit 1
fi
if ! MERGE_HEAD_LINE_COUNT=$(awk 'NF { count += 1 } END { print count + 0 }' "$SYNC_MERGE_HEAD_PATH"); then
  echo "Unable to count MERGE_HEAD entries" >&2
  fail_before_commit 1
fi
if [ "$MERGE_HEAD_LINE_COUNT" -ne 1 ]; then
  echo "MERGE_HEAD must contain exactly one target" >&2
  fail_before_commit 1
fi
if ! MERGE_HEAD_SHA=$(tr -d '\r\n' < "$SYNC_MERGE_HEAD_PATH"); then
  echo "Unable to read MERGE_HEAD" >&2
  fail_before_commit 1
fi
if [ "$MERGE_HEAD_SHA" != "$EXPECTED_UPSTREAM_SHA" ]; then
  echo "MERGE_HEAD does not equal EXPECTED_UPSTREAM_SHA" >&2
  fail_before_commit 1
fi
if ! ORIG_HEAD_SHA=$(git rev-parse --verify ORIG_HEAD); then
  echo "ORIG_HEAD is missing before commit" >&2
  fail_before_commit 1
fi
if [ "$ORIG_HEAD_SHA" != "$ORIGINAL_HEAD_SHA" ]; then
  echo "ORIG_HEAD does not equal the saved original HEAD" >&2
  fail_before_commit 1
fi
if [ -L .github/upstream-main.sha ] || [ ! -e .github/upstream-main.sha ]; then
  echo "The upstream marker is missing or is a symbolic link" >&2
  fail_before_commit 1
fi
if ! WORKTREE_TARGET_SHA=$(tr -d '\r\n' < .github/upstream-main.sha); then
  echo "Unable to read .github/upstream-main.sha" >&2
  fail_before_commit 1
fi
if ! INDEX_TARGET_SHA=$(git show :".github/upstream-main.sha" | tr -d '\r\n'); then
  echo "Unable to read the staged upstream marker" >&2
  fail_before_commit 1
fi
if [ "$WORKTREE_TARGET_SHA" != "$EXPECTED_UPSTREAM_SHA" ] ||
   [ "$INDEX_TARGET_SHA" != "$EXPECTED_UPSTREAM_SHA" ]; then
  echo "The upstream marker is not staged with EXPECTED_UPSTREAM_SHA" >&2
  fail_before_commit 1
fi

# 提交合并和适配（仅已追踪文件的更新 + 新文件 + 目标文件）
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

fail_after_commit() {
  FAILURE_STATUS="${1:-1}"
  echo "Post-commit verification failed; preserving Git-internal state for manual inspection" >&2
  exit "$FAILURE_STATUS"
}

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
  echo "The first merge parent is not the saved original HEAD" >&2
  fail_after_commit 1
fi
if [ "$SECOND_PARENT" != "$EXPECTED_UPSTREAM_SHA" ]; then
  echo "The second merge parent is not EXPECTED_UPSTREAM_SHA" >&2
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
echo "Created and verified the two-parent upstream merge commit"
```

以上检查任一失败，说明 merge commit 的提交关系被破坏（不是恰好双亲 merge、父序不对，
或上游提交不是祖先），禁止继续发布，回到 Step 4e 门禁排查。

```bash
# 查看当前版本
python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])"

# 编辑两个 pyproject.toml，version 字段末尾 +1（如 0.1.15 → 0.1.16）

# 提交版本号变更
git add pyproject.toml skills/ppt-master/pyproject.toml
git commit -m "chore: bump version to X.Y.Z"
```

版本提交保持独立，与 merge commit 分离。Step 6 成功结束时已清理 `.git` 内部临时状态；
之后的独立代码块必须从已提交 marker 重读目标，不得引用 Step 6 的 shell 变量。版本提交
完成后再次验证 ancestry 仍然成立：

```bash
if ! EXPECTED_UPSTREAM_SHA=$(git show HEAD:.github/upstream-main.sha | tr -d '\r\n'); then
  echo "Unable to read the committed upstream marker" >&2
  exit 1
fi
if ! printf '%s\n' "$EXPECTED_UPSTREAM_SHA" | grep -Eq '^[0-9a-f]{40}$'; then
  echo "Committed upstream marker is invalid" >&2
  exit 1
fi
if ! git merge-base --is-ancestor "$EXPECTED_UPSTREAM_SHA" HEAD; then
  echo "Expected upstream SHA is not an ancestor of HEAD after version commit" >&2
  exit 1
fi
```

**如果在 GitHub Actions 环境中运行：**

- schedule 和 workflow_dispatch 都只由准备 job 运行 OpenCode；模型不得获得任何 GitHub 写凭据，也不得 push 或创建 PR。
- 新 trusted runner 在重新验证 bundle、manifest、对象 ancestry 和 main 未前进后，最后一个 step 才使用 `PUSH_PAT` 将明确的 verified SHA 推到唯一同步分支并创建 PR。
- 两条 Actions 路径都禁止直接推送 `main`；PR 合并后再进入现有发布门禁。

**如果本地运行**，完成验证后的提交后停止；发布操作必须由受信任维护流程单独执行。

---

### Step 7: 验证 PyPI 发布

输出验证信息即可，不要尝试执行 `gh` CLI 命令。

**schedule / workflow_dispatch 触发路径：** trusted runner 会创建同步 PR；合并后由现有发布门禁继续。查看 https://github.com/elvisw/ppt-master/actions

**workflow_dispatch 路径：** 输出 "同步提交完成，workflow 将验证 ancestry 并自动 push，下游 CI 链自动触发。查看 https://github.com/elvisw/ppt-master/actions"

**本地流程：** 输出 Actions 页面 URL，提醒用户运行 `uvx ppt-master --version` 验证。

---

## 参考文档

- 上游同步指南：`docs/zh/upstream-sync.md`
- uvx 改造最终笔记：`docs/superpowers/2026-06-08-uvx-refactor-final.md`
- uvx 改造设计：`docs/superpowers/specs/2026-06-08-uvx-refactor-design.md`
- CLI 命令映射：`cli.py` + `skills/ppt-master/cli.py`
- 同步检查脚本：`skills/ppt-master/scripts/check_cli_sync.py`
- 依赖同步检查：`skills/ppt-master/scripts/check_deps_sync.py`
- AGENTS.md 版本规范：`AGENTS.md`
