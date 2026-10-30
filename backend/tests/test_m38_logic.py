"""M38 源码守护：工作区文件夹 ⋯ 菜单新建子文件夹 + 企微映射群搜索框。

两项均为纯前端改动（后端嵌套链路 valid_rel / mkdir(parents=True) / paths_cover 本已就绪，
群列表 /messages/meta 全量返回、前端过滤是仓库惯例），无后端逻辑可测，只做接线断言。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_chatpage_subfolder_entry():
    src = (ROOT / "frontend/src/pages/ChatPage.jsx").read_text(encoding="utf-8")
    # newFolder 接受父路径前缀，菜单入口禁 / 只建直属子级
    assert "const newFolder = async (parent = '')" in src
    assert "`${parent}/${name}`" in src or "parent ? `${parent}/${name}` : name" in src
    assert "子文件夹名不能包含 /" in src
    assert "if (parent) expandToFolder(parent)" in src
    # ⋯ 菜单里有新建子文件夹项，且把当前文件夹传进去
    assert "onClick={() => newFolder(folder)}" in src
    assert "新建子文件夹" in src
    # 顶部工具条入口保留（无参调用 = 一级文件夹、保留层级写法）
    assert "onClick={newFolder}" in src


def test_wecom_mappings_room_search():
    src = (ROOT / "frontend/src/pages/admin/WecomMappings.jsx").read_text(encoding="utf-8")
    assert "roomQuery" in src
    assert "搜索群名 / roomid" in src
    assert "filteredRooms" in src
    # roomid + 群名双匹配
    assert "r.roomid.toLowerCase().includes(rq)" in src
    assert "r.roomname" in src
    # 渲染走过滤后列表
    assert "filteredRooms?.map" in src
