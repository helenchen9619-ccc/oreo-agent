"""M2.9 逻辑测试：技能文件路径放宽中文、会话删除/原始文件下载端点存在性。"""
import pydantic
import pytest

from app.skills.router import SkillFileIn


def test_skill_file_path_allows_chinese():
    f = SkillFileIn(path="脚本/格式化.py", content="x")
    assert f.path == "脚本/格式化.py"
    assert SkillFileIn(path="SKILL.md").path == "SKILL.md"


def test_skill_file_path_rejects_traversal():
    with pytest.raises(pydantic.ValidationError):
        SkillFileIn(path="../x.md")
    with pytest.raises(pydantic.ValidationError):
        SkillFileIn(path="a//b.md")
    with pytest.raises(pydantic.ValidationError):
        SkillFileIn(path="a b.md")  # 空格不允许


def test_chat_delete_and_workspace_raw_routes_registered():
    from app.chat.router import router as chat_router
    from app.workspace.router import router as ws_router

    chat = {(r.path, tuple(sorted(r.methods))) for r in chat_router.routes}
    assert ("/api/chat/conversations/{cid}", ("DELETE",)) in chat
    ws = {(r.path, tuple(sorted(r.methods))) for r in ws_router.routes}
    assert ("/api/agents/{agent_id}/workspace/{file_id}/raw", ("GET",)) in ws
    assert ("/api/agents/{agent_id}/workspace/folder", ("DELETE",)) in ws
