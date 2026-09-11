# 设计文档：上游同步提交关系修复

**日期**: 2026-09-10
**状态**: 已确认，Task 5A/5B 实现后待复审
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
`EXPECTED_UPSTREAM_SHA`。失败日志可打印安全的提交图和状态，但不得回显 marker
原始内容。

## 4. GitHub Actions 边界

### 4.1 统一触发与并发边界

`.github/workflows/sync-upstream.yml` 的 schedule 和 workflow_dispatch 共用一个固定
`sync-upstream` concurrency group，`cancel-in-progress: false`。workflow_dispatch 只允许
`refs/heads/main`；无变化时两个 job 的候选、artifact、分支和 PR 路径全部跳过。

### 4.2 Job 1：Prepare Candidate

`prepare-candidate` 只声明 `permissions: contents: read`，从 `${{ github.sha }}` 以完整历史检出
并验证 `HEAD` 等于 immutable base SHA。由于 `actions/checkout` 的 `token` input 是 required，
空 token 会使 action 失败，而传入 `github.token` 又违反模型 job 的 authenticated-checkout 边界，
实现采用 canonical public URL 的 `git init` + 无 `--depth` fetch（等价 `fetch-depth: 0`）+ detached
checkout；它不写 credential，因此等价并强于 `persist-credentials: false`。它明确添加并 fetch canonical
`https://github.com/hugohe3/ppt-master.git`；fetch 错误直接失败，不使用 stale `upstream/main`。
只配置 `github-actions[bot]` 的 Git author identity。

该 job 只执行一个固定的 `opencode-ai@1.18.30` CLI 路径。模型环境只包含
`DEEPSEEK_API_KEY`、模型选择、`EXPECTED_UPSTREAM_SHA` 和普通 Actions 元数据；不得出现
`github.token`、`GITHUB_TOKEN`、`GH_TOKEN`、`PUSH_PAT`、`id-token: write` 或任何 job-level write
permission。模型只能修改并提交工作树，不能 push、建 PR、改写历史或接触 repository credential。

模型退出后，job 1 使用 base checkout 快照的 helper 做 defense-in-depth 检查：clean worktree、
严格 marker blob、目标上游 ancestry、唯一双父 merge（`^1=base`、`^2=target`）、版本 bump 和
protected gate policy。它创建 `candidate.bundle`，仅包含 candidate ref 相对 base 以上所需 Git
objects；它不归档 worktree、`.git/config` 或模型生成的可执行文件。

### 4.3 Job 2：Verify And Open PR

`verify-and-open-pr` 在 fresh runner 上运行，只声明 `contents: read` 和 artifact 下载所需的
`actions: read`。它从 Job 1 输出的 base SHA 检出 trusted base，下载固定 artifact 名称后只把
bundle 和 manifest 当作不可信 Git object data。它不 checkout candidate，不运行 candidate 中的
workflow、helper、shell、配置、hook、action 或后台进程。

manifest 必须是 UTF-8 JSON 且键集合严格为 `base_sha`、`target_sha`、`verified_sha`；三个值必须
是 40 位小写 SHA，并分别等于 trusted job outputs。bundle 必须通过 Git bundle 校验，导入固定
candidate ref 后 ref tip 必须等于 `verified_sha`，且 base、target、candidate commit objects 都
存在。随后 trusted base helper 再次 fetch canonical upstream，并以 base copies 验证 marker、
上游历史、candidate ancestry、严格 parent order、唯一 merge、版本形状和完整 protected set。

固定 CLI 的安装是独立且无 secret 的 step，只有后续 `opencode run` step 获得
`DEEPSEEK_API_KEY`。发布前 trusted job 重新读取 `origin/main`，要求仍等于 base；立即重新读取
candidate ref，要求仍等于 authoritative `verified_sha`。只有最后的 publication step 才注入
`PUSH_PAT`，并显式设置 null global/system Git config、`GIT_TERMINAL_PROMPT=0`、安全的
`GIT_ASKPASS/SSH_ASKPASS` 和每条 Git 命令的空 `credential.helper` 与 `/dev/null` hooks。

它将明确 SHA 推送到 `opencode/sync-<run-id>-<attempt>`，绝不推送 `main`，再用同一 PAT 创建
`elvisw/ppt-master` 指向 `main` 的 PR。创建后立即用 REST `gh api` 的 `--jq '.base.sha'` 读取该
PR base SHA，要求严格等于 base；若创建失败、main 在 push/create 间前进或 base SHA 不匹配，EXIT trap 会尽力
关闭 PR（若已有编号）并删除刚推分支，然后返回失败。GitHub API 检查不是原子操作；最终仍由
trusted `pull_request_target` 的 strict first-parent/required checks 阻断错误 PR。任何
non-fast-forward、manifest/object/ref mismatch 都 fail-closed，不 rebase、不 rewrite、不 force-push。

### 4.4 Trusted candidate gates

trusted runner 在 base checkout 中以 `git worktree add --no-checkout` 加 `read-tree` 把 candidate
materialize 到隔离临时目录，只当作数据读取。base 版本的 `check_sync_candidate.py` lstat
关键文件、拒绝全部 symlink，并以 AST/text/digest 逻辑检查 root/Skill 的 `COMMANDS` 与 `ALIASES`
映射、依赖与双 `uv.lock`、attribution frontmatter/marker/license digest/files、MANIFEST 与
README/FAQ/roadmap/CONTRIBUTING/PyPI notices/sponsors、8 个 fork Python marker/import 合同和
YAML 语法；它绝不 import 或执行 candidate 脚本。候选版本还由 base 的 `check_uvx_migration`
逻辑读取检查，不能用 candidate 的同名 checker 替换。

trusted job 安装固定的 PyYAML/Ruff gate tooling 后，只对明确 lstat 为 regular 的 8 个 fork
Python 绝对路径执行 trusted `python -I -m py_compile` 与 `ruff check --isolated --select F821`，
并清空 `PYTHONPATH`、使用 trusted cwd。所有结构、语法和 F821 gate 都在 PAT step 之前失败即止。

### 4.5 Trusted-file policy

`.github/scripts/check_upstream_ancestry.py` 的 protected set 覆盖整个 `.github/workflows/` 目录、
`.github/pull.yml`、
trusted helper、`.opencode/command/sync-upstream.md`、`check_cli_sync.py`、`check_deps_sync.py`、
`check_uvx_migration.py`、`attribution_guard.py`、`auto_fix_uvx.py` 以及作为这些 gate executable
specification 的 focused tests。普通 sync PR 修改任一文件都失败；维护这些文件必须走显式 trusted
maintenance/bootstrap 流程。Python 同步策略按 hunk/contract 保留 fork marker/import，并合入
其他上游功能和安全修复，不再使用“整文件零 diff” allowlist。

独立的 `.github/workflows/opencode.yml` 保持原有 issue/review-comment 触发和授权者 guard，
但 action 固定为 `anomalyco/opencode/github@77fc88c8ade8e5a620ebbe1197f3a572d29ae91a`。
其 checkout 只保留 `contents: read`；action 未显式 `use_github_token: true`，当前实现依赖
OIDC，因此保留唯一必需的 `id-token: write`，删除 `contents/pull-requests/issues: write`。

### 4.6 Bootstrap boundary

首次把该 trusted workflow/helper 部署到 main 的 PR，其 base 可能尚无当前 gate，因此不能声称该
PR 已被新 workflow 自身保护。Task 5A 的安全性由人工 diff 审查、独立 unit/YAML/CLI/deps/
attribution 检查和真实临时 Git/artifact 沙盘证明。部署到 main 后，protected gate 文件只能通过
trusted maintenance/bootstrap 流程修改。

### 4.6.1 Task 5A 发布边界（历史，由 Task 5B 取代）

Task 5A 不修改 release workflows；现有 `check-uvx-migration`、`auto-tag` 和 `publish-pypi` 的
ancestry/release owner 继续由 Task 2/5B 管理。同步 PR 进入 `main` 后才由既有发布链处理。

### 4.7 Task 5B 不可变发布链

Task 5B 将发布边界收敛为一个 trusted、只读的
`.github/scripts/check_release_gates.py` owner。`auto-tag` 与 `publish-pypi` 都必须在
精确 release SHA 上调用它；它依次验证 marker/type/SHA/upstream ancestry、完整 root/Skill
`COMMANDS` 与 `ALIASES` 映射、依赖与双 lock、repository-wide tracked-file UVX 扫描、归因/许可证
摘要/sponsors/frontmatter/manifest、八个 fork marker/import 与 `py_compile`/Ruff F821、workflow
YAML/action pin/protected policy，以及严格版本/tag 语法。`check_uvx_migration.py` 的 exit 2
只能是提示，不能替代 immutable successful workflow-run API 证据。

`auto-tag` 只接受同一仓库 `Check UVX Migration` 的 completed/successful `push` run，且
`head_sha`、`head_branch=main`、workflow path 与 repository 全部精确匹配；它 checkout 该
`head_sha`，tag 前重新 fetch `origin/main` 并要求 release SHA 仍是当前 main 历史的一部分，
记录当时的远端 tip，但不再要求 main tip 等于 release SHA（main 后续前进不会改变已发布的
immutable SHA）。它只用 PAT 推送 `refs/tags/<tag>`，绝不修复文件或推送 `main`。版本由 trusted
TOML 解析写入环境并以安全的 canonical grammar 绑定为 `v<version>`。

三个 privileged workflow 都用 `python -I` 调用 release gate，release gate 在 import 前把自身
`.github/scripts` 目录移出 `sys.path`，并验证该目录只含三个 approved trusted module，因此
同目录的 `yaml.py`/`console_encoding.py` 等 shadow 无法以 `SystemExit(0)` 冒充通过。scanner 将
`console_encoding` import 延迟到 `main()`，`PROTECTED_PATHSPECS` 覆盖整个 `.github/scripts/**`，
`PROTECTED_PATHS` 还覆盖 `console_encoding.py`、`workflow_transcript.py` 和两份 `cli.py`。

`check-uvx-migration.yml` 的 job/step 集合被 release policy 精确验证：只有 check job、四个固定
step、无 step-level `if`/`continue-on-error`，ancestry/migration 命令必须逐字等于 approved run
模板的 fail-closed 形式；替换为 no-op、`exit 2`、前置 `exit 0`/`true`/`set +e`、`|| EXIT=0`、
重命名、删除、重排或额外步骤都会失败。同一 policy 对 auto-tag 与 publish OIDC job 执行完整
step inventory：名称、顺序、key 集合、`uses`/`with`/`env` 与 run block 全部 exact；三个
privileged workflow 的 run block 统一拒绝 `git add/am/apply/cherry-pick/commit/merge/rebase/
reset/update-ref`，auto-tag 只允许一个 approved exact tag push（publish/migration 为 0），
`git -c x=y commit`、`git push ...refs/heads/main` 和 force 变体都 fail-closed。OIDC job 的 run
禁止 pip/python/import/exec/eval/uvx/`.whl`，`WHEEL_PATH`/`SDIST_PATH` 只能在 approved
bind/publish step 使用。

wheel 静态检查要求 metadata 精确位于 `ppt_master-{version}.dist-info/METADATA` 且同目录存在
`WHEEL`/`RECORD`；Core Metadata 字段按 ASCII case-insensitive 归并，`Name`/`Version` 折叠后
各恰好一个，重复大小写变体、body spoof 与非 ASCII 字段名都拒绝。CLI mapping parser 拒绝所有
后续 Store 绑定（For/With/ExceptHandler/comprehension/NamedExpr 等）与 `exec`/`eval`/
`globals`/`locals`/`setattr` 动态逃逸，scanner 复用同一个 trusted parser。repository scanner 的
legacy grammar 覆盖 `python.exe`/`python3.12`/`python ./scripts` 与 `uv run` 中间 flags，扫描根
包含 `.claude-plugin/` 与 `projects/`。release gate、candidate 与 migration checker 的全部
`text=True` Git/subprocess 调用显式使用 `encoding="utf-8", errors="replace"`。所有 privileged
workflow 的 workflow/job key 另以白名单钉死：workflow 层拒绝 env/defaults/container/services，
job 层禁止任何未批准的 `env`（含 BASH_ENV）、`defaults`、`container`、`services` 和多余 `if`；
auto-tag 的唯一 job 必须保持 exact condition 与 runner/timeout。publish 的 build-and-gate 与
verify-artifact 也改为完整 step inventory + exact run/with/env 合同，因此无凭据的构建与静态验证
job 也自证只执行 approved 步骤。Core Metadata 字段名还必须满足 `field == field.strip()`，
colon 前不得出现 space/tab。CLI parser 除 Store 绑定与动态逃逸外，还拒绝把 COMMANDS/ALIASES
作为任意未批准 Call 的 positional/keyword 参数，拒绝 `getattr`/`operator.setitem`/
`builtins.__dict__[...]` 等间接调用，同时仍允许 `sorted(COMMANDS)`、`.get()`、`.keys()` 等只读读取。

`publish-pypi` 的 tag push 与 manual recovery 都必须提供/推导相同的 release SHA/tag，精确
fetch tag ref，确认 tag 指向 SHA 且 SHA 在 `origin/main` 历史中，再查询同一成功 migration run
并重新运行完整 gate。workflow concurrency group 只由 canonical tag 和 release SHA 组成，不含
event name。build/gate job 没有 OIDC；它用固定 version + Ubuntu x86_64 可执行文件 checksum 的
`setup-uv`，以 exact pin 安装 PyYAML/Ruff 和 build backend（`setuptools`），并执行
`uv build --no-build-isolation`，不解析任何 latest/range。静态检查用 Core Metadata header parser
要求 `Name`/`Version` 各恰好一个（拒绝 body spoof 与 duplicate）；它只把一个 wheel、一个 sdist
和严格的 SHA-256/release identity manifest 交给后续 jobs。无 OIDC 的 fresh verifier 先静态检查
文件名、hash、metadata、归因文件和归档路径，并输出已验证的文件名与 SHA-256 摘要；只有其成功
依赖之后才启动 `environment: pypi` 的 OIDC publish job。该 job 的 `setup-uv` 同样固定 version +
checksum，不 checkout 或执行包代码，只在另一个 fresh runner 下载同一 immutable artifact，重新
核对完整文件集合和 verifier outputs 的摘要，再对确切路径调用 `uv publish --trusted-publishing
always`。

所有 privileged workflow action 都以 GitHub API 解析到的 40 位 commit SHA 固定，并在 YAML
旁标注 upstream release；workflow-run API 返回值也作为 report 证据保留。上游 SSRF redirect/
DNS-rebinding 与 SVG SMIL sanitizer 风险仍是显式 out-of-scope residual，本次不宣称修复。

### 4.7.1 内容边界 owner 与 overlay policy

`check_upstream_ancestry.py` 同时是内容边界验证的唯一 owner：先定位严格 merge commit `M`
（PR 模式由 `--base-sha..--head-sha` 唯一双父 merge；main/release 模式按 second parent 等于
记录的 upstream target 在 HEAD 历史中唯一查找，不把 version-bump HEAD 当身份）。随后计算
`upstream_base = merge-base(M^1, target)`，用 `git diff --name-only -z --no-renames` 枚举
`upstream_base..target` 的全部 changed path（add/delete/mode/symlink 都展开，rename 展开为
delete+add）。非 overlay path 的 `M` tree entry（mode/type/object 或 absent）必须与 target
严格相等；overlay path 若 `M^1` entry 与 target 不同，则默认要求 `M` entry 不等于 `M^1`
entry（拒绝 `-s ours` 或事后整文件回滚），只有 policy 中以 `retain-base` 逐路径标注并给出
理由时才允许保留 first-parent 内容。overlay 清单是精确文件路径，禁止目录、glob 与重复项；
`M^1` 已等于 target 时 overlay 也必须等于 target。

清单文件 `.github/upstream-overlay-paths.txt` 受 protected set、candidate trusted closure 与
release chain 保护；trusted PR helper、两个 sync job、main gate 与 release chain 全部调用
同一 helper。当前 repair merge `1fcf7154` 的清单为 24 个精确路径（21 个 `merge`、3 个
`retain-base`），随本次提交审阅落地。

additional trusted workflows（`sync-upstream.yml` 的 upload/download artifact、
`check-upstream-ancestry.yml` 与 `opencode.yml` 的 checkout、`opencode.yml` 的
`anomalyco/opencode/github`）也纳入 action pin 校验；部署 GitHub Pages 不在本仓库范围内，
仍列为 out-of-scope residual。

### 4.8 仓库合并设置与落盘方式

通过 fork API 关闭 `allow_squash_merge` 和 `allow_rebase_merge`，保留
`allow_merge_commit=true`，并在修改前后读取 API 回执。该设置只作用于
`elvisw/ppt-master`，不触碰上游。

repair 分支如创建 PR，必须使用 `gh pr merge --merge`；不得使用 squash 或 rebase。
Task 5A 的同步 publication 只创建唯一分支到 `main` 的 PR，不直接写 `main`；合并后必须
fetch `origin/main` 并在远端主分支上重跑 ancestry 门禁。Task 5A 不修改仓库合并设置。

## 5. 当前历史修复与执行目标更新

原始设计中的 repair target 是 `64b65839`；由于 upstream 在实际执行前已推进，用户批准
将本次 Task 4 的 immutable replacement target 更新为
`09ad58f0d58decc9d30799ca83374ff2604ef16b`。在已包含设计和实现提交的
`fix/sync-upstream-ancestry` 分支上执行：

```bash
git merge --no-ff --no-commit 09ad58f0d58decc9d30799ca83374ff2604ef16b
```

保留 fork 的 `svg-pipeline.md` uvx 适配和所有现有 fork marker，审查合并树后创建
真实 merge commit，并把该 SHA 写入 `.github/upstream-main.sha`。该提交用于记录
`09ad58f0d58decc9d30799ca83374ff2604ef16b` 的 ancestry；不通过 reset、rebase、squash 或
cherry-pick 重建。merge commit 完成后，再以独立单父提交将两个 fork 包版本 bump 到
动态计算的下一个未占用 patch 版本。

历史修复后，本地 `HEAD` 必须满足：

```bash
git merge-base --is-ancestor 09ad58f0d58decc9d30799ca83374ff2604ef16b HEAD
git log HEAD..09ad58f0d58decc9d30799ca83374ff2604ef16b --oneline
```

第一条返回 0，第二条无输出。修复只在本地分支提交，不自动推送。

下一次 schedule 在 repair merge 到达远端 `main` 前仍会把 `09ad58f0` 识别为缺失；
这期间若产生冗余同步 PR，应关闭而非合并。repair 落地后检测自然恢复为空。

## 6. 错误处理

- `git merge --no-ff --no-commit` 冲突无法安全解决时执行 `git merge --abort` 并停止。
- ancestry、manifest、bundle、candidate ref 或 main-tip 门禁失败时不尝试用内容比较替代，不发布，不创建有效同步结论。
- schedule 和 workflow_dispatch 都只读 canonical upstream；trusted publication 只写 fork 的唯一同步分支。
- PAT 仅在 trusted runner 的最终 publication step 注入，且只在 OpenCode 成功退出、对象验证和 ancestry 门禁全部通过后执行。
- main 前进或 publication non-fast-forward 时停止并从最新 main 重跑，禁止 force-push、rebase 或 rewrite。
- PR 被 squash/rebase 的入口通过 fork 仓库设置关闭；main 门禁继续作为发布兜底。
- 现有 attribution、CLI sync、F821、依赖与 fork marker 任一门禁失败时仍按原流程阻断。

## 7. 验证计划

1. 静态审查提交关系规则由 `.opencode/command/sync-upstream.md` 所有，单一 pinned CLI prompt 只传入目标 SHA、指向所有者并保留必要的 CRITICAL 门禁。
2. 对历史故障提交 `726c386b` 和本次 replacement target `09ad58f0` 运行 ancestry 检查，确认负向场景失败。
3. 在临时 clone/branch 沙盘执行完整的 Step 2→6，验证 `MERGE_HEAD`、双父 merge、目标文件和独立版本提交。
4. 对 repair 分支最终 `HEAD` 运行 ancestry 检查，确认通过且目标缺失列表为空。
5. 验证 repair merge commit 恰好有两个父节点，其中包含实现分支原 HEAD 和 `09ad58f0`。
6. 校验 sync workflow YAML 可被解析，检查统一 schedule/manual 路径、concurrency、只读权限、固定 CLI、artifact 条件、trusted helper 和 verify-before-publication 顺序。
7. 验证 PR check 的三种输入：目标文件不变时跳过、合法 repair 通过、篡改目标或单父伪 merge 失败。
8. 验证 main 发布门禁对 repair HEAD 通过、对仅更新目标文件但没有 ancestry 的构造提交失败。
9. 读取 GitHub API 确认只允许 merge commit；未来 repair PR 使用 merge 模式，落地后 fetch 并在 `origin/main` 复验目标 ancestry。
10. 运行现有 `check_cli_sync.py`、attribution guard、fork marker、`py_compile`、Ruff F821 和依赖同步门禁。
11. 运行独立 workflow contract、manifest/bundle artifact、protected-file、fetch fail-closed、dangling symlink 和跨平台 Bash discovery 测试；审查 `git diff`、`git log --graph` 和工作区状态，确认未夹带无关改动。

## 8. 涉及文件

- `.opencode/command/sync-upstream.md`
- `.github/workflows/sync-upstream.yml`
- `.github/scripts/check_upstream_ancestry.py`
- `skills/ppt-master/scripts/tests/test_check_upstream_ancestry.py`
- `skills/ppt-master/scripts/tests/test_sync_upstream_ownership.py`
- `skills/ppt-master/scripts/tests/test_sync_upstream_workflow.py`
- `docs/zh/upstream-sync.md`
- `docs/superpowers/specs/2026-09-10-sync-upstream-ancestry-design.md`
- `docs/superpowers/plans/2026-09-10-sync-upstream-ancestry.md`
- `.github/scripts/check_release_gates.py`
- `skills/ppt-master/scripts/check_uvx_repository.py`
- `skills/ppt-master/scripts/tests/test_check_cli_sync.py`
- `skills/ppt-master/scripts/tests/test_release_gates.py`
- `skills/ppt-master/scripts/tests/test_release_workflows.py`
- `skills/ppt-master/scripts/tests/test_uvx_repository_scan.py`
- `.github/workflows/auto-tag.yml`
- `.github/workflows/publish-pypi.yml`
- `.github/workflows/check-uvx-migration.yml`

Task 5A 不修改版本或 Task 3/4 历史；Task 5B 只收紧 fork release workflows，不修改上游代码文件模拟
ancestry，也不执行远端 tag、push、PR、publish 或 ruleset/environment activation。
