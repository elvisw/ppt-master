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

1. `prepare-candidate` 只声明 `contents: read`，以 canonical public URL 无凭据完整 fetch（等价
   `fetch-depth: 0`）并以 `${{ github.sha }}` detached 检出 immutable base；workflow_dispatch 非
   `refs/heads/main` 时 fail-closed。trusted job 使用同一无凭据 checkout 语义，避免 required token
   的 `actions/checkout` 重新引入 authenticated checkout。
2. 该 job 明确 fetch canonical `upstream/main`；fetch 失败不使用 stale ref。无变化时模型、artifact、
   trusted job、分支和 PR 全部跳过。
3. 有变化时先在无 secret step 固定安装一次 `opencode-ai@1.18.30`，再执行唯一的 `opencode run`。
   只有后者获得 API key、模型名、immutable target 和普通 Actions 元数据；不获得 `github.token`、
   `GITHUB_TOKEN`、`GH_TOKEN`、`PUSH_PAT` 或 `id-token: write`，也不能 push 或创建 PR。
4. 模型提交后只上传 `candidate.bundle` 与严格三字段 `manifest.json`；不上传 worktree 或 `.git/config`。
5. `verify-and-open-pr` 在 fresh runner 上从 base SHA 检出 trusted helper，只将 bundle/manifest 当作
   不可信 Git object data；candidate 只被 materialize 到隔离 worktree 作为数据，不执行 candidate
   文件、hook、workflow、配置、action 或进程。
6. base 版本 `check_sync_candidate.py` 重新验证 CLI `COMMANDS`/`ALIASES`、依赖与 locks、attribution
   frontmatter/digest/files、manifest/notices/sponsors、YAML、uvx diff 和 8 个 fork marker/import；
   trusted `python -I -m py_compile` 与 `ruff --isolated --select F821` 只检查明确 regular 文件。
   ancestry helper 另外验证 manifest key/SHA、bundle ref tip、base/target/candidate object、marker、
   upstream ancestry、唯一严格双父 merge、版本 bump 和 protected gate policy。
7. 最终 publication step 才注入 `PUSH_PAT`，使用 null global/system Git config、禁用终端/askpass、
   空 credential helper 和 disabled hooks，将明确 verified SHA 推到 `opencode/sync-<run-id>-<attempt>`。
   `gh pr create` 后立即用 REST fork API 的 `--jq '.base.sha'` 读取 PR base SHA 并要求等于 base；
   创建失败、push/create 间 main 前进或 base SHA mismatch 时，EXIT trap best-effort 关闭 PR并删除刚推分支后失败。API 检查不原子，
   最终由 trusted `pull_request_target` strict first-parent/required checks 阻断错误 PR；绝不推送
   `main`，non-fast-forward 或任意 artifact mismatch 都 fail-closed。

普通 PR 不得修改整个 `.github/workflows/**`、`.github/pull.yml`、protected helper/command/gate/test 文件。
维护者审阅这些文件的变更后，必须显式添加仓库标签 `ci-maintenance-approved`；trusted
`pull_request_target` 只豁免 protected-path 拒绝，仍执行 marker、ancestry、content、version 等其余门禁。
标签的新增和删除都会重新触发检查。首次部署或修复该 trusted gate 的 PR 无法由旧 base 自我证明，
必须依靠人工审查、独立测试和真实 Git/artifact 沙盘证明，不能声称新 gate 已保护它。

独立 `opencode.yml` 保持原有触发和授权者 guard，action 固定为
`anomalyco/opencode/github@77fc88c8ade8e5a620ebbe1197f3a572d29ae91a`；只保留 checkout 所需
`contents: read` 与 OIDC 所需 `id-token: write`，不把它混入同步模型 job。

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
| `auto-tag.yml` | 成功的 `Check UVX Migration` `workflow_run` | 精确 SHA 全门禁 + 自动推送 exact tag（release SHA 只需仍在 main 历史中） |
| `publish-pypi.yml` | tag push `v*` / manual recovery | 精确 SHA 全门禁 + 无 OIDC 构建 + 静态 artifact 验证 + PyPI 发布 |
| `opencode.yml` | issue_comment `/oc` | 通用 OpenCode Agent 入口 |
| `check-uvx-migration.yml` | push to main (merge commit) | 检测合并提交中 `python3` 命令残留 |

### auto-tag.yml 与 publish-pypi.yml 共同门禁

```
Gate 0: immutable successful Check UVX Migration push run 的 head_sha/path/repo/branch/conclusion
Gate 1: upstream marker/type/SHA/upstream membership/HEAD ancestry
Gate 2: root/Skill 完整 COMMANDS 与 ALIASES 映射
Gate 3: 三份依赖清单与双 uv.lock 同步
Gate 4: git ls-files repository-wide UVX 文档/工作流/prompt 扫描
Gate 5: attribution guard、LICENSE digest、sponsors、frontmatter、双 manifest
Gate 6: 八个 fork marker/import、py_compile 与 Ruff F821
Gate 7: YAML/action pin/protected policy 与 canonical version/tag
→ 全部通过 → 只推送 refs/tags/vX.Y.Z → publish-pypi.yml 重新执行全套门禁
```

scanner 是只读的，使用 `git ls-files -z`，不使用内容关键词豁免，也不改写文件；它会把 shell
反斜杠续行合并为同一逻辑命令，并保留原始首行行号。历史证据和仓库自有 Linux CI 只能通过
scanner 所有者维护的精确路径/规则 allowlist 豁免；`auto_fix_uvx.py` 仍是离线手工工具，复用该
allowlist，不拥有第二套排除语法。

release gate、scanner 和 migration checker 均由 `python -I` 调用；gate 自身还会拒绝
`.github/scripts` 中出现的额外模块（例如 `yaml.py` shadow），并把
`console_encoding.py`、`workflow_transcript.py`、两份 `cli.py` 纳入 protected set。

privileged workflow 的 `setup-uv` 固定精确 uv version 与 Ubuntu x86_64 可执行文件 SHA-256
checksum；build job 以 exact pin 安装 gate/Ruff/build backend，并用
`uv build --no-build-isolation` 构建，不解析 latest/range。release policy 对 auto-tag、
`check-uvx-migration.yml` 与 publish OIDC job 执行完整 step inventory：step 数量、名称、顺序、
key 集合、`uses`/`with`/`env` 和 run block 都必须逐字匹配 approved 模板；任何额外 step、no-op、
前置 `exit 0`/`true`/`set +e`、`|| EXIT=0`、重命名、删除、重排或 step-level `if` 都会
fail-closed。三个 privileged workflow 的 run block 禁止 `git add/commit/am/apply/merge/rebase/
cherry-pick/reset/update-ref`；auto-tag 只允许一个 approved exact tag push，`git push
...refs/heads/main`、`git -c x=y commit` 与 `--force` 变体都会被拒绝。OIDC job 的 run 不允许
pip/python/import/exec/eval/uvx 或 `.whl`，`WHEEL_PATH`/`SDIST_PATH` 只能出现在 approved
bind/publish step。

wheel/sdist 的 Core Metadata 使用 header parser：`Name`/`Version` 按 ASCII case-insensitive
归并后各恰好一个，body 中出现同名字段、重复 header、非 canonical 大小写或非 ASCII 字段名都会
被拒绝；wheel metadata 必须精确为 `ppt_master-{version}.dist-info/METADATA`，且同目录
`WHEEL`/`RECORD` 必须存在。两份 `cli.py` 的 mapping parser 拒绝任何后续 Store 绑定
（For/With/ExceptHandler/comprehension/NamedExpr 等）和 `exec`/`eval`/`globals`/`locals`/
`setattr` 动态逃逸，scanner 复用同一个 trusted parser。repository scanner 的 legacy grammar
覆盖 `python.exe`/`python3.12`/`python ./scripts` 与 `uv run` 中间 flags，扫描根包含
`.claude-plugin/` 与 `projects/`。release gate、candidate 与 migration checker 的全部
`text=True` 子进程调用显式使用 `encoding="utf-8", errors="replace"`。

同步 PR 的内容边界由 `check_upstream_ancestry.py` 单一 owner 验证：它定位严格双父 merge
commit `M`（不把 version-bump HEAD 当身份），计算 `upstream_base = merge-base(M^1, target)`，
枚举 `upstream_base..target` 的全部 changed path（add/delete/mode/symlink 均展开）；非 overlay
路径的 `M` tree entry 必须与 target 完全一致，overlay 路径默认必须与 first parent 不同
（拒绝 `-s ours` 或事后整文件回滚），只有 `.github/upstream-overlay-paths.txt` 中以
`retain-base` 逐路径标注并给出理由时才能保留 fork 内容。该清单是受保护文件，只允许精确路径，
禁止目录/glob/重复；当前清单为 24 个路径（21 个 `merge`、3 个 `retain-base`）。trusted PR
helper、两个 sync job、main gate 与 release chain 共用该 owner。只要 marker 变化且找到 strict
merge，就无条件追加 M..HEAD 漂移门禁：除版本文件外，upstream changed path 在 strict merge
与 candidate HEAD 之间必须完全一致；版本文件若也被 upstream 改动，则只允许 project version
（pyproject）或 root package version（uv.lock）变化。内容评估的 policy 始终来自 strict merge
的 trusted first parent（PR 为 base，head/main 为 `M^1`），且 `M` 的 policy entry 必须与
first parent 完全相等；candidate 不能控制规则。仅 `1fcf7154` + `09ad58f0` 精确配对允许一次性
回退到当前 trusted HEAD 并输出 bootstrap 提示。protected 文件按 commit 逐提交扫描其
first-parent 差异，中间篡改即使恢复也失败。21 个 `merge` 条目需人工审阅第三方 resolution，
3 个 `retain-base` 条目在上游再次触及时由 protected policy 变更强制复审。

`sync-upstream.yml` 的 upload/download artifact、`check-upstream-ancestry.yml` 与
`opencode.yml` 的 checkout、`opencode.yml` 的 `anomalyco/opencode/github` 也已固定到
immutable SHA 并由 release policy 校验；GitHub Pages 部署仍为 out-of-scope residual。

---

## 冲突处理

### 核心原则

**保留 fork 的 uvx 适配，按 hunk 和契约保留 fork 标记/import，并合入上游的新功能与安全修复；不使用整文件零改动规则。**

| 冲突类型 | 解决策略 |
|----------|----------|
| `python3 scripts/xxx.py` vs `uvx ppt-master xxx` | 保留 uvx |
| 上游新增 .md 文件中的 `python3` 命令 | 同化为 `uvx ppt-master <cmd>` |
| `cli.py` | 无冲突（上游无此文件）；检查新脚本是否已映射 |
| `pyproject.toml` | 保留 fork 结构（version, tool.uv, tool.setuptools），只同步依赖 |
| `README.md` / `README_CN.md` | 接受上游内容后，在语言切换行与赞助商 `<details>` 块之间重新插入 fork 声明块（`Fork notice` / `Fork 声明`），声明赞助商与捐赠信息属于原作者、与本 fork 无关 |
| `update_repo.py` | 保留 fork 的 uv 功能，合入上游改进 |
| `skills/ppt-master/scripts/*.py` | 按 hunk/contract 审查：保留 fork marker/import，合入其他上游功能与安全修复；用聚焦测试和 F821 验证 |
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

### 手动恢复发布

```bash
gh workflow run publish-pypi.yml \
  -f release_sha=<exact-verified-main-sha> \
  -f release_tag=vX.Y.Z \
  --ref vX.Y.Z
```
workflow 会精确 fetch tag，确认 tag 指向 `release_sha` 且该 SHA 属于 `origin/main` 历史，
查询同一 SHA 的 successful push migration run，并重新运行所有 release gates；任一项失败都不
构建、不发布。直接手工 push 未经验证的 tag 不属于支持的发布入口。

### 自动发布

`auto-tag.yml` 只接受成功的 `Check UVX Migration` `workflow_run`，checkout 其 immutable
`head_sha`；tag 前重新 fetch `origin/main`，要求 release SHA 仍属于当前 main 历史并记录当时的
远端 tip，然后只推送指向该 exact SHA 的 `refs/tags/vX.Y.Z`。main 之后继续前进不会改变这个
immutable release SHA，也不会让已完成的发布失效。它不包含 manual trigger、auto-fix、main push
或 OIDC。tag 推送再触发 `publish-pypi.yml`；publish concurrency group 只绑定 canonical tag 与
release SHA，不含 event name。

publish workflow 的 build/gate job 没有 `id-token: write` 或 PyPI credential；只上传 wheel、
sdist 和严格 manifest。无 OIDC verifier 在 fresh runner 的 `runner.temp` 静态检查 hash、文件名、
metadata、归因文件和归档路径，并输出已验证的文件名与 SHA-256 摘要；只有成功依赖后才启动
`environment: pypi` 的 OIDC job。OIDC job 不 checkout、导入或执行 wheel，只在另一个 fresh
runner 重新核对完整 artifact 文件集合和摘要，再调用 `uv publish --trusted-publishing always`
发布确切路径。PyPI environment 的审批/分支保护必须在 workflow 集成后由维护者单独 bootstrap，
本任务不通过 API 激活远端保护。

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
| `.github/scripts/check_release_gates.py` | auto-tag/publish 共用的 immutable release gate 与 artifact verifier |
| `.opencode/command/sync-upstream.md` | OpenCode sync-upstream 命令定义 |
| `cli.py` | CLI 命令映射（根目录） |
| `skills/ppt-master/cli.py` | CLI 命令映射（skill 目录） |
| `skills/ppt-master/scripts/check_cli_sync.py` | CLI 映射完整性检查 |
| `skills/ppt-master/scripts/check_deps_sync.py` | 依赖清单一致性检查 |
| `skills/ppt-master/scripts/check_uvx_migration.py` | 合并提交中 python3 残留检测 |
| `skills/ppt-master/scripts/check_uvx_repository.py` | 基于 git ls-files 的只读 repository-wide UVX scanner |
| `docs/superpowers/specs/2026-06-08-uvx-refactor-design.md` | uvx 改造设计文档 |
| `docs/superpowers/2026-06-08-uvx-refactor-final.md` | uvx 改造最终笔记 |
