# PyPI 历史版本清理实施计划

> 执行者须使用 `executing-plans` 或 `subagent-driven-development`，逐项实施并保留验证证据。本计划尚未授权代码实现或实际删除。

**目标：** 每月生成只读清理清单，由维护者在本机交互登录后确认并删除旧版本。

**架构：** 一个仓库维护脚本拥有保留规则及报告接口；独立 Action 只调用报告接口。本机删除入口复用规则，并调用固定版本的 `pypi-cleanup`，不自建认证客户端。

**技术栈：** Python 3.12、`packaging==25.0`、`pypi-cleanup==0.1.12`、`unittest`、GitHub Actions、uvx。

**设计依据：** [已认可方案及实施选择](../specs/2026-10-18-pypi-cleanup-design.md)。

## 全局约束

- 固定目标 `ppt-master`；保留最新 10 个非空版本、最近 30 天有文件上传的版本，以及 `info.version` 默认版本。
- 按 PEP 440 版本排序；UTC 时间；30 天边界包含等于；每个版本使用最新文件上传时间。
- 不改包版本、包依赖、skill CLI、发布链或同步链。不存储密码、验证码或认证会话。
- Action 只读；实际删除仅在本机交互终端确认后执行；首次真实清理另行确认具体清单。
- 测试只在 `skills/ppt-master/scripts/tests/`，使用 `unittest` 与模拟网络。
- GitHub 操作只指向 `elvisw/ppt-master`；实现走分支与 PR，不自动合并。

## 重点验证

1. `0.1.10` 排在 `0.1.9` 前；预发布版本按 PEP 440 处理。
2. 某版本后来追加新文件时，最新上传时间保护整个版本。
3. 无文件、无候选、异常元数据均不触发认证或删除。
4. 确认后发生新发布或追加文件，不扩大已确认删除范围。
5. 删除工具部分失败或公开查询仍有缓存时，不误报全部完成。

## 文件布局

- 新增 `.github/scripts/pypi_cleanup.py`：查询、规则、报告、本机执行及核验。
- 新增 `.github/workflows/pypi-cleanup-report.yml`：月度与手动只读报告。
- 新增 `skills/ppt-master/scripts/tests/test_pypi_cleanup.py`：脚本回归测试。
- 新增 `docs/zh/pypi-cleanup.md`：维护者操作、认证限制及失败恢复。
- 修改 `docs/pypi-publishing.md`：仅添加指向维护说明的链接。

## 实施前门槛

- [x] 建立独立计划分支 `docs/pypi-cleanup-plan`。
- [x] 同步至远端 `origin/main`：`0443ee7820ecf94d55afd6b6f2aee1dd4a178ad1`。
- [ ] 评审本计划与设计，并由用户批准实施方式。
- [ ] 实施开始时重新核实最新基线、适用 AGENTS.md 与保护规则。

## 任务 1：共享规则与报告入口

**文件：** `.github/scripts/pypi_cleanup.py`、`skills/ppt-master/scripts/tests/test_pypi_cleanup.py`。

**接口：** `fetch_metadata() -> dict` 固定查询公开项目 JSON；`build_report(metadata: dict, *, now: datetime) -> dict` 为纯函数；`main(argv: list[str] | None = None) -> int` 提供 `report` 子命令。

报告顶层字段固定为 `project`、`generated_at`、`keep_latest`、`keep_days`、`total_bytes`、`reclaim_bytes`、`keep`、`delete`。两个列表中的条目含 `version`、`size_bytes`、`latest_upload`。支持 `report --json <path> --markdown <path>`；任一输出参数均可省略，无输出参数时向 stdout 输出 Markdown，诊断到 stderr。

- [ ] 先写规则失败测试：12 个超过 30 天的版本只保留最大 10 个；另有旧版本的新文件时额外保留；截止时间相等时保留；少于 10 个全保留；空文件列表不计入名额；默认版本始终保留。
- [ ] 补充异常测试：无法解析的版本、时间缺失或无时区、负文件大小、项目名不符均失败；JSON 或 HTTP 异常不生成成功报告。`--help` 无网络与写文件副作用。
- [ ] 在 scripts 目录运行单模块，记录预期失败：`uvx --from packaging==25.0 python -m unittest tests.test_pypi_cleanup`。不使用测试 discover。
- [ ] 实现纯规则与报告；公开查询设置超时，不读取认证配置。新脚本是仓库维护工具，不加入两个 skill CLI 映射。
- [ ] 重跑相同单模块至通过；在线运行报告一次，核对实际 PyPI 版本数、容量与候选清单，保存真实输出但不将过时快照作为测试夹具。
- [ ] 提交规则、报告及测试：`feat: add PyPI cleanup reporting`。

## 任务 2：本机交互删除入口

**文件：** 同一维护脚本与同一测试模块。

**接口：** `build_cleanup_command(versions: list[str], username: str) -> list[str]` 构造参数数组；`verify_cleanup(before: dict, after: dict) -> dict` 的字段为 `deleted`、`remaining`、`missing_kept`，各为版本字符串列表，分别表示已删除、未删除及意外丢失的保留版本；`main` 增加 `apply --username <name>`。

- [ ] 先写失败测试：Action 环境或非交互终端拒绝；空清单不启动子进程；确认文本不等于 `DELETE ppt-master` 时停止；候选正则只精确匹配已确认版本，包括正则特殊字符转义。
- [ ] 补充重新查询测试：候选被新文件或保留规则保护时停止并要求重新预览；不把新候选加入已确认集合。模拟部分删除、验证查询失败、工具非零退出，均非零退出且列出未完成项。
- [ ] 运行任务 1 的单模块，确认新增测试先失败，再实现入口。显示完整候选列表及容量后读取确认文本，随后重新查询核验候选仍可删除。
- [ ] 子进程使用 `uvx --from pypi-cleanup==0.1.12 pypi-cleanup`，传递固定 `--package ppt-master`、`--username`、精确锚定的 `--version-regex`、`--days 30`、`--yes`、`--do-it`。禁止 `--delete-project`、宽泛正则、shell 字符串执行或 debug 模式。
- [ ] 继承本机终端以便工具交互认证；移除子进程环境中的 `PYPI_CLEANUP_PASSWORD`，且始终传用户名，避免通过 `.pypirc` 自动读密码。入口不接收或保存密码、验证码。
- [ ] 删除后最多每 5 秒重新查询一次，共 6 次，只重试 GET，不重试删除。候选未完全消失、查询失败或保留版本意外丢失均非零退出，分别报告，不把工具退出 0 当成成功证据。
- [ ] 重跑单模块；本机只验证 `apply --help`、无候选路径及拒绝确认路径，不做真实删除。提交：`feat: add local PyPI cleanup confirmation`。

## 任务 3：月度报告、维护说明与交付验证

**文件：** `.github/workflows/pypi-cleanup-report.yml`、`docs/zh/pypi-cleanup.md`、`docs/pypi-publishing.md`。

**接口：** workflow 与维护说明都调用任务 1、2 的 `main`，保留规则不得复制到 YAML 或独立脚本中。

- [ ] 新建 `PyPI Cleanup Report` workflow：`schedule` 为 `17 3 1 * *`，即每月 1 日 03:17 UTC；另有无输入参数的 `workflow_dispatch`。只设 `contents: read`，单 job `report`，`ubuntu-latest`，10 分钟超时。
- [ ] checkout 设置 `persist-credentials: false`；checkout、setup-uv、upload-artifact 复用当前 `check_release_gates.py::EXPECTED_ACTION_PINS` 的精确 SHA 与 release 注释，uv 版本及 checksum 复用同文件的已批准常量。不改此 owner 或现有门禁。
- [ ] 调用 `uvx --python 3.12 --from packaging==25.0 python -I <absolute_repo>/.github/scripts/pypi_cleanup.py report --json <absolute_output>/report.json --markdown <absolute_output>/report.md`。将 Markdown 追加到 `GITHUB_STEP_SUMMARY`；artifact 名为 `pypi-cleanup-report`，保留 35 天。候选为空也是成功报告；查询失败则 job 失败。
- [ ] 不设置 Secrets、`id-token: write` 或仓库写权限，不调用 `apply`，不新增 PR、push 或发布触发器；`workflow_dispatch` 也不能接受用户提供的项目名、脚本或删除参数。
- [ ] 维护说明记录相同报告命令与本机 `apply --username <name>` 命令；解释密码及 TOTP 交互、可能的邮件验证、精确确认文本、不可恢复性、部分失败与重新预览流程。命令是维护者操作，不改变最终用户的 skill 使用方式。
- [ ] 通过 YAML 解析与结构检查核实 cron、权限、action SHA、报告参数；不为 workflow 文本新增回归测试。对新增 Python 运行 `py_compile` 与 `uvx ruff check --select F821`，重跑单模块测试。
- [ ] 提交：`ci: add monthly PyPI cleanup report`。实现 PR 因新增 `.github/scripts/**` 与 `.github/workflows/**` 需要 `ci-maintenance-approved`；该标签仅豁免 protected-path 拒绝，不豁免其他门禁。
- [ ] PR 中列出单模块测试、真实在线报告输出、静态检查，以及明确的未执行项：真实登录、真实删除、最终释放容量。检查通过不等于可以合并，等待用户确认。
- [ ] workflow 在默认分支获准落地后手动触发一次只读运行，逐项核对 job summary、artifact 和实际在线候选。记录 run URL，由用户确认此里程碑。
- [ ] 最后才由维护者在本机重新获取清单并确认首次真实删除；若该步未执行，交付状态必须写明“报告已验证，实际清理未执行”。

## 计划自检

- 每项设计要求分别由任务 1 的纯规则、任务 2 的交互边界及任务 3 的只读运行承担。
- 重点验证中的排序、追加文件、空清单与元数据异常归任务 1；发布竞态、部分失败及缓存归任务 2。
- 单一规则 owner 是新维护脚本；没有修改运行时 skill，也没有引入第二条发布或删除自动化链。
- 实施按任务顺序推进。建议主代理内联实现，最后做一次独立只读评审；三个任务共享同一脚本，不适合并行改写。此建议尚待用户选择。
- 设计已由独立子代理只读检查，结果为 `nothing important left`；该检查不代表代码、认证或实际清理已经验证。
