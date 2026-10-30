#!/bin/bash
# 生成 backend/.env.prod：随机密钥现生成，业务凭证一律从环境变量带入（绝不写进仓库）。
# 用法（部署机上）：
#   export FEISHU_APP_ID=... FEISHU_APP_SECRET=... ADMIN_FEISHU_OPEN_IDS=... POSTGRES_PASSWORD=...
#   export BOCHA_API_KEY=...        # 可选，联网搜索
#   bash deploy/gen_env_prod.sh
set -e
cd "$(dirname "$0")/../backend"

missing=""
for v in FEISHU_APP_ID FEISHU_APP_SECRET ADMIN_FEISHU_OPEN_IDS POSTGRES_PASSWORD; do
  [ -z "${!v}" ] && missing="$missing $v"
done
if [ -n "$missing" ]; then
  echo "缺少环境变量:$missing" >&2
  exit 1
fi

FERNET=$(python3 -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())")
JWT=$(openssl rand -hex 32)
cat > .env.prod << ENVEOF
DEBUG=false
DATABASE_URL=postgresql+asyncpg://ea:${POSTGRES_PASSWORD}@postgres:5432/enterprise_agent
REDIS_URL=redis://redis:6379/0
FERNET_KEY=${FERNET}
JWT_SECRET=${JWT}
JWT_EXPIRE_HOURS=12
FEISHU_APP_ID=${FEISHU_APP_ID}
FEISHU_APP_SECRET=${FEISHU_APP_SECRET}
ADMIN_FEISHU_OPEN_IDS=${ADMIN_FEISHU_OPEN_IDS}
DEV_LOGIN_ENABLED=false
SANDBOX_BASE_URL=http://sandbox:8100
BOCHA_API_KEY=${BOCHA_API_KEY:-}
ENVEOF
chmod 600 .env.prod
echo ".env.prod written"
