# 设计文档：上游同步提交关系修复

**日期**: 2026-09-10
**状态**: 已确认，待实施
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

## 2. 目标与范围

- 保证每次上游同步产生一个真实的双父 merge commit。
- 保证 `upstream/main` 在推送或合并前是同步分支 `HEAD` 的祖先。
- schedule 路径同时具有模型内门禁和独立 PR CI 门禁。
- workflow_dispatch 路径由 workflow 验证后确定性推送，不允许模型直接推送。
- 创建一次 ancestry repair merge，把当前 `upstream/main` 纳入 fork 历史。
- 保留现有 uvx、attribution、fork marker、依赖和发布门禁，不改发布链。
- 不自动 push，不创建远端 PR，不修改上游仓库。

## 3. 同步提交模型

### 3.1 开始 merge

Step 2 改为：

```bash
git merge --no-ff --no-commit upstream/main
```

`--no-ff` 保证即使可以快进也创建 merge commit；`--no-commit` 让冲突解决、
uvx 适配和提交前门禁在 merge 状态中完成。模型不得在该命令之后执行
`git reset`、`git rebase`、squash merge 或 `git cherry-pick` 来重写上游关系。

### 3.2 提交与版本

Step 6 先提交已暂存的 merge 结果：

```bash
git add -u
git add cli.py skills/ppt-master/cli.py pyproject.toml skills/ppt-master/pyproject.toml
git commit -m "merge upstream/main: resolve conflicts, adapt to uvx, sync cli.py mappings"
```

该提交必须保留 fork 原 `HEAD` 和 `upstream/main` 两个父节点。完成 merge commit
后再修改版本并创建独立的 `chore: bump version` 提交。

### 3.3 Ancestry 门禁

在 merge commit 后以及最终发布边界执行：

```bash
git merge-base --is-ancestor upstream/main HEAD
```

非零结果是阻断失败，不得通过文件内容相同、提交消息含 `merge`、或模型报告
成功来降级。失败日志必须打印 `HEAD`、`upstream/main` 与
`git log HEAD..upstream/main --oneline`。

## 4. GitHub Actions 边界

### 4.1 Schedule 路径

`.github/workflows/sync-upstream.yml` 的 schedule prompt 指向
`.opencode/command/sync-upstream.md` 的提交模型，并明确禁止历史改写。
OpenCode action 仍负责创建分支和 PR。

新增 `.github/workflows/check-upstream-ancestry.yml`。它对
`opencode/schedule-*` PR：

1. 以完整历史检出 PR head SHA，而不是 GitHub 临时 merge ref。
2. fetch `hugohe3/ppt-master` 的 `main` 为 `upstream/main`。
3. 运行 ancestry 门禁。
4. 失败时输出双方 SHA 与缺失提交，并以非零状态阻止该 PR 被视为有效同步。

普通功能 PR 不执行上游 ancestry 判断，避免上游在 PR 开发期间前进时误报。

### 4.2 Workflow Dispatch 路径

manual prompt 改为只修改和提交，禁止模型 push。`opencode run` 返回后，workflow
执行独立的 `Verify upstream ancestry` step；通过后才由下一步运行
`git push origin HEAD:main`。验证失败时 shell 直接终止，模型无法用文字声明绕过。

## 5. 当前历史修复

在 `fix/sync-upstream-ancestry` 分支基于当前 fork `main` 执行：

```bash
git merge --no-ff --no-commit upstream/main
```

保留 fork 的 `svg-pipeline.md` uvx 适配和所有现有 fork marker，审查合并树后创建
真实 merge commit。该提交用于记录 `64b65839` 的 ancestry；不通过 reset、rebase、
squash 或 cherry-pick 重建。

历史修复后，本地 `HEAD` 必须满足：

```bash
git merge-base --is-ancestor upstream/main HEAD
git log HEAD..upstream/main --oneline
```

第一条返回 0，第二条无输出。修复只在本地分支提交，不自动推送。

## 6. 错误处理

- `git merge --no-ff --no-commit` 冲突无法安全解决时执行 `git merge --abort` 并停止。
- ancestry 门禁失败时不尝试用内容比较替代，不推送，不创建有效同步结论。
- schedule PR 检查只读上游，不向上游写入任何内容。
- manual push 仅在 OpenCode 成功退出且 ancestry 门禁通过后执行。
- 现有 attribution、CLI sync、F821、依赖与 fork marker 任一门禁失败时仍按原流程阻断。

## 7. 验证计划

1. 静态审查两个同步 prompt 与命令文档只保留一个提交关系所有者，其他位置使用指针式措辞。
2. 对历史故障提交 `726c386b` 运行 ancestry 检查，确认它不能通过。
3. 对 repair 分支最终 `HEAD` 运行 ancestry 检查，确认通过且缺失提交列表为空。
4. 验证 repair merge commit 至少有两个父节点，其中包含同步前 fork `HEAD` 和 `64b65839`。
5. 校验两个 workflow YAML 可被解析，检查 schedule 与 manual 条件及 push 顺序。
6. 运行现有 `check_cli_sync.py`、attribution guard、fork marker、`py_compile`、Ruff F821 和依赖同步门禁。
7. 审查 `git diff`、`git log --graph` 和工作区状态，确认未夹带无关改动。

## 8. 涉及文件

- `.opencode/command/sync-upstream.md`
- `.github/workflows/sync-upstream.yml`
- `.github/workflows/check-upstream-ancestry.yml`（新增）
- `docs/superpowers/specs/2026-09-10-sync-upstream-ancestry-design.md`

当前 ancestry repair 通过 Git 提交关系完成，不通过修改上游代码文件模拟。
