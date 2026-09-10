# 设计文档：上游同步提交关系修复

**日期**: 2026-09-10
**状态**: 已确认，审阅后修订，待复审
**来源**: GitHub Actions 运行 `34400493716` 与 PR #65

## 1. 背景

定时同步运行检测到上游提交 `64b65839`，并一度成功执行
`git merge upstream/main --no-edit`。OpenCode 随后为了整理提交结构执行
`git reset --soft 8aee5cd0`，再把相同文件树提交为 `726c386b`。

`726c386b` 的提交消息声称它是 merge，但 GitHub API 只报告父提交
`8aee5cd0`。上游代码内容已进入 fork，上游提交关系却没有进入 fork：
当前 `git log origin/main..upstream/main --oneline` 仍列出 `64b65839`。

根因是同步文档要求 Step 2 立即 merge，Step 6 又创建一个 merge/adaptation
提交，却未禁止历史改写，也没有在发布边界验证 ancestry。模型因此把提交
消息和提交数量误当成契约，破坏了真正的双父 merge commit。

历史扫描还发现多个提交消息以 `merge upstream/main` 开头、实际却只有一个父
节点的旧提交。本次不改写或清洗既有历史；新的 ancestry 不变量从 repair merge
开始生效，后续检查不得再根据提交消息推断 merge 真实性。

## 2. 目标与范围

- 保证每次上游同步产生一个真实的双父 merge commit。
- 保证本次同步选定的 upstream SHA 在推送、合并和发布前是 `HEAD` 的祖先。
- schedule 路径同时具有模型内门禁和独立 PR CI 门禁。
- workflow_dispatch 路径由 workflow 验证后确定性推送，不允许模型直接推送。
- 创建一次 ancestry repair merge，把当前 `upstream/main` 纳入 fork 历史。
- 在 `main → check-uvx-migration → auto-tag → publish-pypi` 发布边界强制验证 ancestry。
- fork 仓库关闭 squash merge 与 rebase merge，只保留 merge commit。
- 保留现有 uvx、attribution、fork marker、依赖和其他发布门禁。
- 本次修复实施不自动 push、不创建远端 PR；所有流程都不得修改上游仓库。

## 3. 同步提交模型

### 3.1 开始 merge

Step 1 在 fetch 后记录不可变的目标 SHA：

```bash
EXPECTED_UPSTREAM_SHA=$(git rev-parse upstream/main)
```

workflow 同时把该值写入 `steps.upstream.outputs.upstream_sha`，两个 OpenCode 路径
都必须使用该 SHA，而不是稍后可能变化的分支名。Step 2 改为：

```bash
git merge --no-ff --no-commit "$EXPECTED_UPSTREAM_SHA"
```

`--no-ff` 保证即使可以快进也创建 merge commit；`--no-commit` 让冲突解决、
uvx 适配和提交前门禁在 merge 状态中完成。模型不得在该命令之后执行
`git reset`、`git rebase`、squash merge、`git cherry-pick`、切换分支或清除
`MERGE_HEAD`。该禁令从 merge 开始生效，到真实 merge commit 创建完成为止；
无法解决冲突时唯一允许的退出方式是 `git merge --abort` 后停止运行。

### 3.2 同步目标记录

新增 `.github/upstream-main.sha`，内容是一行 40 位小写 Git SHA。每次同步把
`EXPECTED_UPSTREAM_SHA` 写入该文件，并把它纳入真实 merge commit。该文件是
CI 的稳定验证输入，避免 PR 审阅期间上游继续前进时，把一次有效同步误判为
ancestry 损坏。

### 3.3 提交与版本

Step 6 先提交已暂存的 merge 结果：

```bash
git add -u
git add .github/upstream-main.sha cli.py skills/ppt-master/cli.py pyproject.toml skills/ppt-master/pyproject.toml
git commit -m "merge upstream/main: resolve conflicts, adapt to uvx, sync cli.py mappings"
```

该提交必须保留 fork 原 `HEAD` 和 `EXPECTED_UPSTREAM_SHA` 两个父节点，并包含
更新后的 `.github/upstream-main.sha`。完成 merge commit 后再修改版本并创建独立
的 `chore: bump version` 提交。

### 3.4 Ancestry 门禁

在 merge commit 后以及最终发布边界执行：

```bash
git merge-base --is-ancestor "$EXPECTED_UPSTREAM_SHA" HEAD
```

非零结果是阻断失败，不得通过文件内容相同、提交消息含 `merge`、或模型报告
成功来降级。同步分支还必须证明至少一个新增 merge commit 的直接父节点等于
`EXPECTED_UPSTREAM_SHA`。失败日志打印 `HEAD`、目标 SHA、目标文件内容和相关
提交图。

## 4. GitHub Actions 边界

### 4.1 Schedule 路径

`.github/workflows/sync-upstream.yml` 的 schedule prompt 指向
`.opencode/command/sync-upstream.md` 的提交模型，并明确禁止历史改写。
两个 prompt 的 `CRITICAL` 块都传入并引用
`${{ steps.upstream.outputs.upstream_sha }}`。OpenCode action 仍负责创建分支和 PR。

新增 `.github/workflows/check-upstream-ancestry.yml`，不配置 `paths` filter。它对
所有 PR 先比较 base 与 head 的 `.github/upstream-main.sha`；文件未变化时明确
输出“非上游同步 PR”并成功结束，文件新增或变化时：

1. 以完整历史检出 PR head SHA，而不是 GitHub 临时 merge ref。
2. fetch `hugohe3/ppt-master` 的 `main` 为 `upstream/main`。
3. 验证目标文件格式、目标 SHA 属于真实上游历史，并且是 PR head 的祖先。
4. 验证 base..head 范围内存在以目标 SHA 为直接父节点的 merge commit。
5. 失败时输出 base/head/目标 SHA 与提交图，并以非零状态阻止该 PR 被视为有效同步。

验证使用目标文件中的固定 SHA，不要求 PR head 包含检查时最新的 upstream tip。
因此普通 PR 和上游在审阅期继续前进都不会误报；分支命名变化也不会绕过检查。

### 4.2 Workflow Dispatch 路径

manual prompt 和 `.opencode/command/sync-upstream.md` 的 GitHub Actions 段都改为
只修改和提交，禁止模型 push。模型运行前不再把 `PUSH_PAT` 写入 remote URL，
manual 的 OpenCode 环境也不再注入可写 `GH_TOKEN`。`opencode run` 返回后，workflow
执行独立的 `Verify upstream ancestry` step；通过后才在独立 push step 中注入 PAT、
设置 remote 并运行 `git push origin HEAD:main`。验证失败时 shell 直接终止，模型
无法用文字声明绕过。

如果 push 因 `main` 并发前进而被 non-fast-forward 拒绝，本次运行失败并停止；
不得 rebase 或 force-push，必须从最新 `main` 重新执行完整同步流程。

### 4.3 Main 发布边界

`.github/workflows/check-uvx-migration.yml` 在现有 uvx 检查之前读取
`.github/upstream-main.sha`，fetch 上游并验证：

1. 目标文件是合法 SHA，且目标提交属于 `upstream/main` 历史。
2. 目标 SHA 是当前 `main` HEAD 的祖先。

任何失败都让 `Check UVX Migration` 失败。`auto-tag.yml` 已只在该 workflow 成功时
继续，因此 ancestry 损坏会阻断 tag 与 PyPI 发布。该门禁验证最后一次已登记的
同步目标，不要求 fork 在每次普通 main push 时追平实时 upstream tip。

### 4.4 仓库合并设置与落盘方式

通过 fork API 关闭 `allow_squash_merge` 和 `allow_rebase_merge`，保留
`allow_merge_commit=true`，并在修改前后读取 API 回执。该设置只作用于
`elvisw/ppt-master`，不触碰上游。

repair 分支如创建 PR，必须使用 `gh pr merge --merge`；不得使用 squash 或 rebase。
合并后必须 fetch `origin/main` 并在远端主分支上重跑 ancestry 门禁。当前任务不自动
push 或创建 PR，仓库设置修改和本地提交除外。

## 5. 当前历史修复

在已包含设计和实现提交的 `fix/sync-upstream-ancestry` 分支上执行：

```bash
git merge --no-ff --no-commit 64b65839c7f8096a534c872c03d688a2b2491c8f
```

保留 fork 的 `svg-pipeline.md` uvx 适配和所有现有 fork marker，审查合并树后创建
真实 merge commit，并把该 SHA 写入 `.github/upstream-main.sha`。该提交用于记录
`64b65839` 的 ancestry；不通过 reset、rebase、squash 或 cherry-pick 重建。

历史修复后，本地 `HEAD` 必须满足：

```bash
git merge-base --is-ancestor 64b65839c7f8096a534c872c03d688a2b2491c8f HEAD
git log HEAD..64b65839c7f8096a534c872c03d688a2b2491c8f --oneline
```

第一条返回 0，第二条无输出。修复只在本地分支提交，不自动推送。

下一次 schedule 在 repair merge 到达远端 `main` 前仍会把 `64b65839` 识别为缺失；
这期间若产生冗余同步 PR，应关闭而非合并。repair 落地后检测自然恢复为空。

## 6. 错误处理

- `git merge --no-ff --no-commit` 冲突无法安全解决时执行 `git merge --abort` 并停止。
- ancestry 门禁失败时不尝试用内容比较替代，不推送，不创建有效同步结论。
- schedule PR 检查只读上游，不向上游写入任何内容。
- manual push 仅在 OpenCode 成功退出且 ancestry 门禁通过后执行。
- manual push non-fast-forward 时停止并从最新 main 重跑，禁止 force-push。
- PR 被 squash/rebase 的入口通过 fork 仓库设置关闭；main 门禁继续作为发布兜底。
- 现有 attribution、CLI sync、F821、依赖与 fork marker 任一门禁失败时仍按原流程阻断。

## 7. 验证计划

1. 静态审查提交关系规则由 `.opencode/command/sync-upstream.md` 所有，两个 prompt 只传入目标 SHA、指向所有者并保留必要的 CRITICAL 门禁。
2. 对历史故障提交 `726c386b` 和目标 `64b65839` 运行 ancestry 检查，确认负向场景失败。
3. 在临时 clone/branch 沙盘执行完整的 Step 2→6，验证 `MERGE_HEAD`、双父 merge、目标文件和独立版本提交。
4. 对 repair 分支最终 `HEAD` 运行 ancestry 检查，确认通过且目标缺失列表为空。
5. 验证 repair merge commit 至少有两个父节点，其中包含实现分支原 HEAD 和 `64b65839`。
6. 校验三个 workflow YAML 可被解析，检查 schedule/manual 条件、凭据注入和 verify-before-push 顺序。
7. 验证 PR check 的三种输入：目标文件不变时跳过、合法 repair 通过、篡改目标或单父伪 merge 失败。
8. 验证 main 发布门禁对 repair HEAD 通过、对仅更新目标文件但没有 ancestry 的构造提交失败。
9. 读取 GitHub API 确认只允许 merge commit；未来 repair PR 使用 merge 模式，落地后 fetch 并在 `origin/main` 复验目标 ancestry。
10. 运行现有 `check_cli_sync.py`、attribution guard、fork marker、`py_compile`、Ruff F821 和依赖同步门禁。
11. 审查 `git diff`、`git log --graph` 和工作区状态，确认未夹带无关改动。

## 8. 涉及文件

- `.opencode/command/sync-upstream.md`
- `.github/workflows/sync-upstream.yml`
- `.github/workflows/check-upstream-ancestry.yml`（新增）
- `.github/workflows/check-uvx-migration.yml`
- `.github/upstream-main.sha`（新增）
- `docs/superpowers/specs/2026-09-10-sync-upstream-ancestry-design.md`

当前 ancestry repair 通过 Git 提交关系完成，不通过修改上游代码文件模拟。
