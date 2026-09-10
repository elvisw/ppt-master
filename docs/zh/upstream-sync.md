# 合并上游更新工作流

## 远程仓库布局

```
origin    → https://github.com/elvisw/ppt-master.git    (你的 fork)
upstream  → https://github.com/hugohe3/ppt-master.git   (原作者)
```

---

## 触发方式总览

| 方式 | 触发 | 适用场景 | 自动发布 |
|------|------|----------|----------|
| **定时自动 (schedule)** | 每日 UTC 18:00 | 定期维护，无需人工干预 | ✅ 创建 PR |
| **手动 (workflow_dispatch)** | 仅允许从 `main` 分支运行 | 需要立即生成同步 PR | ✅ trusted job 创建 PR |
| **本地 CLI** | `.opencode/command/sync-upstream.md` | 本地开发时手动执行 | ❌ 手动打 tag |

---

## Task 5A 最终安全边界

`sync-upstream.yml` 使用固定 `sync-upstream` concurrency group，`cancel-in-progress: false`。
schedule 和 workflow_dispatch 共用相同的两 job 路径：

1. `prepare-candidate` 只声明 `contents: read`，以 `${{ github.sha }}` 完整检出并验证 immutable
   base；workflow_dispatch 非 `refs/heads/main` 时 fail-closed。
2. 该 job 明确 fetch canonical `upstream/main`；fetch 失败不使用 stale ref。无变化时模型、artifact、
   trusted job、分支和 PR 全部跳过。
3. 有变化时只执行一次固定 `opencode-ai@1.18.30`。模型只获得 API key、模型名、immutable target
   和普通 Actions 元数据；不获得 `github.token`、`GITHUB_TOKEN`、`GH_TOKEN`、`PUSH_PAT` 或
   `id-token: write`，也不能 push 或创建 PR。
4. 模型提交后只上传 `candidate.bundle` 与严格三字段 `manifest.json`；不上传 worktree 或 `.git/config`。
5. `verify-and-open-pr` 在 fresh runner 上从 base SHA 检出 trusted helper，只将 bundle/manifest 当作
   不可信 Git object data。它不 checkout 或执行 candidate 文件、hook、workflow、配置、action 或进程。
6. trusted helper 重新验证 manifest key/SHA、bundle ref tip、base/target/candidate object、marker、
   upstream ancestry、唯一严格双父 merge、版本 bump 和 protected gate policy。
7. 最终 publication step 才注入 `PUSH_PAT`，使用 null global/system Git config 和 disabled hooks，
   将明确 verified SHA 推到 `opencode/sync-<run-id>-<attempt>`，再创建 `elvisw/ppt-master` → `main` PR。
   绝不推送 `main`；main 前进、non-fast-forward 或任意 artifact mismatch 都 fail-closed。

普通 PR 不得修改 protected workflow/helper/command/gate/test 文件；这些文件必须通过显式 trusted
maintenance/bootstrap 流程维护。首次部署该 gate 的 PR 可能没有被 base 自身保护，必须依靠人工审查、
独立测试和真实 Git/artifact 沙盘证明，不能声称新 gate 已保护它。

---

### 方式一：定时自动（GitHub Actions schedule）

`sync-upstream.yml` 每日 UTC 18:00（北京时间次日 02:00）自动运行：

```
schedule cron → 检测 upstream/main → 无变化则跳过；有变化则注入 immutable SHA
→ prepare-candidate 运行 OpenCode → 上传 bundle/manifest → fresh trusted runner 复核
→ 推唯一同步分支并创建 PR
→ PR 门禁通过并合并到 main → auto-tag.yml → publish-pypi.yml
```

schedule 路径不使用 OpenCode GitHub Action。模型只准备候选提交；trusted job 使用 PAT 创建
同步 PR，不直接推送 `main`。上游无变化时所有后续步骤均正常跳过。

`EXPECTED_UPSTREAM_SHA` 由 workflow 注入 OpenCode 环境；Actions 中缺失或与 fetch 后
的 `upstream/main` 不一致会 fail-closed。只有本地执行时，变量为空才允许回退到 fetch
后的 upstream tip；CLI 会用 `git rev-parse --git-path ppt-master-sync` 和原子 `mkdir`
建立每次同步的独占 `.git` 状态目录，把 fallback target、original HEAD、marker 原始
存在状态/字节快照和 merge ownership 写入其中，后续独立 shell 逐块重读，成功或已安全
恢复的失败路径清理整个状态目录。

### 方式二：手动触发（workflow_dispatch）

1. 在 GitHub Actions 中选择 `Sync Upstream` 的 `workflow_dispatch`
2. 选择 `main`；非 `refs/heads/main` 会被前置 guard 拒绝
3. 有上游变化时，workflow 将 immutable `EXPECTED_UPSTREAM_SHA` 和模型变量注入固定 CLI
4. OpenCode 只合并、适配和提交，不获得任何 GitHub 写凭据，也不得执行 push
5. 新 trusted runner 下载并验证严格 manifest、Git bundle、base/target/candidate object、唯一双父
   merge、版本形状和 protected gate policy
6. 最后一个 workflow step 才注入 `PUSH_PAT`，显式推 verified SHA 到唯一分支并创建 PR；缺少 PAT、
   main 前进或验证失败均停止，不 force-push、不 push main
7. 上游无变化时 OpenCode、artifact、trusted job、branch 和 PR 均跳过

### 方式三：本地 OpenCode CLI

```bash
# 在项目根目录下
opencode
# 然后输入: /sync-upstream
```

本地执行不会触发 GitHub Actions 的 main-ref/PAT 推送路径。命令会用独占 `.git/ppt-master-sync/`
状态目录跨 shell 持久化 target、merge 前 HEAD 和 marker 原始状态；merge 成功后先确认
`MERGE_HEAD` 恰好为固定 target，再用本次 merge 设置的 `ORIG_HEAD` 校验原始第一父，最后
才写入/暂存 marker。already-up-to-date、冲突、marker 写入或 `git add` 失败只有在当前
HEAD/ORIG_HEAD/MERGE_HEAD ownership 全部成立时才会 abort 并恢复 marker；foreign merge、
foreign HEAD 或 ignored/untracked marker 会保留现场并停止。

---

## GitHub Actions 工作流一览

| 工作流文件 | 触发 | 功能 |
|-----------|------|------|
| `sync-upstream.yml` | schedule / workflow_dispatch（仅 main） | 两 job 隔离模型与凭据；trusted job 推唯一分支并创建 PR |
| `auto-tag.yml` | push to main (pyproject.toml 变更) | 7 道门禁校验 + 自动打 tag → 触发 PyPI 发布 |
| `publish-pypi.yml` | tag push `v*` | 构建 wheel + 发布到 PyPI |
| `opencode.yml` | issue_comment `/oc` | 通用 OpenCode Agent 入口 |
| `check-uvx-migration.yml` | push to main (merge commit) | 检测合并提交中 `python3` 命令残留 |

### auto-tag.yml 门禁

```
Gate 0: 两个 pyproject.toml 版本一致
Gate 1: cli.py 映射完整 (check_cli_sync.py)
Gate 2: .md 文件中无 python3 残留
Gate 3: .md 文件中无 uv run 残留
Gate 4: 依赖清单一致 (check_deps_sync.py)
Gate 5: Skill 完整性 guard
Gate 6: wheel attribution 文件完整
→ 全部通过 → git tag vX.Y.Z → publish-pypi.yml
```

---

## 冲突处理

### 核心原则

**保留 fork 的 uvx 适配，合入上游的新功能。`skills/ppt-master/scripts/*.py` 除 `attribution_guard.py` 与 `register_template.py` 外零改动。**

| 冲突类型 | 解决策略 |
|----------|----------|
| `python3 scripts/xxx.py` vs `uvx ppt-master xxx` | 保留 uvx |
| 上游新增 .md 文件中的 `python3` 命令 | 同化为 `uvx ppt-master <cmd>` |
| `cli.py` | 无冲突（上游无此文件）；检查新脚本是否已映射 |
| `pyproject.toml` | 保留 fork 结构（version, tool.uv, tool.setuptools），只同步依赖 |
| `README.md` / `README_CN.md` | 接受上游内容后，在语言切换行与赞助商 `<details>` 块之间重新插入 fork 声明块（`Fork notice` / `Fork 声明`），声明赞助商与捐赠信息属于原作者、与本 fork 无关 |
| `update_repo.py` | 保留 fork 的 uv 功能，合入上游改进 |
| `skills/ppt-master/scripts/*.py` | **零改动** —— docstring 中 `python3` 残留已知且可接受 |
| `attribution_guard.py` | **保留 fork 的 `_SKILL_GATE_MARKER`（`uvx ppt-master attribution-guard`）**；合入上游其他改动；合并后必须运行 guard 验证（exit 0） |
| `register_template.py` | **保留 fork 的 `PPT_MASTER_TEMPLATES_DIR` 库根解析**（env > cwd 检出 > wheel 内置）；合入上游其他改动。uvx wheel 缓存只读，注册必须落到可写检出目录 |

### 命令转换规则

**规则一：cli.py 已有映射 → `uvx ppt-master <command>`**

对于 `cli.py` 的 `COMMANDS` 字典中已存在的映射：
- `python3 skills/ppt-master/scripts/xxx.py` → `uvx ppt-master <cmd>`
- `python3 scripts/xxx.py` → `uvx ppt-master <cmd>`
- `` `uv` `run` `skills/ppt-master/scripts/xxx.py` `` → `uvx ppt-master <cmd>`
- `uv run scripts/xxx.py` → `uvx ppt-master <cmd>`

**规则二：cli.py 无映射 → 添加到 cli.py**

运行 `python skills/ppt-master/scripts/check_cli_sync.py` 检测缺失脚本。

kebab-case 命名：下划线 `_` → 连字符 `-`，子目录取文件名。同时添加到根目录和 `skills/ppt-master/` 两个 `cli.py` 的 `COMMANDS` 和 `COMMAND_DESCRIPTIONS`。

### 内部脚本（不转换）

以下脚本没有 cli.py 映射，保留 `uv run` 调用：
- `svg_finalize/flatten_tspan.py`
- `svg_finalize/svg_rect_to_path.py`
- `svg_finalize/fix_image_aspect.py`
- `svg_finalize/embed_icons.py`

### 豁免目录

以下目录不进行 `python3` → `uvx` 转换：
- `docs/superpowers/` — 设计文档
- `docs/windows-installation.md` — 安装文档
- `docs/rules/code-style.md` — 代码规范
- `docs/zh/upstream-sync.md` — 本文档

---

## 上游新增依赖时

1. 同步到两个 `pyproject.toml` 和 `requirements.txt`
2. 在两个目录运行 `uv lock`：
   ```bash
   uv lock && cd skills/ppt-master && uv lock
   ```
3. 运行校验：
   ```bash
   python skills/ppt-master/scripts/check_deps_sync.py
   ```

**pyyaml 依赖保护**：`pyyaml>=6.0` 是 `register-template` 所需依赖。上游已修复 Issue #269 并在 `skills/ppt-master/requirements.txt` 顶部声明 `PyYAML>=6.0`，不再是 fork 独有。合并上游依赖变更时必须保留 pyyaml 且只保留一份（上游条目在文件顶部，fork 旧条目在文件底部——若合并后出现两份，删除底部 fork 旧条目，保留上游顶部条目）；`check_deps_sync.py` 只校验三份清单互相一致，合并后需人工确认 pyyaml 仍在三份清单中且无重复。

---

## 版本发布

### AGENTS.md 约束

打 `v*` tag 前，**必须** 更新两个 `pyproject.toml` 的 `version` 字段为同一值。

### 手动发布

```bash
git tag vX.Y.Z && git push origin vX.Y.Z
```
`publish-pypi.yml` 自动构建发布。

### 自动发布

`auto-tag.yml` 在门禁通过后自动打 tag。tag 推送触发 `publish-pypi.yml`。

---

## 重要注意事项

- **`.gitignore` 必须包含 `!uv.lock` 例外规则**（`*.lock` 会匹配 `uv.lock`）
- **`workflow_dispatch` 只允许 `refs/heads/main`** — 前置 guard 缺失或 ref 不匹配时
  fail-closed；这不是可从功能分支绕过的同步入口
- **OpenCode 不 push** — schedule 和 manual 的模型都不接触任何 GitHub 写凭据；fresh trusted
  runner 只有在所有 object/ref/ancestry 门禁通过后才注入 `PUSH_PAT`，推明确 SHA 到唯一同步分支
  并创建 PR，绝不推送 `main`
- **无变化正常跳过** — OpenCode、artifact、trusted job、branch 和 PR 统一由
  `has_changes == 'true'` 控制
- **artifact 必须严格** — 只允许 `candidate.bundle` 与 manifest；manifest 只能有
  `base_sha`、`target_sha`、`verified_sha` 三个合法且匹配的 SHA，bundle tip 必须匹配 `verified_sha`
- **验证必须针对提交对象** — trusted helper 只从 base checkout 执行，candidate 只作为 Git object
  data 导入；工作树中的未提交 marker 不能冒充已提交记录
- **验证必须拒绝未跟踪文件** — `git status --porcelain=v1 --untracked-files=all` 非空
  时 Verify 立即失败
- **命令代码块不得共享普通变量** — 每个独立 shell 都通过 `git rev-parse --git-path`
  重读 `.git` 内部 target/original-HEAD 状态；成功和 abort 路径都清理临时状态
- **合并关系必须精确** — `BASE_SHA` 与 immutable 目标都必须是 HEAD 祖先，并且必须
  找到恰好双父 merge，第一父为 `BASE_SHA`、第二父为 `EXPECTED_UPSTREAM_SHA`
- **`skills/ppt-master/scripts/*.py` 的 `python3` 残留** — `check_uvx_migration.yml` 已豁免该目录
- **合并后必须运行 `python skills/ppt-master/scripts/attribution_guard.py` 且 exit 0** — 上游若更新 attribution 约束（`_SKILL_GATE_MARKER`、`_REQUIRED_GATE_FILES`、metadata 字段、LICENSE 摘要），必须同步适配：SKILL.md 只保留一次 `uvx ppt-master attribution-guard`、`skills/ppt-master/` 下 LICENSE/SPONSORS.md/SPONSORS_CN.md 存在、MANIFEST.in（根与 skill）仍包含 4 个 attribution 文件。`auto-tag.yml` 的 Gate 5/6 会拦截发布
- 合并后运行 `uvx ppt-master check-deps-sync` 验证依赖一致性
- 两处 `uv.lock` 文件必须提交以实现可重复构建

---

## 相关文件

| 文件 | 说明 |
|------|------|
| `.github/workflows/sync-upstream.yml` | 两 job 的 candidate bundle、trusted verification 和 PR publication |
| `.github/scripts/check_upstream_ancestry.py` | base checkout 中执行的 ancestry、manifest、版本和 protected-file helper |
| `.opencode/command/sync-upstream.md` | OpenCode sync-upstream 命令定义 |
| `cli.py` | CLI 命令映射（根目录） |
| `skills/ppt-master/cli.py` | CLI 命令映射（skill 目录） |
| `skills/ppt-master/scripts/check_cli_sync.py` | CLI 映射完整性检查 |
| `skills/ppt-master/scripts/check_deps_sync.py` | 依赖清单一致性检查 |
| `skills/ppt-master/scripts/check_uvx_migration.py` | 合并提交中 python3 残留检测 |
| `docs/superpowers/specs/2026-06-08-uvx-refactor-design.md` | uvx 改造设计文档 |
| `docs/superpowers/2026-06-08-uvx-refactor-final.md` | uvx 改造最终笔记 |
