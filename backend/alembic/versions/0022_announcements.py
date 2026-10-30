"""M12：announcements 更新公告表（含首篇飞书生态公告种子）+ 现有数字人轮次上限全部调到 100。

Revision ID: 0022_announcements
Revises: 0021_feishu_scopes_text
Create Date: 2026-09-03
"""
import sqlalchemy as sa
from alembic import op

revision = "0022_announcements"
down_revision = "0021_feishu_scopes_text"
branch_labels = None
depends_on = None

_SEED_TITLE = "飞书生态全面打通：你的数字人现在能读懂你的一天"
_SEED_CONTENT = """本次更新后，数字人和飞书彻底打通，三件大事：

## 📮 定时任务结果直达飞书
定时任务跑完后，结果可以**一对一私聊推到你本人的飞书**（仅自己可见），也可以通过**群机器人 Webhook 推送到指定群**。个人摘要选私聊，团队共享选群。

## 📋 一站式总结你的一天
完成飞书授权后，数字人能读取你的**私聊、群聊、审批、邮件和智书合同**，自动提炼重点事项、待办和容易被遗漏的消息。

**推荐玩法**：建一个每天早上的定时任务，指令写「总结我昨天的飞书动态，提炼重要事项和待办」，推送目标选「私聊推给我」——每天一封你的专属工作日报，自动送达飞书。

## 📝 飞书文档/表格/多维表格，能读更能写
- **读**：把链接发给它就行，文档、表格、多维表格、文件夹都能直接读。
- **写**：新建文档/电子表格/多维表格、按排版格式写入正文、改指定段落。
- **管**：多维表格建列、改列、删列，记录增删改——说一句「帮我把这些数据整理成多维表格」就能直接交付成品。

有问题或想要的新能力，随时找 IT 信息部。"""


def upgrade() -> None:
    op.create_table(
        "announcements",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("pub_date", sa.String(10), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        sa.text("INSERT INTO announcements (title, pub_date, content) VALUES (:t, :d, :c)").bindparams(
            t=_SEED_TITLE, d="2026-09-03", c=_SEED_CONTENT
        )
    )
    # 现有数字人（模板+实例）轮次全部调到新上限；新领用默认 16 不变，管理员可按需在管理端调
    op.execute(sa.text("UPDATE agents SET max_loop_rounds = 100"))


def downgrade() -> None:
    op.drop_table("announcements")
