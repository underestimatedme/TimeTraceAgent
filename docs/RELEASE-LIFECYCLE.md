# 固定命令发布与回滚

适用于已有部署脚本、CI 或发布平台的项目。复用 `workspace check` 登记命令，不接受模型临时生成的发布命令。网页“项目流程”中将“发布健康检查与回滚流程”设为需要，可按准备、授权、交付、健康/回滚、后续工作记录证据。目前发布命令在执行电脑运行，网页不直接触发部署。

## 一次性登记

在执行电脑用 `timetrace workspace check add <workspace> <name> -- <绝对程序路径> <参数>` 登记五项：

| 名称 | 合约 |
| --- | --- |
| release-prepare | 构建、测试，失败返回非零；不发布 |
| release-deploy | 发布 `TIMETRACE_RELEASE_COMMIT`，等待发布完成再返回；不得启动脱离进程的后台发布 |
| release-health | 实际检查已发布版本与健康状态，不能仅验证 HTTP 200 |
| release-rollback | 恢复 `TIMETRACE_RELEASE_PREVIOUS`，等待完成 |
| release-rollback-health | 核对旧版本已恢复与健康状态 |

所有命令均无 shell 拼接。Agent 会移除 AI 凭据及敏感环境变量；确有必要的发布变量按既有 `check_env_keep` 登记。不要将密码放在命令参数中。外部脚本和程序会哈希绑定；发布副本包含的源码和构建产物也绑定到授权。

## 每次发布

1. 在源仓库提交所需版本，使用 `timetrace release prepare <workspace> --previous <已验证的旧提交>`。准备在独立副本进行，不改主工作区。
2. 阅读返回的 `contract`、准备步骤结果、确切版本和回滚命令。准备失败不会发布。
3. 用返回的编号和方案哈希运行 `timetrace release execute <id> --approve <authorization>`。此授权包含方案中的自动回滚。
4. `timetrace release status <id>` 查看持久化凭据。`healthy` 表示发布和健康检查通过；`rolled_back` 表示发布未通过、已回滚并核对恢复；`rollback_failed` 需要人工处理。原样重试已完成的执行只返回凭据，不重放发布。
5. 将 `~/.timetrace/releases/<id>/receipt.json` 的必要信息或平台链接放入项目交付记录，再记录发布效果与下一步。

结果为 `*:running`、`deployment_unknown` 或其他中间状态时，可能已发生外部操作。执行器拒绝重放；先在发布平台核对进程、版本和结果，必要时按已登记方案人工恢复。不得靠新建请求绕过未知结果。发布步骤必须是同步、幂等或具备平台请求 ID 的脚本。

本机测试用真实临时 HTTP 服务验证成功发布、错误版本导致健康失败、恢复旧版本、恢复复验，以及重复请求、方案变化和中断保护。这不替代各项目对真实发布平台的验收。
