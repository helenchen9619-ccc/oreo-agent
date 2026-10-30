"""核心纯逻辑单测：授权判定、成本计算、配额判定（不依赖数据库）。"""
from decimal import Decimal
from types import SimpleNamespace

from app.gateway.service import compute_cost, evaluate_grants, quota_exceeded


def _user(role="member", uid=1, dept=10):
    return SimpleNamespace(role=role, id=uid, department_id=dept)


def _grant(grantee_type, grantee_id=None):
    return SimpleNamespace(grantee_type=grantee_type, grantee_id=grantee_id)


def _model(pin="0.002", pout="0.008"):
    return SimpleNamespace(price_input_per_1k=Decimal(pin), price_output_per_1k=Decimal(pout))


def _quota(enabled=True, limit_tokens=None, limit_cost=None):
    return SimpleNamespace(enabled=enabled, limit_tokens=limit_tokens, limit_cost=limit_cost)


# ---------- 授权 ----------

def test_admin_bypasses_grants():
    assert evaluate_grants([], _user(role="admin")) is True


def test_no_grants_denies_member():
    assert evaluate_grants([], _user()) is False


def test_user_grant_allows():
    assert evaluate_grants([_grant("user", 1)], _user(uid=1)) is True
    assert evaluate_grants([_grant("user", 2)], _user(uid=1)) is False


def test_department_grant_allows_same_dept():
    assert evaluate_grants([_grant("department", 10)], _user(dept=10)) is True
    assert evaluate_grants([_grant("department", 11)], _user(dept=10)) is False


def test_all_grant_allows_everyone():
    assert evaluate_grants([_grant("all")], _user(uid=99, dept=None)) is True


# ---------- 成本 ----------

def test_compute_cost():
    cost = compute_cost(_model(), input_tokens=1000, output_tokens=500)
    assert cost == Decimal("0.002") * 1 + Decimal("0.008") * Decimal("0.5")


def test_compute_cost_zero_price():
    assert compute_cost(_model("0", "0"), 100, 100) == Decimal(0)


# ---------- 配额 ----------

def test_quota_token_limit():
    q = _quota(limit_tokens=1000)
    assert quota_exceeded(q, used_tokens=999, used_cost=Decimal(0)) is False
    assert quota_exceeded(q, used_tokens=1000, used_cost=Decimal(0)) is True


def test_quota_cost_limit():
    q = _quota(limit_cost=Decimal("1.5"))
    assert quota_exceeded(q, used_tokens=0, used_cost=Decimal("1.4")) is False
    assert quota_exceeded(q, used_tokens=0, used_cost=Decimal("1.5")) is True


def test_quota_disabled_never_exceeded():
    q = _quota(enabled=False, limit_tokens=1)
    assert quota_exceeded(q, used_tokens=99999, used_cost=Decimal("999")) is False


def test_quota_unlimited_never_exceeded():
    q = _quota(limit_tokens=None, limit_cost=None)
    assert quota_exceeded(q, used_tokens=10**9, used_cost=Decimal("99999")) is False
