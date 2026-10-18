# PyPI 版本清理

本页面向仓库维护者，记录 `ppt-master` 在 PyPI 上的历史版本清理流程。最终用户通过安装并使用本 skill、以自然语言描述需求来使用工具，具体命令由 AI agent 在内部调用，因此本页的清理操作不改变最终用户的使用方式。清理只作用于 PyPI 上已发布的文件，不涉及 GitHub tags、Releases 或仓库源码，也不改变包版本、依赖与发布链。

报告是只读的；删除只能在本机交互终端中确认后执行。

## 1. 保留策略

- 保留最新的 10 个非空版本；
- 保留最近 30 天内（含 30 天边界）有文件上传的版本；
- 保留 `info.version` 指向的默认版本；
- 其余非空版本列为删除候选。

版本按 PEP 440 排序，时间统一为 UTC，每个版本使用其最新一次文件上传时间。保留策略的唯一实现是 `.github/maintenance/pypi_cleanup.py`；本页与其保持一致，如有出入以脚本为准。

## 2. 月度报告

`.github/workflows/pypi-cleanup-report.yml` 每月 1 日 03:17 UTC 生成只读报告，也可通过 `workflow_dispatch` 手动触发。报告写入 job summary，并作为 `pypi-cleanup-report` artifact 保留 35 天。该工作流只有 `contents: read` 权限，不读取任何 secret，也不会删除任何内容；候选为空时同样是一份成功报告，查询失败时 job 失败。

要得到同样的报告，在仓库根目录运行：

```bash
uvx --python 3.12 --from packaging==25.0 python -I .github/maintenance/pypi_cleanup.py report \
  --json report.json --markdown report.md
```

省略 `--json` 与 `--markdown` 时，报告打印到 stdout。

## 3. 本机删除

先运行报告并核对候选清单，然后在本机交互终端执行：

```bash
uvx --python 3.12 --from packaging==25.0 python -I .github/maintenance/pypi_cleanup.py apply --username <name>
```

`<name>` 是你的 PyPI 用户名。流程如下：

1. 脚本重新拉取公开元数据，打印完整候选清单（版本、字节数、最新上传时间）与可回收容量；
2. 交互提示 `Type 'DELETE ppt-master' to delete these <n> versions:`，只有逐字符输入 `DELETE ppt-master` 才会继续，其他输入不会触发任何删除；
3. 删除前再次查询，确认候选仍然可删；若候选已被新的上传或保留规则保护，脚本中止且不删除任何内容；
4. 删除由子工具 `uvx --from pypi-cleanup==0.1.12 pypi-cleanup` 执行。该子工具在终端中交互询问 PyPI 密码；本脚本不读取、不传递、不存储密码，并会把 `PYPI_CLEANUP_PASSWORD` 从子进程环境中移除。若账号启用了两步验证，需按提示输入 TOTP 验证码；子工具也可能要求完成邮件验证；
5. 删除后以 5 秒为间隔最多复核 6 次，确认候选已消失且保留的版本仍然存在。

在 CI 环境（设置了 `GITHUB_ACTIONS`）或非交互终端中，`apply` 会直接拒绝执行。

### 不可恢复

删除会把对应版本的文件从 PyPI 永久移除；PyPI 不允许重新上传同一版本号，因此被删除的版本号无法再次使用。执行前请逐项确认候选清单与保留策略一致。

### 部分失败与重新预览

- 候选为空：脚本以非零码退出并提示没有可删除项，不启动任何子进程；
- 子工具非零退出：脚本报告失败，并列出**未确认删除且未经逐项核实**的候选，不视为删除成功；
- 复核超时或候选被重新保护：脚本停止，不会重试删除；
- 出现以上任一情况，请先重新运行 `report` 子命令查看当前状态，再用 `apply` 重新预览并确认。
