# 参与贡献

欢迎 Issue 和 PR！本文档说明本地开发环境与提交要求。

## 本地开发环境

前置：Docker Desktop、Python 3.12+、Node 20+

```bash
# 1. 起基础设施（postgres + redis + sandbox）
docker compose up -d postgres redis sandbox

# 2. 后端
cd backend
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # Windows；Linux/macOS: .venv/bin/pip
cp .env.example .env   # 生成 FERNET_KEY 填入：
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
.venv/Scripts/python -m alembic upgrade head
.venv/Scripts/python -m uvicorn app.main:app --port 8000 --reload

# 3. 前端（新终端）
cd frontend
npm install
npm run dev          # http://localhost:5173
```

首次使用：登录页 dev 登录 `admin`（管理员）→ 模型管理录入供应商 key 与模型 → 建授权 → 开聊。

## 提交要求

- **测试必须全绿**：`cd backend && python -m pytest tests -q`；改动行为必须配测试（仓库现有 850+ 用例，测试风格参照邻近文件）
- 前端改动需 `npm run build` 通过；**涉及角色分支（admin/普通用户）的前端改动，PR 描述里注明已用非 admin 账号冒烟**
- 数据库结构变更：新增 alembic 迁移（`alembic revision`），迁移必须可重复执行安全（软删除风格沿用现有约定）
- **密钥红线**：任何真实密钥/内部域名/内部人员信息不进代码、测试与文档；`.env*` 永不提交（gitignore 已拦，force-add 前请三思）

## 代码约定

- 后端：FastAPI + SQLAlchemy 2 async；新端点注意 Depends 门禁声明顺序（装饰期求值）
- 前端：函数组件 + hooks（**hooks 一律前置声明**，勿在渲染期引用后声明的 state——有过全员白屏事故教训）
- 中文注释为主，关键设计决策在代码旁注明原因（本仓库风格）

## 提交信息

无强制格式，建议中文一句话说清「改了什么 + 为什么」，如：
`修复长会话复读：消息窗口改 DESC LIMIT 再反转，附回归测试`
