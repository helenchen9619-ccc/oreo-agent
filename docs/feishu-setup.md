# 飞书自建应用配置指南（M4a WebUI 免登）

OREO 平台的飞书登录代码已全部就绪，只差一个飞书自建应用。按下面步骤操作，约 15-20 分钟。
操作入口：<https://open.feishu.cn> → 右上角「开发者后台」（用你公司的飞书账号登录）。

## 第 1 步：创建企业自建应用

1. 开发者后台 → 「创建企业自建应用」；
2. 名称填 **OREO**（或"奥洛智能体平台"），描述随意，图标随意；
3. 创建后进入应用详情页。

## 第 2 步：拿凭证填进平台

1. 左侧「凭证与基础信息」→ 复制 **App ID** 和 **App Secret**；
2. 填进 `backend/.env`：

   ```
   FEISHU_APP_ID=cli_xxxxxxxxxx
   FEISHU_APP_SECRET=xxxxxxxxxxxx
   ```

3. 重启后端（本地 uvicorn 无热重载，Ctrl+C 重起）。

## 第 3 步：配置重定向 URL（最关键，错了报 20029）

左侧「安全设置」→ 「重定向 URL」→ 添加：

```
http://localhost:5173/login
```

- 必须**逐字符一致**：协议 http、不要末尾多余斜杠、路径是 `/login`；
- 以后部署到云服务器，再来这里**追加**一条 `https://你的域名/login`（本地这条保留，两条并存）；
- 可以配多条，互不影响。

## 第 4 步：开通权限

左侧「权限管理」→ 搜索并开通：

| 权限 | scope | 用途 |
|---|---|---|
| 获取用户基本信息 | `contact:user.base:readonly` | 登录时读取姓名/头像/open_id |

## 第 4.5 步：M7 飞书内容读取的额外 scope（允许读取飞书内容开关）

数字人开启「允许读取飞书内容」后，以**用户本人身份**读飞书文档/知识库/群聊历史（同飞书智能助手的视野边界：只能读该用户自己有权限看到的内容）。需要在「权限管理」额外开通：

| 权限 | scope | 用途 |
|---|---|---|
| 查看新版文档 | `docx:document:readonly` | 读文档正文（会议纪要/方案等） |
| 查看知识库 | `wiki:wiki:readonly` | 解析知识库链接到具体文档 |
| 查看云空间文件元数据 | `drive:drive:readonly` | 文档搜索的基础权限 |
| 搜索云文档 | `search:docs:read` | 按关键词在用户有权限的文档里搜索 |
| 获取消息 | `im:message` | 读群聊/单聊历史消息（总结讨论用） |
| 获取群组信息 | `im:chat:readonly` | 列出用户所在的群、按群名定位 |
| 获取用户受权信息（离线访问） | `offline_access` | 发 refresh_token，授权一次长期有效（每次使用自动滚动续期，>30 天不用才需重新授权） |
| 获取通讯录基础信息 | `contact:contact.base:readonly` | 聊天总结时把发送者 open_id 显示成姓名；没有也能跑，只是显示 ID（注意：batch 接口实际要的是这个，不是 contact:user.base:readonly，99991679 报错实测） |

注意：

1. **加完 scope 必须重新「创建版本 → 发布」**，否则用户授权时拿不到新权限；
2. 发布后**每个用户需要点一次增量授权**：打开 `https://<你的域名>/login?upgrade=1`（已登录状态下会自动跳飞书授权页，点确认即完成，授权长期有效，平台自动滚动刷新 token）；
3. token 平台侧 Fernet 加密存储、只进不出（任何接口都不回传），与模型密钥同一条红线。

**合规告知口径（建议管理员向员工说明）**：
> 平台数字人读取的是「你本人飞书里有权限看到」的文档和聊天记录，用于帮你总结、检索、分析；平台看不到你看不到的内容，也不会把你授权范围内的内容展示给其他用户。聊天总结只回给你本人，不进入共享区。

## 第 5 步：发布版本 + 设可用范围

1. 左侧「应用发布」→「版本管理与发布」→ **创建版本** → 提交发布
   （企业自建应用免审核，秒过）；
2. 发布时「可用范围」先选 **部分成员**、只加你自己——验收通过后再改成部门/全员；
   > 可用范围不包含某人时，他授权会直接报错，这是最常见的"为什么别人登不上"原因。

## 第 6 步：冷启动（把你自己的账号变成平台管理员）

第一次登录时平台里还没有任何绑定关系，按这个顺序来：

1. 打开 <http://localhost:5173> → 点「飞书登录」→ 飞书里确认授权；
2. 预期会提示"尚未授权"——**这是正常的**。此时看后端控制台日志，有一行：

   ```
   [feishu] 未授权登录被拒绝: open_id=ou_xxxxxxxx name=你的飞书姓名
   ```

3. 把这个 `ou_xxxxxxxx` 填进 `backend/.env`：

   ```
   ADMIN_FEISHU_OPEN_IDS=ou_xxxxxxxx
   ```

   （多个人用英文逗号隔开）
4. 重启后端 → 再点一次「飞书登录」→ 这次自动创建管理员账号并进入平台；
5. 进管理端「组织/用户」给同事建账号：**姓名必须和飞书姓名一字不差**
   （认领靠姓名匹配，这是唯一匹配键）；
6. 同事首次点「飞书登录」→ 自动认领预建账号，之后直达。

## 排错表

| 现象 | 原因 | 处理 |
|---|---|---|
| 飞书页面报 20029 / "redirect_uri 请求不合法" | 后台重定向 URL 与实际不一致 | 逐字符核对第 3 步（协议、端口、尾斜杠） |
| 授权时提示无权限/应用不可用 | 可用范围没包含该成员，或版本未发布 | 检查第 5 步 |
| 点飞书登录提示"未配置飞书应用" | .env 没生效 | 确认 FEISHU_APP_ID 已填且后端已重启 |
| 登录提示"尚未授权，请联系管理员" | 平台没有同名账号 | 管理端建号，姓名=飞书姓名；或走第 6 步冷启动 |
| 登录提示"多个同名账号" | 系统里有重名用户 | 管理端改掉其中一方的姓名 |

## 云上部署时的差异

- `DEV_LOGIN_ENABLED=false`（**必须**，开发登录不校验密码）；
- 飞书后台重定向 URL 追加 `https://域名/login`；
- `ADMIN_FEISHU_OPEN_IDS` 把云上管理员的 open_id 带上（与本地同一个飞书租户则通用）。

## 附：定时任务推飞书群（群机器人 Webhook，M4b）

这是**平台 → 群**的单向通知，和上面的自建应用 OAuth 登录完全独立，不需要公网入口：

1. 在飞书里建好接收通知的群 → 群右上角「设置」→「群机器人」→「添加机器人」→ 选「自定义机器人」；
2. **安全设置选「签名校验」**（推荐），复制生成的密钥；
   - 若选「自定义关键词」：推送文本里必须包含该关键词，建议直接填任务名里的词（推送格式是 `【任务名】…`，天然命中）；
3. 复制机器人的 Webhook 地址（形如 `https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx`）；
4. 平台 → 对话页中栏「定时任务」→ 新建/编辑任务 → 底部填「飞书群机器人 Webhook」，选了签名校验就再填密钥；
5. 保存后在任务行菜单点「测试推送」，群里应秒到一张测试卡片；之后任务每次跑完自动把结果以**卡片消息**推进群——全文直接装在卡片里（单卡片约 1 万字上限），超长自动拆成多条连发（标题带 `(1/2)` 序号），群里即看全文，不要求群成员有平台账号。

排错：

| 现象 | 原因 |
|---|---|
| 测试推送报 `sign match fail` / 19021 | 密钥填错，或机器人没开签名校验却填了密钥 |
| 报 `keywords not match` / 19024 | 机器人选了关键词模式，但推送文本不含关键词 |
| 报 19022 / IP 限制 | 机器人开了 IP 白名单，把服务器出口 IP 加进去 |
| 任务跑成功但群里没消息 | 任务行的「飞书推送失败」红字有具体原因；推送失败不影响任务本身的运行状态 |

## 附：数字人读飞书内容（per-user 数据授权，M7）

数字人可读「**用户本人**飞书视野」的内容：云文档/知识库（纯文本）、按关键词搜文档、总结群聊与自己单聊的历史消息。读不了别人之间的私聊（与飞书智能助手同边界）。token 平台加密存储、自动滚动刷新，一次授权长期有效（任何 30 天窗口内用过就会自动续期；超过 30 天未用或新增权限后才需重新授权）。

### 飞书后台一次性配置

1. **应用能力**：除「网页应用」外还需添加「**机器人**」能力——`im/v1/*` 接口（即使用户身份调用）要求应用激活 Bot 能力，否则报 `Bot ability is not activated`（应用级错误，用户授权解决不了）。只开能力即可，无需配置事件订阅/消息卡片/回调；
2. **权限管理**新增以下 scope（全选**用户身份 / user_access_token** 档）：

   | scope | 用途 |
   |---|---|
   | `docx:document:readonly` | 读文档纯文本 |
   | `wiki:wiki:readonly` | 知识库节点解析 |
   | `drive:drive:readonly` | 云空间资源元信息 |
   | `search:docs:read` | 按关键词搜文档 |
   | `im:message` | 读群聊/单聊历史消息 |
   | `im:chat:readonly` | 列用户所在群 |
   | `offline_access` | refresh_token 长期续期 |
   | `contact:contact.base:readonly` | 群聊总结里把发送者 open_id 解析成姓名（缺了也能用，降级显示 open_id） |
   | `search:message` | M8 日报：按时间窗全量搜索用户消息（空 query 即可扫全天，2026-08-28 实测） |
   | `im:message.p2p_msg:get_as_user` | M8b：读私聊消息正文（缺它私聊单条 GET 报 230027；加上后实测私聊全通） |
   | `mail:user_mailbox.message:readonly` + `mail:user_mailbox.message.subject:read` + `mail:user_mailbox.message.body:read` + `mail:user_mailbox.folder:read`（+可选 `mail:user_mailbox.mail_contact:read` / `…mail_address:read` 解析发件人姓名） | M9：读收件箱邮件（每日总结扩邮箱）。**飞书邮箱权限是字段级粒度**：readonly 档只给元数据+明文 body_preview，subject 和正文要单独开 subject:read / body:read（2026-08-31 云上实测）。邮件列表路径是 `/mail/v1/user_mailboxes/me/messages?folder_id=INBOX`（folder_id 是 query 参数；路径段写法网关级 404）；列表 items 直接是 message_id 字符串列表；详情 `GET /messages/{id}?format=metadata`（format 合法枚举只有 full/metadata，非法值报 99992402）。**坑**：只开 scope 不够，必须同时加进平台 `FEISHU_USER_SCOPES`（auth/feishu.py）——consent 页只列平台显式请求的权限 |
   | `approval:approval.list:readonly`（**应用身份档**，不走用户授权） | M9：读审批（每日总结扩审批）。2026-08-31 云上实测定论：审批查询接口**只支持 tenant token**（user token 报 99991668）；这一个 scope 覆盖三个接口——待我审批 `POST /approval/v4/tasks/search`（user_id+task_status_list=["PENDING"]）/ 我发起的 `POST /approval/v4/instances/query` / 抄送我的 `POST /approval/v4/instances/search_cc`（`/approval/v4/tasks/query` 和 `/approval/v4/cc/search` 都是错路径，网关 404）。应用级变更，发布后用户无需重授权 |

> **M8c 补充**：推送「私聊推给我」还需 `im:message:send_as_bot` 的**应用身份档**（tenant token 发消息）——与上面用户身份档是两档独立权限，只开一边会报 `Access denied…[im:message:send, im:message, im:message:send_as_bot]`（2026-08-28 实测）。应用级变更，发布后用户无需重新授权。
3. **创建版本 → 发布**（版本详情的权限清单里要能看到以上各项）；
4. 平台侧：数字人详情打开「允许读取飞书内容」开关。

### 用户授权

已登录用户访问 `/login?upgrade=1`（数字人遇到未授权时会主动给这个链接），consent 页勾选同意即可；每次普通飞书登录只刷基础身份（不申请数据 scope，防止权限配置不全时打死登录），数据授权必须走 upgrade 链接。**验证点：consent 页应列出文档/消息等 7 项权限；如果只显示「获取用户身份标识」，说明走的是普通登录链接而非 upgrade 链接（平台侧 upgrade 链路会显式携带 scope 参数——飞书不传 scope 时默认只授予最小权限 `auth:user.id:read`）。**

### 合规告知口径（给管理员）

开启后平台以员工本人身份读取其飞书可见内容，建议向员工告知：「平台数字人可在你授权后读取你有权限的飞书文档和你的聊天消息用于工作辅助；读到的内容只服务于你本人的会话，不会共享给其他用户。」聊天总结内容不落长期记忆、不进共享区（prompt 纪律约束）；审计只记动作与对象 id（feishu.doc_read / feishu.doc_search / feishu.chat_search / feishu.chat_history），不记消息正文与 token。

### 排错

| 现象 | 原因 |
|---|---|
| 授权后仍反复收到授权链接 | consent 没给新 scope：版本未真正发布，或走了普通登录链接（没带 scope 参数） |
| consent 页只有「获取用户身份标识」 | 飞书不传 scope 参数时默认只给 `auth:user.id:read`；确认点的是 `/login?upgrade=1` 且后端已更新到含 scope 参数的版本 |
| 授权后群聊接口报 `Bot ability is not activated` | 应用没开「机器人」能力（应用级开关，与用户授权独立）——开启后重新发布版本即可，无需重新授权 |
| 授权完成但 DB 里 scope 没变 | 前端回调 bug 已修（2026-08-28）：OAuth `?code=` 处理必须在"已登录跳首页"判断之前；查日志有无 `POST /api/auth/feishu/callback` 确认交换发生 |
| 总结群聊提示权限不足 | `im:message` 未生效；或用户不在该群 |
| 发送者显示一串 ou_ 开头 id | 缺 `contact:contact.base:readonly`（注意不是 contact:user.base:readonly，99991679 报错实测），静默降级，不影响总结内容 |

## 附：每日飞书动态总结 + 私聊推送（M8）

M7 之上的两个增强。需要新增 2 个 scope：**`search:message`**（搜索消息，用户身份档）+ **`im:message.p2p_msg:get_as_user`**（以用户身份获取单聊消息，用户身份档——**这个是私聊正文的钥匙**：不加它，私聊单条 GET 报 230027；加上后 2026-08-28 云上实测 8/8 全通）。加完照例发布版本 + 用户重走一次 `/login?upgrade=1`；推送私聊复用 im:message + 机器人能力。

### 全会话扫描（feishu_chat_daily）

- 「总结我今天的飞书动态」→ 走 `search/v2/message` 空 query 按时间窗全量扫消息 id，逐条取正文、按会话分组出素材，提炼重要事项/待办/被分享的内容；
- **覆盖范围（2026-08-28 M8b 云上实测）**：全部群聊 + 私聊 + 机器人类会话均可读——私聊段标「私聊·对方姓名」（p2p 不在 /im/v1/chats 列表里，会话名靠 chat members 去掉自己后 contact 解析；机器人通知类会话 members 只有自己，标签退化为「私聊」）。个别消息读取失败（已撤回/权限边界）会如实标注条数跳过；
- 「我和xx聊了什么」→ feishu_chat_history 的 chat 参数直接传人名：群名没命中时自动用消息搜索发现近 30 天的私聊会话再按对方姓名匹配；
- `feishu_chat_search` 的 query 留空可列全部群（不用再先报名字）；
- 诊断脚本：`backend/check_feishu_chats.py`（会话 chat_mode 分布）、`backend/check_feishu_msg_search.py`（搜索接口契约/scope/私聊可读性），docker cp 进容器跑，免 rebuild。

### 推送「私聊推给我」

定时任务的推送目标新增「私聊推给我」：跑完由 **OREO 应用机器人给用户发 1 对 1 飞书消息**（`/im/v1/messages`，tenant 身份，接收人=任务属主的 feishu_open_id）。与群 webhook **二选一**——个人动态摘要含私聊视野，推群会泄露给群成员。前提：该用户用飞书登录过平台（有 feishu_open_id）。

**注意**：发私聊走的是**应用身份（tenant token）**，需在权限管理开通 **`im:message:send_as_bot`**（或 `im:message`）的**应用身份档**并发布版本——只开通用户身份档的 `im:message` 不够，会报 `Access denied. One of the following scopes is required: [im:message:send, im:message, im:message:send_as_bot]`（2026-08-28 实测踩坑）。这是应用级变更，发布后用户无需重新授权。

### 每日日报配置教程（发给用户的口径）

1. 数字人详情打开「允许读取飞书内容」，自己先点一次 `/login?upgrade=1` 授权；
2. 该数字人的「定时任务」tab 新建任务：频率=每天 18:00（或早上 9 点总结昨天，指令里 days 写 1），指令填：
   > 用 feishu_chat_daily 拉取我今天的飞书动态，提炼：1) 需要我跟进的重要事项和待办 2) 同事分享给我的链接/文件/信息（尤其我可能没注意到的）3) 各会话一句话摘要。按重要度排序输出。
3. 推送目标选「私聊推给我」，先点「测试推送」确认飞书里能收到 OREO 机器人的私聊卡片；
4. 之后每天到点自动跑、自动推到本人飞书，群里任何人看不到。

## 附：飞书审批读取（feishu_approval_daily，M9a）

数字人可查「当前用户」的审批三段动态：待我审批 / 我发起的 / 抄送我的（`feishu_approval_daily` 工具，挂在「允许读取飞书内容」开关下）。

与上面所有功能的**身份差异**：审批查询接口**只支持应用身份（tenant token）**（用户身份调会报 99991668「user access token not support」，2026-08-31 云上实测），按当前用户的 feishu_open_id 过滤——所以：

1. 飞书后台开通 **`approval:approval.list:readonly`** 的**应用身份档** → 创建版本 → 发布。这一个 scope 覆盖三个接口：
   - 待我审批：`POST /approval/v4/tasks/search`（user_id + task_status_list=["PENDING"] + 秒级时间窗）
   - 我发起的：`POST /approval/v4/instances/query`（返回嵌套 approval/group/instance）
   - 抄送我的：`POST /approval/v4/instances/search_cc`（注意不是 `/cc/search`，那个是 404 错路径）
2. **应用级变更，发布后用户无需重新授权**（与用户身份档的 mail scope 不同）；
3. 用户侧唯一前提：用飞书登录过平台（有 feishu_open_id 绑定），未绑定会收到绑定引导文案；
4. 审计 `feishu.approval_daily` 只记三段命中计数，不落审批内容。

### M9a.2：审批实例详情（feishu_approval_detail）

列表之外，数字人可按需深挖单个审批实例的明细（`feishu_approval_detail(instance_code)`，code 在 daily 列表每行末尾括号里）：**表单内容**（`form` 字段是控件值 JSON 字符串——radio/input/textarea 是字符串、checkbox 是数组、fieldList 明细是多行子控件数组；`type=text` 是静态说明控件，渲染时跳过）+ **审批进度**（`task_list`：各节点审批人 open_id + 状态 + 毫秒时间戳，姓名复用用户 token 的 contact 权限解析，缺了降级 open_id）。

权限：详情接口要 **`approval:approval:readonly`**（应用身份档）——和列表的 `approval:approval.list:readonly` 是**两把独立钥匙**（缺了报 99991672 点名三选一：`approval:approval:readonly` / `approval:approval` / `approval:instance`，选 readonly 最小权限）；此端点同样**不收 user token**（99991668，控制台那个用户级的 `approval:instance:read` 对应的是另一个接口路径，别混淆）。开通+发布即生效，用户无需重授权。审计 `feishu.approval_detail` 只记 instance_code 和状态，不落表单内容。

**边界**：审批是只读能力。同意/驳回/加签/催办（`approval:task:write` / `approval:instance:write` 用户身份档）刻意没做——写操作不可逆、平台暂无工具调用二次确认机制，待 M4 机器人渠道或确认机制再立项。

## 附：飞书邮箱日报（feishu_mail_daily，M9b）

数字人可扫描「当前用户本人」邮箱收件箱（INBOX）近 N 天的邮件，给出「主题 + 时间 + 正文预览 + 附件文件名 + 未读/重要标记」素材（`feishu_mail_daily` 工具，挂在「允许读取飞书内容」开关下；与 feishu_chat_daily / feishu_approval_daily 一起组成每日动态总结，模型负责提炼）。

与审批相反，邮箱**全部走用户身份（per-user token）**，且 scope 是**字段级粒度**（M9 调研期已全部开通+发布+全员重授权，零新增控制台工作）：

- `mail:user_mailbox.message:readonly`（列表+元数据）/ `.subject:read`（主题）/ `.body:read`（正文预览）/ `mail:user_mailbox.folder:read`（文件夹）——**字段级**：readonly 只给元数据，主题和正文预览要单独开；「后台开了 scope 但授权页没有」= 没进 FEISHU_USER_SCOPES 重授权。
- 文件夹 `GET /mail/v1/user_mailboxes/me/folders`：系统文件夹 folder_id 是字面量（INBOX/SENT/ARCHIVED/TRASH/DRAFT/SPAM），自带 unread_message_count（日报头部显示「收件箱总未读 N 封」）。
- 列表 `GET /mail/v1/user_mailboxes/me/messages?folder_id=INBOX&page_size=N`：**items 直接是 message_id 字符串列表**（不是对象、无时间字段），page_token 翻页，时间倒序；folder_id 必须是 query 参数（放路径段是网关级 404）。
- 详情 `GET /mail/v1/user_mailboxes/me/messages/{id}?format=metadata`：明文 subject + body_preview（**免解码**）+ attachments 文件名 + label_ids（UNREAD/IMPORTANT）+ internal_date 毫秒戳；format 合法枚举只有 full/metadata（其他报 99992402——枚举校验可用于 fuzz 参数全集），full 只多 base64url 编码的 body_plain_text/body_html（日报不需要）。

工具实现要点：列表不给时间字段 → **时间窗截停只能靠逐条 metadata 的 internal_date 判断**，撞到窗口边缘即停止（倒序保证后面全更早）；单封详情失败跳过计数不拖垮整体；审计 `feishu.mail_daily` 只记封数/未读数，不落主题和正文。

**边界**：发件人（from）字段实测不返回（字段级 scope 也不含它）——日报不显示发件人，prompt 层已写明别承诺；需要邮件全文/回复操作时引导用户去飞书邮箱。
