# OREO · 企业数字人管理平台

<!-- TODO(发布前): 把徽章里的 OWNER 替换为实际 GitHub 用户名 -->

[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![CI](https://github.com/OWNER/oreo-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/OWNER/oreo-agent/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.12-blue)](backend/requirements.txt)
[![React](https://img.shields.io/badge/react-18-61dafb)](frontend/package.json)

**让每个员工都有自己的 AI 数字同事。** OREO 是一个自部署的企业数字人平台：用模板批量创建懂岗位、会工具、有记忆的数字员工，接入飞书/企微工作流，全程审计留痕。

<!-- TODO(发布前): 30 秒演示 GIF（docs/images/demo.gif）+ 截图墙 -->

## ✨ 特性

- **数字人体系** — 内置岗位模板（经营参谋 / 销售助手 / 门店开发 / 视觉设计 / 办公文书…），SOUL + AGENTS 声明式定义，开箱即用也可以自己写
- **技能市场** — 技能即 Markdown：创建、绑定、fork、版本升级、回滚；数字人还能在对话里把做法沉淀成个人技能
- **飞书全生态** — 免密登录、文档/表格/多维表格读写、云盘上传、群聊与私聊总结、审批查询、邮箱日报、每日定时推送
- **企业微信会话存档**（可选模块）— 消息落库、敏感词告警、导出与保留策略（SDK 需按腾讯许可自行下载）
- **代码执行沙箱** — 独立容器跑数据分析/制图/OCR/PDF 解析，产物直挂对话附件
- **记忆体系** — 每个数字人一份 MEMORY.md + 用户偏好笔记，支持一键提炼整合
- **运行中心** — 逐轮钻取每次模型调用（token、成本、工具调用链），定时任务托管
- **安全合规** — 全量审计留痕、维护模式、API 白名单工具、提示词注入扫描、模型 Key Fernet 加密存储
- **移动端自适应** — 手机浏览器完整可用

## 🆚 和同类项目的差异

| | OREO | Dify / FastGPT 等 |
|---|---|---|
| 定位 | 企业**数字人**（岗位、记忆、技能成长） | 通用 LLM 应用编排 |
| 组织模型 | 多租户用户/部门 + 数字人归属与共享 | 应用为中心 |
| IM 集成 | 飞书全生态读写 + 企微会话存档 | 多为机器人消息收发 |
| 合规 | 审计留痕 / 维护模式 / 白名单工具内置 | 视项目而定 |

## 🚀 快速开始

前置：已安装 Docker（含 Compose 插件）。

```bash
git clone https://github.com/OWNER/oreo-agent.git
cd oreo-agent
docker compose -f docker-compose.quickstart.yml up -d --build
```

打开 <http://localhost:8080>：

1. 登录页选 **dev 登录**，用户名输 `admin`（管理员）；
2. 右上角进入**模型管理**，录入你的模型供应商 API Key 与模型名，创建授权；
3. 回到对话页，开聊 🎉

> 代码执行功能需要再构建一次沙箱镜像（首次体验可跳过）：
> `docker build -f sandbox/runner.Dockerfile -t ea-sandbox-runner:local sandbox/`

**本地开发**（改代码用）：见 [CONTRIBUTING.md](CONTRIBUTING.md)。
**生产部署**：`docker-compose.prod.yml` + `deploy/gen_env_prod.sh`，部署前必读 [SECURITY.md](SECURITY.md) 的安全自查清单。
**飞书接入**：按 [docs/feishu-setup.md](docs/feishu-setup.md) 创建自建应用并配置权限。

## 🏗 架构

```
浏览器 / 手机
   │
 nginx (web) ── /api ──▶ app (FastAPI) ──▶ PostgreSQL + pgvector
                            │           ──▶ Redis
                            │           ──▶ sandbox (代码执行容器编排)
                            │           ──▶ 飞书 / 企微 / 模型供应商
```

- 后端：Python 3.12 / FastAPI / SQLAlchemy 2 (async) / Alembic
- 前端：React 18 / Vite / Tailwind CSS 4 / Radix UI
- 沙箱：独立 FastAPI 服务，按需起一次性执行容器

## ✅ 测试

```bash
cd backend && pip install -r requirements.txt && python -m pytest tests -q
```

850+ 单元测试，CI 对每个 push/PR 全量运行（含前端构建与 compose 校验）。

## 🗺 路线图

- [ ] 英文文档与 i18n
- [ ] 更多岗位模板与官方技能包
- [ ] 自动化矩阵：定时任务串联 / 邮件触发 / 绩效看板
- [ ] Loop 闭环：数字人自检复盘与技能 SOP 化

## 参与贡献

欢迎 Issue 与 PR，请先读 [CONTRIBUTING.md](CONTRIBUTING.md)。安全漏洞请按 [SECURITY.md](SECURITY.md) 私下报告。

## 许可证

[Apache-2.0](LICENSE) © The OREO Authors。第三方资产见 [NOTICE](NOTICE)。

---

## English

**OREO is a self-hosted enterprise digital-human platform**: create role-aware AI teammates from templates, equip them with Markdown-defined skills, plug into Feishu (Lark) and WeCom workflows — with audit trails, a model gateway with per-user quotas, a code-execution sandbox, and long-term memory. One command to try locally:

```bash
docker compose -f docker-compose.quickstart.yml up -d --build   # → http://localhost:8080
```

Highlights: digital-human templates & skill marketplace · full Feishu ecosystem (docs/sheets/bitable/drive/chat/mail/approval) · optional WeCom session archive · sandboxed code execution · ops console with per-turn drill-down · 850+ tests · Apache-2.0.
