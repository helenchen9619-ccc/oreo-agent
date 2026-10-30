# 安全政策 / Security Policy

## 报告漏洞

**请不要在公开 Issue 中报告安全漏洞。**

推荐方式（任选其一）：

1. GitHub 私有漏洞报告：仓库页面的 **Security → Advisories → Report a vulnerability**（仅维护者可见）；
2. 在 Issue 中仅说明「发现一个安全问题，希望私下沟通」，维护者会主动联系你。

请尽量附上：受影响版本、复现步骤、潜在影响。我们会在 72 小时内响应。

## 支持版本

| 版本 | 状态 |
|---|---|
| 最新 release | ✅ 接受安全修复 |
| 更早版本 | ❌ 请升级到最新版 |

## 云上例行更新 SOP

适用于云机（/opt/enterprise-agent）的日常版本更新。**首次部署/重新生成环境请走 `deploy/gen_env_prod.sh`，不在本节范围。**

```bash
# 1. 前端管理端侧栏开「维护模式」
# 2. 上传改动文件到对应路径后：
cd /opt/enterprise-agent
docker compose -f docker-compose.prod.yml build app web   # 按需，只改后端就 build app
docker compose -f docker-compose.prod.yml up -d
docker exec ea-app alembic upgrade head                    # 仅当本批含新迁移
docker exec ea-app ls /app/data                            # 必验：data 挂载还在（漂移两度致数据蒸发）
# 3. 管理员自测（前端角色分支有改动时必须再用非 admin 账号冒烟），然后关维护模式
```

要点：

- **不需要每次 export `POSTGRES_PASSWORD`**：云机已有兜底来源（项目根 `.env` 或 shell 环境）。验证：`docker compose -f docker-compose.prod.yml config -q && echo OK`。仅当报 `must export POSTGRES_PASSWORD` 时按 compose 注释 export（值与 `backend/.env.prod` 的 `DATABASE_URL` 口令一致）。pgdata 卷已初始化，该口令只在首次建库时生效，例行重建仅是解析期门槛。
- **`backend/.env.prod` 从不随更新上传/覆盖**：它在云机常驻（chmod 600）。只有重新生成时才注意：`gen_env_prod.sh` 需 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `ADMIN_FEISHU_OPEN_IDS` / `POSTGRES_PASSWORD` 四个环境变量，且生成物默认 `DEV_LOGIN_ENABLED=false`。
- **seed 脚本云上认证一律用 `SEED_TOKEN` 环境变量传 admin token**（生产 dev-login 已关闭，无 SEED_TOKEN 时脚本才回落 dev-login）。

## 部署安全自查清单（自部署用户必读）

- 生产环境必须设置 `DEV_LOGIN_ENABLED=false`（默认模板已关闭）；
- `JWT_SECRET` / `FERNET_KEY` 使用 `deploy/gen_env_prod.sh` 随机生成，不要复用示例值；
- 数据库口令通过 `POSTGRES_PASSWORD` 环境变量注入，prod compose 没有默认值；
- postgres / redis 不要暴露公网端口（prod compose 默认不暴露）；
- 沙箱服务默认只绑定 `127.0.0.1`，不要改成 `0.0.0.0`；
- 企业微信会话存档私钥（`private_key.pem`）只放在服务器数据卷，绝不提交进仓库。
