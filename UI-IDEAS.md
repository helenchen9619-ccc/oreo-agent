# UI/UX 想法 Backlog

> 用法：想到什么随时往对应分区丢，不用管格式和优先级。
> M2 动手前先消化「结构性」分区（落进页面骨架）；「细节打磨」分区在 M2 完成后统一做一轮。

## 结构性（布局 / 导航 / 主题 / 信息架构）—— M2 前消化

- [x] **单 WebUI + 角色模块权限**：全量功能一个界面，按角色勾选开放模块（对话/数字人管理/模型网关/用量报表/审计…），前端导航按权限渲染。M2 先内置 admin/member 两角色 + 模块勾选，自定义角色后期 → 已消化进 M2 设计
- [x] **左侧导航**：页面 6-8 个时用单级左导航，超 10 个再分大小导航 → 已消化
- [x] **对话页参考 ChatGPT/Claude**：左 = 对话明细列表（可选中续聊），右 = 当前对话窗 → 已消化
- [x] **上下文进度可见**：对话窗内显示 已用tokens/context_window 进度条，接近压缩阈值变色预警 → 已消化
- [x] **用户自己的 workspace**：拖拽文件进对话即上传并引用；用户可管理自己的文件和 skill；workspace 按 (user, agent) 隔离 → 已消化（M2 先支持文本类文件注入，Office 解析后置）
- [x] **数字人三层模型**（关键数据模型决策）：模板层（IT 共享：SOUL/AGENTS/skills/共享MEMORY）→ 个人层（user×agent：个人调教指令/个人memory/个人skills/个人workspace，互相隔离）→ 会话层（组装 = 模板 + 个人）。依据：OpenClaw workspace 跟 agent（团队翻车根因）、Magic 组织级 bot + 按用户 workspace → 已消化
- [x] **数字人管理用卡片墙**：点击卡片 → 详情：.md 文件页内 Tab 编辑（带版本）+ skills 勾选 → 已消化
- [ ] 主题风格：未定 → 浅色起步，CSS 变量走设计 token，暗色模式框架自带后期可开
- [x] **UI 框架整体切到 shadcn/ui + Tailwind**（2026-08-06 定）：对话页要 ChatGPT/Claude 气质、组件源码进项目随便改、暗色 CSS 变量自带；管理表格用 Data Table(TanStack)。依据：Magic 前端同栈（React+Vite+Tailwind+shadcn）已验证。M1 的 3 个 AntD 页面 M2 顺手重写

## 细节打磨（文案 / 样式 / 交互 / 动效）—— M2 后统一做

- [ ] （待补充）

## 已消化

| 想法 | 落点 | 完成时间 |
|---|---|---|
| 1.1 单 WebUI + 角色模块勾选 | M2 角色-模块权限表 + 导航按权限渲染 | 待 M2 |
| 2.1 对话页布局 + 上下文进度条 | M2 对话页重构 | 待 M2 |
| 2.1 workspace 文件/skill 拖拽引用 | M2 workspace 模块（user×agent 隔离） | 待 M2 |
| 2.3 三层模型（模板/个人/会话） | M2 agent_user_overlays 表 + 提示组装分层 | 待 M2 |
| 2.2/3 数字人卡片墙 + .md Tab 编辑器 | M2 管理端数字人模块 | 待 M2 |
