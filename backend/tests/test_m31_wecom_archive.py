"""M31 企微会话存档 Phase 1：RSA 解密 / 消息落库映射 / 同步循环 / 敏感词告警 / 管理端路由。

本地无 .so（Linux only）——SDK 层一律注入假实现；DB 用语句文本匹配的 fake（repo 惯例）。
"""
import base64
import json
from types import SimpleNamespace

import pytest

import app.wecom_archive.alerts as alerts
import app.wecom_archive.maps as maps
import app.wecom_archive.router as wrouter
import app.wecom_archive.sync as sync
from app.models.system import SystemSetting
from app.models.user import AuditLog
from app.models.wecom import WecomKeyword, WecomMessage
from app.wecom_archive.sdk import decrypt_random_key
from app.wecom_archive.sync import msg_to_row, sync_wecom_archive

pytestmark = pytest.mark.asyncio


# ---------- fake DB（语句文本按表名分派，同 test_m15 范式） ----------

class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeResult:
    def __init__(self, row=None, rows=None, scalar=None):
        self._row, self._rows, self._scalar = row, rows or [], scalar

    def scalar_one_or_none(self):
        return self._row

    def scalar_one(self):
        return self._scalar

    def scalars(self):
        return _Scalars(self._rows)


class _FakeDB:
    def __init__(self, seq_row=None, existing_msgids=(), keywords=(), contacts=None, room_row=None, admins=()):
        self.seq_row = seq_row
        self.existing_msgids = list(existing_msgids)
        self.keywords = [SimpleNamespace(word=w) for w in keywords]
        self.contacts = [SimpleNamespace(userid=u, name=n) for u, n in (contacts or {}).items()]
        self.room_row = room_row
        self.admins = list(admins)
        self.added = []
        self.commits = 0

    async def execute(self, stmt, *a, **k):
        s = str(stmt)
        if "system_settings" in s:
            return _FakeResult(row=self.seq_row)
        if "wecom_messages" in s:
            return _FakeResult(rows=self.existing_msgids, scalar=None)
        if "wecom_keywords" in s:
            return _FakeResult(rows=self.keywords, scalar=len(self.keywords))
        if "wecom_contacts" in s:
            return _FakeResult(rows=self.contacts, scalar=len(self.contacts))
        if "wecom_rooms" in s:
            return _FakeResult(row=self.room_row, scalar=1 if self.room_row else 0)
        if "users" in s:  # alerts 的 admin 查询
            return _FakeResult(rows=self.admins)
        return _FakeResult()

    def add(self, r):
        self.added.append(r)

    async def flush(self):
        pass

    async def commit(self):
        self.commits += 1


class _FakeSdk:
    """decrypt_data 直接 json.loads encrypt_chat_msg（测试把明文 JSON 编进密文字段）。"""

    def __init__(self, pages):
        self.pages = list(pages)
        self.seq_calls = []

    def get_chat_data(self, seq, limit=100):
        self.seq_calls.append(seq)
        return self.pages.pop(0) if self.pages else []

    def decrypt_data(self, rand_key, enc):
        return json.loads(enc)

    def close(self):
        pass


def _settings(**over):
    base = dict(
        WECOM_CORPID="corp",
        WECOM_ARCHIVE_SECRET="sec",
        WECOM_ARCHIVE_KEY_PATH="k.pem",
        WECOM_ARCHIVE_PUBKEY_VER="2",
        WECOM_ARCHIVE_SO_PATH="sdk.so",
        WECOM_CONTACT_SECRET="",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _item(seq, msg, ver=2):
    return {
        "seq": seq,
        "publickey_ver": ver,
        "encrypt_random_key": "ignored",
        "encrypt_chat_msg": json.dumps(msg, ensure_ascii=False),
    }


def _msg(msgid, text="你好", sender="zhangsan", roomid="", msgtime=1758000000000, msgtype="text", action="send"):
    m = {"msgid": msgid, "action": action, "from": sender, "tolist": ["lisi"], "roomid": roomid,
         "msgtime": msgtime, "msgtype": msgtype}
    if msgtype == "text" and action == "send":
        m["text"] = {"content": text}
    return m


@pytest.fixture(autouse=True)
def _reset_sync_state():
    sync._last_contact_refresh = None
    sync._priv_key_cache.clear()
    maps._tokens.clear()
    yield


@pytest.fixture
def _patch_crypto(monkeypatch):
    """同步测试不碰真实 RSA/.so：私钥读取与 RSA 解密全替换。"""
    monkeypatch.setattr(sync, "_private_key_pem", lambda path: b"pem")
    monkeypatch.setattr(sync, "decrypt_random_key", lambda pem, b64: b"rand")


def _patch_settings(monkeypatch, **over):
    monkeypatch.setattr(sync, "get_settings", lambda: _settings(**over))


# ---------- decrypt_random_key（真实 RSA 往返，纯 Python） ----------

class TestDecryptRandomKey:
    def test_roundtrip(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption())
        rand = b"rand-key-abc123"
        enc = base64.b64encode(key.public_key().encrypt(rand, padding.PKCS1v15())).decode()
        assert decrypt_random_key(pem, enc) == rand


# ---------- msg_to_row 映射 ----------

class TestMsgToRow:
    def test_text(self):
        row = msg_to_row(_msg("m1", "正文内容"), seq=7)
        assert (row.seq, row.msgid, row.text, row.msgtype, row.sender_userid) == (7, "m1", "正文内容", "text", "zhangsan")
        assert json.loads(row.receivers) == ["lisi"] and row.msgtime == 1758000000000

    def test_recall(self):
        row = msg_to_row(_msg("m2", action="recall", msgtype=""), seq=8)
        assert row.text == "[撤回了一条消息]" and row.action == "recall"

    def test_placeholders(self):
        assert msg_to_row(_msg("m3", msgtype="image"), 9).text == "[图片]"
        assert msg_to_row(_msg("m4", msgtype="voice"), 10).text == "[语音]"
        assert msg_to_row(_msg("m5", msgtype="whatever"), 11).text == "[whatever]"

    def test_link(self):
        m = _msg("m6", msgtype="link")
        m["link"] = {"title": "周报"}
        assert msg_to_row(m, 12).text == "[链接] 周报"


# ---------- 同步循环 ----------

class TestSync:
    async def test_not_configured_noop(self, monkeypatch):
        monkeypatch.setattr(sync, "get_settings", lambda: _settings(WECOM_CORPID="", WECOM_ARCHIVE_SECRET=""))
        assert await sync_wecom_archive(_FakeDB()) is None

    async def test_basic_insert_and_seq_advance(self, monkeypatch, _patch_crypto):
        _patch_settings(monkeypatch)
        db = _FakeDB()
        sdk = _FakeSdk(pages=[[_item(1, _msg("m1", "第一条")), _item(2, _msg("m2", "第二条"))]])
        stats = await sync_wecom_archive(db, sdk=sdk)
        rows = [x for x in db.added if isinstance(x, WecomMessage)]
        assert [r.msgid for r in rows] == ["m1", "m2"]
        assert stats["inserted"] == 2 and stats["last_seq"] == 2
        setting = next(x for x in db.added if isinstance(x, SystemSetting))
        assert setting.key == "wecom_archive_seq" and setting.value == "2"
        assert db.commits >= 1
        assert sdk.seq_calls[0] == 0  # 首轮无游标从 seq=0 起拉

    async def test_resume_from_stored_seq(self, monkeypatch, _patch_crypto):
        _patch_settings(monkeypatch)
        db = _FakeDB(seq_row=SystemSetting(key="wecom_archive_seq", value="50"))
        sdk = _FakeSdk(pages=[[_item(51, _msg("m9"))]])
        stats = await sync_wecom_archive(db, sdk=sdk)
        assert sdk.seq_calls[0] == 50  # 断档续拉
        assert stats["last_seq"] == 51
        assert db.seq_row.value == "51"  # 已有游标行被更新而非新建
        assert not [x for x in db.added if isinstance(x, SystemSetting)]

    async def test_dedupe_existing_msgid(self, monkeypatch, _patch_crypto):
        _patch_settings(monkeypatch)
        db = _FakeDB(existing_msgids=["m1"])
        sdk = _FakeSdk(pages=[[_item(1, _msg("m1")), _item(2, _msg("m2"))]])
        stats = await sync_wecom_archive(db, sdk=sdk)
        rows = [x for x in db.added if isinstance(x, WecomMessage)]
        assert [r.msgid for r in rows] == ["m2"] and stats["inserted"] == 1

    async def test_skip_wrong_pubkey_ver(self, monkeypatch, _patch_crypto):
        _patch_settings(monkeypatch)
        db = _FakeDB()
        sdk = _FakeSdk(pages=[[_item(1, _msg("old"), ver=1), _item(2, _msg("new"), ver=2)]])
        stats = await sync_wecom_archive(db, sdk=sdk)
        assert stats["skipped_ver"] == 1 and stats["inserted"] == 1
        assert stats["last_seq"] == 2  # 解不开的也前进游标（换公钥前历史永不可解）

    async def test_poison_message_skipped(self, monkeypatch, _patch_crypto):
        _patch_settings(monkeypatch)

        class _PoisonSdk(_FakeSdk):
            def decrypt_data(self, rand_key, enc):
                if "bad" in enc:
                    raise RuntimeError("10008")
                return json.loads(enc)

        db = _FakeDB()
        sdk = _PoisonSdk(pages=[[_item(1, _msg("bad")), _item(2, _msg("good"))]])
        stats = await sync_wecom_archive(db, sdk=sdk)
        rows = [x for x in db.added if isinstance(x, WecomMessage)]
        assert [r.msgid for r in rows] == ["good"] and stats["last_seq"] == 2

    async def test_room_name_from_cache(self, monkeypatch, _patch_crypto):
        from datetime import UTC, datetime

        _patch_settings(monkeypatch)
        db = _FakeDB(room_row=SimpleNamespace(roomname="销售一群", updated_at=datetime.now(UTC)))
        sdk = _FakeSdk(pages=[[_item(1, _msg("m1", roomid="wr123"))]])
        stats = await sync_wecom_archive(db, sdk=sdk)
        assert stats["inserted"] == 1  # 群名走映射表缓存命中，不打 HTTP（fake db 无 HTTP 通路，调了必炸）


# ---------- 敏感词告警 ----------

class TestKeywordAlert:
    async def test_hit_pushes_admins_and_audit_no_content(self, monkeypatch, _patch_crypto):
        _patch_settings(monkeypatch)
        pushed = []

        async def _fake_push(open_id, title, content, ok=True):
            pushed.append((open_id, title, content))
            return None

        monkeypatch.setattr(alerts, "push_feishu_card_to_me", _fake_push)
        admins = [SimpleNamespace(id=1, feishu_open_id="ou_a"), SimpleNamespace(id=2, feishu_open_id="ou_b")]
        db = _FakeDB(keywords=["回扣"], admins=admins, contacts={"zhangsan": "张三"})
        sdk = _FakeSdk(pages=[[_item(1, _msg("m1", "这事儿给点回扣就办了"))]])
        stats = await sync_wecom_archive(db, sdk=sdk)
        assert stats["hits"] == 1
        assert [p[0] for p in pushed] == ["ou_a", "ou_b"]
        assert "回扣" in pushed[0][2] and "张三" in pushed[0][2]
        audit = next(x for x in db.added if isinstance(x, AuditLog))
        assert audit.action == "wecom.keyword_hit"
        assert "这事儿" not in (audit.detail or "")  # 审计不落正文
        assert "回扣" in audit.detail

    async def test_no_hit_no_push(self, monkeypatch, _patch_crypto):
        _patch_settings(monkeypatch)

        async def _boom(*a, **k):
            raise AssertionError("不应推送")

        monkeypatch.setattr(alerts, "push_feishu_card_to_me", _boom)
        db = _FakeDB(keywords=["回扣"], admins=[SimpleNamespace(id=1, feishu_open_id="ou_a")])
        sdk = _FakeSdk(pages=[[_item(1, _msg("m1", "正常聊工作"))]])
        stats = await sync_wecom_archive(db, sdk=sdk)
        assert stats["hits"] == 0
        assert not [x for x in db.added if isinstance(x, AuditLog)]


# ---------- 通讯录刷新节流 ----------

class TestContactRefresh:
    async def test_refresh_called_when_secret_set(self, monkeypatch, _patch_crypto):
        _patch_settings(monkeypatch, WECOM_CONTACT_SECRET="csec")
        calls = []

        async def _fake_refresh(db, corpid, secret):
            calls.append(secret)
            return 3

        monkeypatch.setattr(sync, "refresh_contacts", _fake_refresh)
        sdk = _FakeSdk(pages=[[]])
        await sync_wecom_archive(_FakeDB(), sdk=sdk)
        assert calls == ["csec"]
        # 6 小时内第二轮不再刷新
        await sync_wecom_archive(_FakeDB(), sdk=_FakeSdk(pages=[[]]))
        assert calls == ["csec"]

    async def test_refresh_skipped_without_secret(self, monkeypatch, _patch_crypto):
        _patch_settings(monkeypatch, WECOM_CONTACT_SECRET="")

        async def _boom(*a, **k):
            raise AssertionError("不应刷新")

        monkeypatch.setattr(sync, "refresh_contacts", _boom)
        await sync_wecom_archive(_FakeDB(), sdk=_FakeSdk(pages=[[]]))


# ---------- maps 纯逻辑 ----------

class TestMaps:
    async def test_display_names(self):
        db = _FakeDB(contacts={"zhangsan": "张三", "lisi": "李四"})
        names = await maps.display_names(db, ["zhangsan", "lisi"])
        assert names == {"zhangsan": "张三", "lisi": "李四"}

    async def test_display_names_empty_input_no_query(self):
        db = _FakeDB()
        assert await maps.display_names(db, []) == {}

    async def test_resolve_room_name_cache_hit(self):
        from datetime import UTC, datetime

        db = _FakeDB(room_row=SimpleNamespace(roomname="销售一群", updated_at=datetime.now(UTC)))
        assert await maps.resolve_room_name(db, "corp", "sec", "wr123") == "销售一群"

    async def test_resolve_room_name_negative_cache_retry(self, monkeypatch):
        """M31.3：空名负缓存超过 24h 要重试 API（外部群后补可解析能自愈）。"""
        from datetime import UTC, datetime, timedelta

        old = SimpleNamespace(roomname="", updated_at=datetime.now(UTC) - timedelta(hours=25))
        db = _FakeDB(room_row=old)
        calls = []

        async def _fake_token(corpid, secret):
            return "tok"

        class _Resp:
            def json(self):
                return {"errcode": 0, "roomname": "新群名"}

        class _Client:
            def __init__(self, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def post(self, url, params=None, json=None):
                calls.append(url)
                return _Resp()

        monkeypatch.setattr(maps, "_access_token", _fake_token)
        monkeypatch.setattr(maps.httpx, "AsyncClient", _Client)
        assert await maps.resolve_room_name(db, "corp", "sec", "wr123") == "新群名"
        assert old.roomname == "新群名" and calls

    async def test_resolve_room_name_negative_cache_fresh_no_retry(self, monkeypatch):
        """M31.3：空名负缓存 24h 内不重复调 API（外部群消息高频，不能每条都打 HTTP）。"""
        from datetime import UTC, datetime

        fresh = SimpleNamespace(roomname="", updated_at=datetime.now(UTC))
        db = _FakeDB(room_row=fresh)

        async def _boom(*a):
            raise AssertionError("不应调 API")

        monkeypatch.setattr(maps, "_access_token", _boom)
        assert await maps.resolve_room_name(db, "corp", "sec", "wr123") == ""


# ---------- M31.3 告警会话标签三态（群名未解析≠私聊） ----------

class TestAlertRoomLabel:
    async def _run(self, monkeypatch, _patch_crypto, roomid, room_row=None):
        _patch_settings(monkeypatch)
        pushed = []

        async def _fake_push(open_id, title, content, ok=True):
            pushed.append(content)
            return None

        monkeypatch.setattr(alerts, "push_feishu_card_to_me", _fake_push)
        db = _FakeDB(keywords=["回扣"], admins=[SimpleNamespace(id=1, feishu_open_id="ou_a")], room_row=room_row)
        sdk = _FakeSdk(pages=[[_item(1, _msg("m1", "给点回扣", roomid=roomid))]])
        await sync_wecom_archive(db, sdk=sdk)
        assert pushed, "命中应推送"
        return pushed[0], db

    async def test_named_group(self, monkeypatch, _patch_crypto):
        from datetime import UTC, datetime

        row = SimpleNamespace(roomname="销售一群", updated_at=datetime.now(UTC))
        card, db = await self._run(monkeypatch, _patch_crypto, "wr123", room_row=row)
        assert "群「销售一群」" in card

    async def test_unnamed_group_not_labeled_dm(self, monkeypatch, _patch_crypto):
        """群名解析失败（外部群）→ 显示群聊而非私聊（本 bug 的回归断言）。"""
        from datetime import UTC, datetime

        row = SimpleNamespace(roomname="", updated_at=datetime.now(UTC))  # 新鲜负缓存，不触发 API
        card, db = await self._run(monkeypatch, _patch_crypto, "wr123", room_row=row)
        assert "群聊（群名未解析）" in card and "私聊" not in card
        audit = next(x for x in db.added if isinstance(x, AuditLog))
        assert "群聊:wr123" in audit.detail

    async def test_dm_still_dm(self, monkeypatch, _patch_crypto):
        card, db = await self._run(monkeypatch, _patch_crypto, "")
        assert "私聊/单聊" in card
        audit = next(x for x in db.added if isinstance(x, AuditLog))
        assert '"room": "私聊"' in audit.detail


# ---------- 管理端路由 ----------

class TestRouter:
    def _admin(self):
        return SimpleNamespace(id=1, role="admin")

    async def test_create_and_audit(self):
        db = _FakeDB()
        r = await wrouter.create_keyword(wrouter.KeywordIn(word=" 回扣 "), db=db, admin=self._admin())
        kw = next(x for x in db.added if isinstance(x, WecomKeyword))
        assert kw.word == "回扣" and r["word"] == "回扣"
        assert any(getattr(x, "action", "") == "wecom.keyword_create" for x in db.added)

    async def test_create_dup_409(self):
        from fastapi import HTTPException

        db = _FakeDB()
        db.keywords = []  # dup 查询走 wecom_keywords 分支，需要 scalar_one_or_none 返回已有行
        existing = SimpleNamespace(word="回扣")

        class _DupDB(_FakeDB):
            async def execute(self, stmt, *a, **k):
                if "wecom_keywords" in str(stmt):
                    return _FakeResult(row=existing, rows=[])
                return await super().execute(stmt, *a, **k)

        with pytest.raises(HTTPException) as ei:
            await wrouter.create_keyword(wrouter.KeywordIn(word="回扣"), db=_DupDB(), admin=self._admin())
        assert ei.value.status_code == 409

    async def test_delete_renames_to_free_unique(self):
        kw = WecomKeyword(word="回扣")
        kw.id = 5

        class _DB(_FakeDB):
            async def execute(self, stmt, *a, **k):
                if "wecom_keywords" in str(stmt):
                    return _FakeResult(row=kw)
                return await super().execute(stmt, *a, **k)

        db = _DB()
        await wrouter.delete_keyword(5, db=db, admin=self._admin())
        assert kw.deleted_at is not None and kw.word == "回扣#deleted-5"  # 软删改名释放唯一约束

    async def test_status_fields(self):
        db = _FakeDB(seq_row=SystemSetting(key="wecom_archive_seq", value="88"), keywords=["a", "b"])
        r = await wrouter.wecom_status(db=db, _=self._admin())
        assert r["last_seq"] == 88 and r["keyword_count"] == 2
        assert set(r) >= {"configured", "sdk_present", "contact_secret", "message_count", "contact_count", "room_count", "last_sync_at"}


# ---------- 配置字段 ----------

class TestConfig:
    def test_wecom_settings_fields_exist(self):
        from app.core.config import get_settings

        s = get_settings()
        for f in ("WECOM_CORPID", "WECOM_ARCHIVE_SECRET", "WECOM_ARCHIVE_KEY_PATH", "WECOM_ARCHIVE_PUBKEY_VER", "WECOM_ARCHIVE_SO_PATH", "WECOM_CONTACT_SECRET", "WECOM_ARCHIVE_SYNC_ENABLED"):
            assert hasattr(s, f), f

    async def test_sync_disabled_escape_hatch(self, monkeypatch):
        """逃生开关：WECOM_ARCHIVE_SYNC_ENABLED=false 时即使配好也 no-op（2026-09-23 原生崩溃事故教训）。"""
        monkeypatch.setattr(sync, "get_settings", lambda: _settings(WECOM_ARCHIVE_SYNC_ENABLED=False))
        assert await sync_wecom_archive(_FakeDB(), sdk=_FakeSdk(pages=[])) is None


# ---------- early_load 抢先加载（2026-09-23 事故修复守护） ----------

class TestEarlyLoad:
    def test_noop_when_unconfigured(self):
        """本地/未配置环境：PRELOADED 为 None，不影响测试与开发。"""
        import app.wecom_archive.early_load as el

        assert el.PRELOADED is None  # 本地无 WECOM_CORPID/.so，必须 no-op

    def test_main_imports_early_load_first(self):
        """源码断言：main.py 必须在任何会拖进 SQLAlchemy 的 import 之前加载 early_load。"""
        from pathlib import Path

        src = (Path(__file__).parent.parent / "app" / "main.py").read_text(encoding="utf-8")
        early_pos = src.index("from app.wecom_archive import early_load")
        assert early_pos < src.index("from fastapi import FastAPI"), "early_load 必须是 main.py 第一个 app 侧 import"
        assert early_pos < src.index("from app.core.config import"), "early_load 必须抢在 app 其他模块（SQLAlchemy 链）之前"
