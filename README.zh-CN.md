# RelayOps

运维交接、服务监控与事件管理平台，提供可选的 AI 辅助能力。

## 主要功能

- 项目与产品管理、交接验收、版本记录和恢复预案。
- 应用健康、任务执行与超时、模型漂移监控。
- 值班分派、事件处理、SLA、审计和运营看板。
- 可选的文档提取、交接草稿、页面助手、诊断和待确认操作。

## 启动

```bash
git clone https://github.com/yhnapoleon/RelayOps.git
cd RelayOps
docker compose up -d --build
```

访问 http://localhost:8080 ，使用 `admin / admin123` 登录。普通演示账号为 `testuser / test123`，运维账号为 `relayopsmember1 / relayops123`。这些账号仅用于本机演示，Compose 的端口默认只绑定本机。

无需 LLM Key 即可体验核心业务流程。若需 AI 能力，将 `.env.example` 复制为 `.env`，显式设置模型服务地址、Key 和模型名称。邮件默认仅写日志，不实际发送。

## 账号与鉴权

使用本地用户名密码登录，密码通过加盐 PBKDF2-SHA256 哈希保存，会话使用 JWT。管理员可在 **Admin Panel → User Management** 创建账号、设置密码和修改角色；用户可通过侧栏的 **Change password** 修改自己的密码。新密码至少 12 位，修改密码或退出登录会使该账号所有已有会话失效。角色调整在下次请求生效。

初始化不含演示账号的新数据库时，将 `.env.example` 复制为 `.env`，设置 `RELAYOPS_DEMO_ACCOUNTS=false`、`RELAYOPS_ADMIN_USERNAME`、`RELAYOPS_ADMIN_PASSWORD` 和随机的 `RELAYOPS_JWT_SECRET`。初始化配置不会覆盖已有密码；关闭演示账号初始化也不会删除已创建的账号。尚未设置本地密码的已有账号，需要管理员先为其设置密码才能登录。

[演示步骤](docs/DEMO.md) · [系统架构](docs/ARCHITECTURE.md) · [开发与验证](docs/DEVELOPMENT.md)
