# 设计文档：同步确定性豁免

**日期**: 2026-09-22
**状态**: 用户逐节批准（方案 1 瘦版：类 A 确定性豁免 + 两步部署；谓词 B 暂缓）。第一步已部分落地（两个 cli.py 的 `spec-review`/`check-spec-annotations` 映射与两条 `.md` overlay 已随 fb478a93 sync 入库）；第二步类 A 豁免与 §2 abort 预检待实现
**来源**: GitHub Actions 运行 `35621828772`（Sync Upstream，workflow_dispatch）

## 背景

运行 `35621828772` 在 `prepare-candidate` 的 "Verify candidate on preparation runner" step 被 trusted checker 拒绝：

- base（fork main）= `ebfb6be1a7a93e9fa1d9a922a2b469e0955f12b2`
- target（上游目标）= `fb478a93cbb8d359dea33b75687a1bfdb6f504cd`
- candidate（模型产出的 merge tip）= `e244f8afd841acab67027d3dbb798066b94a0a36`

失败信息逐字如下：

```
##[error]The PR changes a protected CI gate file in commit e244f8afd841acab67027d3dbb798066b94a0a36
##[error]Preparation checker rejected the candidate
```

同一候选在内容边界判定处还会命中：

```
##[error]The sync merge does not carry the upstream content for the non-overlay path skills/ppt-master/scripts/docs/spec_review.md
```

两类死锁：

1. **cli.py 受保护**：target 在上游新增 `skills/ppt-master/scripts/spec_review/server.py`、`skills/ppt-master/scripts/check_spec_annotations.py` 两个带 `def main` 入口的脚本；fork 的 uvx 适配（同步文档 Step 4）要求在 `cli.py` 与 `skills/ppt-master/cli.py` 的 `COMMANDS`/`COMMAND_DESCRIPTIONS` 中登记新命令（`spec-review`、`check-spec-annotations`）。但两个 cli.py 同属 `check_upstream_ancestry.py` 的 `PROTECTED_PATHS`，`_assert_no_protected_changes` 逐 commit 扫描并 fail-closed。上游树中根本不存在这两个文件，所以这不是"上游改动触及保护文件"，而是"合并结果必须包含该改动才符合 fork 契约、保护检查却恒拒绝"。
2. **`.md` 内容边界**：上游新增 `skills/ppt-master/scripts/docs/spec_review.md`（非 overlay、非 diverged），并修改 `skills/ppt-master/workflows/stages/refine-spec.md`（fork 自 `upstream_base` 起未改动，同样是非 overlay、非 diverged）。两条路径都含 `python3 .../scripts/...` 调用，fork 契约要求改写成 `uvx ppt-master <command>`；`_verify_content_boundary` 对非 overlay 路径要求与 target 字节相等，对 diverged 路径要求不能静默保留 fork 内容。改写即偏离 target，不改写即违反 uvx 契约；唯一合法通道是在 `.github/upstream-overlay-paths.txt` 登记 overlay，而该策略文件本身也在 `PROTECTED_PATHS` 中。
   - 本次真正的 diverged 路径（fork 与上游自 `upstream_base` 起同时改动）是 `skills/ppt-master/scripts/README.md`、`skills/ppt-master/scripts/docs/image.md`、`skills/ppt-master/workflows/generate-pptx.md`、`skills/ppt-master/workflows/stages/resume-execute.md`；其中后两条已有 overlay 条目，无 overlay 的 diverged 路径是 `skills/ppt-master/scripts/README.md` 与 `skills/ppt-master/workflows/stages/resume-execute.md`（必须产出非 first-parent 的 reviewed resolution，本设计不改其语义）。

两类都是结构性死锁：候选每次重跑都做出同样的必要改动，检查器每次都同样拒绝，模型没有任何被允许的手段满足其中一侧；普通重跑必然失败。

本设计为类 A（受保护 cli.py 的确定性映射）引入**确定性豁免**：豁免不由 allowlist、人工标签或模型判断决定，而是由检查器从同一仓库的不可变 Git 对象重新计算期望；同一 `(base, target, merge)` 输入必然得到同一判定，因此重跑收敛而不是碰运气。`.md` 自动豁免（谓词 B）暂缓（见 §1 与 §5），本次的 `.md` 路径先由维护通道修复（见 §4 第一步）。

## 1. 类 A 豁免谓词（已批准，瘦版）

单一 owner 是 `.github/scripts/check_upstream_ancestry.py` 中的纯函数：

```
build_exemption_plan(repo, base_sha, merge_commit, target_sha) -> ExemptionPlan
```

- `first_parent` 指 strict merge commit 的 first parent（PR 模式即 trusted base）。
- 只读同一 repo 内的 Git 对象；不读工作树、环境变量、时钟或网络。
- 所有路径枚举按字节序排序；结果与调用顺序、locale、平台无关。
- 读取一律走 `git show <sha>:<path>` 的原始 bytes；`def main` 判定与其余 AST 判断都基于这些 bytes 解析，不做行尾、空白或文本规范化。
- AST 解析失败、对象缺失、entry 不是 regular blob 时 fail-closed，永不当成豁免。
- `ExemptionPlan` 只含 `cli_expectations[path]`（该文件相对 first-parent 允许新增的键、映射值与判定要求）与 `new_commands`；不做整文件字节合成，不含 `doc_expected`。
- 类 A 只作用于两个 cli.py 的保护扫描：它们永不出现在 `.github/upstream-overlay-paths.txt` 中，本设计不为策略文件新增任何条目，豁免不与 policy 条目竞争。
- `check_sync_candidate.py` 通过其既有 `_load_trusted_module` 加载 trusted `check_upstream_ancestry.py` 复用同一实现，不复制规则，也不新增 `.github/scripts` 模块（保持已批准的 trusted 模块集合）。

### 类 A 三要素判定

适用路径是代码内闭集：`cli.py`、`skills/ppt-master/cli.py`（两个 cli.py）；上游树中不存在这两个文件，基线因此是 **first-parent AST**，不是 target。

- **期望集**：`upstream_base..target`（`upstream_base = merge-base(first_parent, target)`）中状态为 `A`、路径匹配 `skills/ppt-master/scripts/**/*.py`、且满足 `check_cli_sync.find_scripts_with_main` 口径的脚本；计划构建在 Git 对象上等价实现该口径（AST 中存在 `def main` 函数，沿用其 `_` 前缀目录与 `__` 前缀文件跳过规则），不读工作树。只处理新增：删除或重命名 fail-closed。不自创 `if __name__ == "__main__"` 或其它入口口径。
- **只增不改**：候选每个 cli.py 的 AST 相对其 first-parent AST，只允许在 `COMMANDS` 与 `COMMAND_DESCRIPTIONS` 中新增条目；既有键、既有值、既有描述与文件其余 AST 一律原样保留（不重算、不修改；cli.py 自身 docstring 变更不豁免）。
- **两文件增量一致**：两个 cli.py 的映射增量必须完全一致——任一文件新增的每个 `COMMANDS` 条目，另一个文件必须以逐字相同的键和值新增同一条目；新增的 `COMMAND_DESCRIPTIONS` 条目在两个文件中都必须存在且非空。
- **新增值∈期望集**：每个新增 `COMMANDS` 条目的映射值必须逐字等于脚本相对 `scripts/` 的 POSIX 路径，且 ∈ 期望集；未知脚本值 fail-closed。
- **提示而非契约**：命令名与描述的取法只作为工作流提示，不做契约校验——kebab-case（basename 去 `.py`；词干恰为 `server` 时改用父目录名，如 `spec_review/server.py → spec-review`；`_`→`-`）与模块 docstring 派生描述仅供模型参考；检查器只要求新增键不与 first-parent `COMMANDS`、`ALIASES` 或彼此冲突（冲突 fail-closed），且新增描述非空。
- 描述非空即满足，不做"与 docstring 首行精确相等"的契约；不做整文件字节合成，也不要求候选 blob 与任何合成结果字节相等。

### 谓词 B：文档 uvx 渲染（暂缓，不实现）

`.md` overlay 自动豁免**暂缓**：`.md` 适配沿用"维护先行"，由维护通道补 overlay 条目（见 §4 第一步），本设计不实现文档豁免、不新增 boundary 文案。若该死锁复发率高，先把 `auto_fix_uvx.py` 的改写逻辑抽成无副作用纯函数（如 `auto_fix_uvx.render`）供 checker 复用，再重新提交该谓词。

## 2. 校验顺序与错误文案（已批准，瘦版）

顺序保持 fail-closed，豁免只收窄拒绝面，绝不短路既有门禁：

1. 既有结构门禁不变：base/head commit 存在、marker 转移、expected target、上游 ancestry、唯一双父 strict merge（`^1=base`、`^2=target`）、版本 bump（`--require-version-bump`）。豁免不适用于这些门禁。
2. strict merge 定位后、任何 protected/boundary 判定之前构建 `ExemptionPlan`；构建失败即整体 fail-closed。
3. `_assert_no_protected_changes`：`--allow-protected-changes` 语义不变。唯一新增容忍——commit 恰为 strict merge M，且该 commit 中被触及的 protected path 属于类 A 闭集，且对应的 cli.py blob 通过 §1 三要素判定。其他 commit、其他 protected path 沿用原文案：
   `The PR changes a protected CI gate file in commit {commit}`
   类 A 闭集内判定不匹配时使用新文案：
   `The protected CLI mapping file {path} in commit {commit} does not match the deterministic command mapping`（保留 commit 便于定位，与旧文案同格式）。
4. `_verify_content_boundary`：完全不改。policy 逻辑、diverged 与 non-overlay 分支、两条旧文案逐字不变；谓词 B 暂缓后没有文档豁免参与，boundary 不接受任何新文案。
5. `_verify_post_merge_drift`：不变。M..HEAD 仍要求每个 changed path 的 tree entry 与 M 完全一致；豁免路径同样冻结，不得在豁免之后继续改动。
6. 成功输出不变：boundary 与 protected 扫描的文案逐字保持现状；类 A 豁免不新增输出句，protected 扫描维持无输出。
7. 确定性保证：同一 `(base, target, M)` 重复构建 plan 必须得到同一判定（测试以独立进程验证）；判定不读取候选作者、提交消息、时间戳或任何外部状态。

### 工作流提前 abort 指引（约 30 行 shell 检测，不碰 checker）

`.opencode/command/sync-upstream.md` 在合并开始前执行的只读预检；命中即 abort（不尝试产出 candidate、不进入重跑循环），并打印维护流指引：

- 检测 1（cli.py 映射）：按 §1 期望集口径，从 Git 对象判断 first-parent 两个 cli.py 是否缺少本次上游新增入口的映射，并判断其能否被类 A 确定性覆盖——缺失脚本 ∈ 期望集且新增键不与既有 `COMMANDS`/`ALIASES` 冲突时可继续（在两个 cli.py 中确定性补条目）；否则 abort，提示走维护流（两个 cli.py 属保护集，普通同步 PR 无法修改）。类 A 落地前（§4 第二步之前），任何 cli.py 缺失映射都按 abort 处理。
- 检测 2（.md 适配）：枚举本次上游修改的 `.md` 路径，过滤出"含需改写调用（`python3`/`python`/`uv run` + scripts 调用）且 `policy=None` 且非 diverged（fork 自 `upstream_base` 起未改动）"的路径；命中即 abort，提示维护流补 overlay 条目（本次的 `spec_review.md`、`refine-spec.md` 即此类）。
- 形态与边界：约 30 行 shell，只读 `git merge-base`/`git diff`/`git show` 与 overlay 策略文件；不新增 Python 模块、不修改任何 checker、不发网络请求。

## 3. 测试（已批准，瘦版）

目录：`skills/ppt-master/scripts/tests/`。

- `test_check_upstream_ancestry.py`：
  - 类 A 正向：base 含两个 cli.py（保留它们真实存在的文件头差异），target 新增带 `def main` 的脚本（含 `spec_review/server.py` 式 server 目录形态），merge 在两个 cli.py 中以相同键值新增 `COMMANDS`/`COMMAND_DESCRIPTIONS` 条目（描述非空）→ protected 扫描与 boundary 通过；
  - 类 A 负向：新增描述为空、新增值指向非本次新增脚本、改动既有条目、cli.py 自身 docstring 或其余 AST 被改、只在一个 cli.py 增条目（增量不一致）、非 merge commit 触及时分别命中新文案或旧文案；
  - 提示而非契约：fixture 用不符合 kebab-case 提示但无冲突的新键仍通过，锁定"命名推导只做提示"的口径；
  - 兼容：既有断言 `protected CI gate file` 与两条 boundary 文案保持逐字不变；无豁免参与的旧用例全部保持通过；
  - 确定性：同一 fixture 在两个独立子进程中构建 plan，断言判定相同；AST 不可解析的 fixture fail-closed；
  - 复现运行 `35621828772` 的双死锁 fixture：用本地合成的 commit 对象（两个 cli.py、`spec_review.md`、`refine-spec.md`、新增 `def main` 脚本），不依赖真实上游对象，不发网络请求；
  - 合成未来事件的 fixture：模拟"下一次上游新增 `def main` 脚本 + 候选只增确定性映射"，断言类 A 豁免被实际触发（与 §4 验收对应）。
- `test_sync_candidate.py`：trusted candidate 关卡复用同一 plan——豁免范围内的 cli.py 改动通过，额外 protected 改动仍按原文案拒绝。

## 4. 部署与回滚（已批准，两步）

- 第一步（立即，trusted maintenance 通道，约 12 行）：为本次两个上游新增入口（`spec-review`、`check-spec-annotations`）在两个 cli.py 中补齐 `COMMANDS`/`COMMAND_DESCRIPTIONS` 映射，并在 `.github/upstream-overlay-paths.txt` 增加两条 overlay：`skills/ppt-master/scripts/docs/spec_review.md` 与 `skills/ppt-master/workflows/stages/refine-spec.md`；同时把 `.opencode/command/sync-upstream.md` 的 Step 4 产出要求同步为类 A 契约口径，并落地 §2 的 abort 指引。这些文件都在保护集中，普通同步 PR 无法修改，必须走维护通道。
- 第二步（随后，同维护通道）：瘦版类 A 豁免落盘——`.github/scripts/check_upstream_ancestry.py`、`.github/scripts/check_sync_candidate.py` 及 `skills/ppt-master/scripts/tests/test_check_upstream_ancestry.py`、`skills/ppt-master/scripts/tests/test_sync_candidate.py`。落盘后未来上游新增 `def main` 脚本时，候选可确定性增补两个 cli.py 映射而不触发 protected 门禁。
- 备选 (d) 未采纳：不进入实现与验收范围，仅记录决策。
- 豁免本身是纯计算，无持久状态、无缓存文件；落盘由上述维护 PR 完成，豁免机制不自行创建分支或 PR，也不修改 overlay 策略条目。
- 验收：以最新 `main` 为 base 重新 dispatch `Sync Upstream`（target 仍为 `fb478a93cbb8d359dea33b75687a1bfdb6f504cd`），确认 candidate 通过 preparation checker，且 PR 检查与 preparation 命中同一豁免判定；并加"合成未来事件"验证——用本地 fixture 或受控分支模拟一次上游新增 `def main` 脚本的未来运行，确认类 A 豁免在该路径上被实际触发、判定与测试一致，不依赖真实上游对象、不发网络请求。
- 回滚：以同样 maintenance 通道 revert 上述 checker/tests 提交。回滚后下一次运行恢复原有 fail-closed 行为，已合并 PR 不受影响；禁止用 force-push、rebase 或删除分支代替回滚。

## 5. 非目标（已批准，瘦版）

- 谓词 B 暂缓：本设计不实现 `.md` overlay 自动豁免；`.md` 适配沿用维护先行，需要时由维护通道补 overlay 条目。重提前置条件：先把 `auto_fix_uvx.py` 的改写抽成可复用的纯函数（如 `auto_fix_uvx.render`）。
- 不新增 allowlist 或豁免配置文件，不新增按目录、glob 或路径前缀直接豁免的规则，不引入人工批准标签。
- 不放宽 `PROTECTED_PATHS` 的常规语义：普通 PR 仍不能修改 protected 文件；两个 cli.py 仅在 sync merge 中、且通过类 A 三要素判定时被容忍；其他 protected 路径（含 overlay 策略文件自身）不豁免。
- overlay 策略仍全部人工维护：本次两条 `.md` overlay 条目由第一步维护 PR 人工加入；既有 `.py` overlay 条目（如 `confirm_ui/server.py`、`svg_editor/server.py`）继续人工审阅，本设计不自动新增或修改任何 overlay 条目。
- 不扩展为通用改写引擎：不做重命名、删除、目录、非 `.md`、`.github/**`、`projects/**` 路径；不重算既有命令条目或描述。
- 不改变 ancestry、marker、strict merge、drift、版本 bump 与发布门禁的语义和文案；不修改 `.github/workflows/**`。
- 不处理与本次死锁无关的残余风险（如上游 SSRF、SVG sanitizer），不触碰上游仓库，不新增 secret、权限或推送路径。

## 决策记录

- 2026-09-22：用户确认走方案 1（确定性豁免），对 §1–§5 逐节批准。
- 2026-09-22（瘦版修订）：用户批准瘦版——删除谓词 B（暂缓，待 `auto_fix_uvx.render` 纯函数抽离后再重提）；类 A 收敛为只增不改 + 两文件增量一致 + 值∈上游新增 `def main` 脚本（复用 `find_scripts_with_main` 口径）+ 描述非空，命名推导仅作提示、不做契约，不合成整文件字节；新增工作流提前 abort 指引；部署改为两步（先维护 PR，后类 A 豁免）；备选 (d) 未采纳。修正背景分类：`refine-spec.md` 非 diverged；无 overlay 的 diverged 路径为 `skills/ppt-master/scripts/README.md` 与 `skills/ppt-master/workflows/stages/resume-execute.md`。
