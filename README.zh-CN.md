# RelayOps

运维交接、服务监控与事件管理平台，提供可选的 AI 辅助能力。

这是作者独立开发的公司项目的脱敏作品版，用于展示架构设计、完整业务流程与工程实现。公开版本使用虚构身份和演示场景，重建配置与模型监控协议，移除了原始 Git 历史、企业部署脚本、内部凭证和品牌图片。项目不代表任何公司的官方产品。

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

[演示步骤](docs/DEMO.md) · [系统架构](docs/ARCHITECTURE.md) · [开发与验证](docs/DEVELOPMENT.md) · [脱敏范围](docs/PROVENANCE.md)
