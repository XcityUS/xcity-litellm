"""Tests for the xct context-document CRUD (/v1/xct-context).

Mirrors the xct-skills test harness (mock prisma + TestClient). Covers:
1. create assigns server-side uuid + persists caller identity
2. list ACL scoping for non-admins (AND clause with is_public/user/team OR)
3. get 404 on missing
4. patch 403 for non-owner non-admin
5. delete succeeds for the owner
6. content over 256 KB is rejected (413)
7. list rows carry content_preview, never the full content
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
from litellm.proxy.context_endpoints.endpoints import router
from litellm.types.xct_context import MAX_CONTEXT_CONTENT_BYTES


def _make_client(role=LitellmUserRoles.PROXY_ADMIN, user_id="u-1", team_id="t-1"):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[user_api_key_auth] = lambda: UserAPIKeyAuth(
        api_key="sk-x", user_id=user_id, team_id=team_id, user_role=role
    )
    return TestClient(app)


def _mock_row(**overrides):
    base = dict(
        context_id="ctx-001",
        title="My preferences",
        content="I like dark mode.",
        tags=None,
        is_public=False,
        team_id="t-1",
        user_id="u-1",
        created_by="u-1",
        created_at=datetime(2026, 8, 31, 12, 0, 0),
        updated_at=datetime(2026, 8, 31, 12, 0, 0),
        xct_metadata={},
    )
    base.update(overrides)
    m = MagicMock()
    for k, v in base.items():
        setattr(m, k, v)
    m.model_dump = MagicMock(return_value=base)
    return m


def _mock_prisma():
    prisma = MagicMock()
    prisma.db.litellm_xctcontexttable.create = AsyncMock()
    prisma.db.litellm_xctcontexttable.find_unique = AsyncMock()
    prisma.db.litellm_xctcontexttable.find_many = AsyncMock()
    prisma.db.litellm_xctcontexttable.update = AsyncMock()
    prisma.db.litellm_xctcontexttable.delete = AsyncMock()
    return prisma


def test_create_context_persists_caller_identity():
    """The server assigns the uuid (DB default) and stamps the caller as owner."""
    prisma = _mock_prisma()
    prisma.db.litellm_xctcontexttable.create.return_value = _mock_row(
        context_id="new-id", title="My preferences"
    )
    with patch("litellm.proxy.proxy_server.prisma_client", prisma):
        resp = _make_client(role=LitellmUserRoles.INTERNAL_USER).post(
            "/v1/xct-context",
            json={"title": "My preferences", "content": "I like dark mode."},
            headers={"Authorization": "Bearer k"},
        )
    assert resp.status_code == 200
    create_kwargs = prisma.db.litellm_xctcontexttable.create.call_args.kwargs["data"]
    # No client-supplied context_id: the uuid comes from the DB default.
    assert "context_id" not in create_kwargs
    assert create_kwargs["user_id"] == "u-1"
    assert create_kwargs["team_id"] == "t-1"
    assert create_kwargs["created_by"] == "u-1"
    assert resp.json()["context_id"] == "new-id"


def test_list_context_scopes_to_caller_for_non_admin():
    prisma = _mock_prisma()
    prisma.db.litellm_xctcontexttable.find_many.return_value = [
        _mock_row(context_id="ctx-1", user_id="u-1"),
    ]
    with patch("litellm.proxy.proxy_server.prisma_client", prisma):
        resp = _make_client(role=LitellmUserRoles.INTERNAL_USER).get(
            "/v1/xct-context", headers={"Authorization": "Bearer k"}
        )
    assert resp.status_code == 200
    args = prisma.db.litellm_xctcontexttable.find_many.call_args.kwargs
    # Where clause must AND the base filters with a caller-clauses OR.
    where = args["where"]
    assert "AND" in where
    inner_or = next(c for c in where["AND"] if "OR" in c)["OR"]
    # contains exactly is_public + user + team clauses
    keys = {next(iter(d)) for d in inner_or}
    assert keys == {"is_public", "user_id", "team_id"}


def test_list_context_admin_sees_all():
    prisma = _mock_prisma()
    prisma.db.litellm_xctcontexttable.find_many.return_value = []
    with patch("litellm.proxy.proxy_server.prisma_client", prisma):
        resp = _make_client(role=LitellmUserRoles.PROXY_ADMIN).get(
            "/v1/xct-context", headers={"Authorization": "Bearer k"}
        )
    assert resp.status_code == 200
    where = prisma.db.litellm_xctcontexttable.find_many.call_args.kwargs["where"]
    assert "AND" not in where  # no caller scoping for admins


def test_list_context_returns_preview_not_content():
    long_content = "x" * 5000
    prisma = _mock_prisma()
    prisma.db.litellm_xctcontexttable.find_many.return_value = [
        _mock_row(context_id="ctx-1", content=long_content),
    ]
    with patch("litellm.proxy.proxy_server.prisma_client", prisma):
        resp = _make_client(role=LitellmUserRoles.PROXY_ADMIN).get(
            "/v1/xct-context", headers={"Authorization": "Bearer k"}
        )
    assert resp.status_code == 200
    row = resp.json()["data"][0]
    assert row["content_preview"] == "x" * 200
    assert "content" not in row  # full body never leaks into list pages


def test_get_context_404_when_missing():
    prisma = _mock_prisma()
    prisma.db.litellm_xctcontexttable.find_unique.return_value = None
    with patch("litellm.proxy.proxy_server.prisma_client", prisma):
        resp = _make_client(role=LitellmUserRoles.PROXY_ADMIN).get(
            "/v1/xct-context/nope", headers={"Authorization": "Bearer k"}
        )
    assert resp.status_code == 404


def test_get_context_403_for_unrelated_non_admin():
    prisma = _mock_prisma()
    prisma.db.litellm_xctcontexttable.find_unique.return_value = _mock_row(
        context_id="ctx-001", user_id="OTHER_USER", team_id="OTHER_TEAM"
    )
    with patch("litellm.proxy.proxy_server.prisma_client", prisma):
        resp = _make_client(role=LitellmUserRoles.INTERNAL_USER).get(
            "/v1/xct-context/ctx-001", headers={"Authorization": "Bearer k"}
        )
    assert resp.status_code == 403


def test_get_context_returns_full_content():
    prisma = _mock_prisma()
    prisma.db.litellm_xctcontexttable.find_unique.return_value = _mock_row(
        context_id="ctx-001", content="full body here"
    )
    with patch("litellm.proxy.proxy_server.prisma_client", prisma):
        resp = _make_client(role=LitellmUserRoles.INTERNAL_USER).get(
            "/v1/xct-context/ctx-001", headers={"Authorization": "Bearer k"}
        )
    assert resp.status_code == 200
    assert resp.json()["content"] == "full body here"


def test_patch_context_403_for_non_owner_non_admin():
    prisma = _mock_prisma()
    prisma.db.litellm_xctcontexttable.find_unique.return_value = _mock_row(
        context_id="ctx-001", user_id="OTHER_USER"
    )
    with patch("litellm.proxy.proxy_server.prisma_client", prisma):
        resp = _make_client(role=LitellmUserRoles.INTERNAL_USER).patch(
            "/v1/xct-context/ctx-001",
            json={"title": "evil"},
            headers={"Authorization": "Bearer k"},
        )
    assert resp.status_code == 403
    prisma.db.litellm_xctcontexttable.update.assert_not_awaited()


def test_patch_context_owner_succeeds():
    prisma = _mock_prisma()
    prisma.db.litellm_xctcontexttable.find_unique.return_value = _mock_row(
        context_id="ctx-001", user_id="u-1"
    )
    prisma.db.litellm_xctcontexttable.update.return_value = _mock_row(
        context_id="ctx-001", title="renamed", user_id="u-1"
    )
    with patch("litellm.proxy.proxy_server.prisma_client", prisma):
        resp = _make_client(role=LitellmUserRoles.INTERNAL_USER).patch(
            "/v1/xct-context/ctx-001",
            json={"title": "renamed"},
            headers={"Authorization": "Bearer k"},
        )
    assert resp.status_code == 200
    assert resp.json()["title"] == "renamed"
    update_data = prisma.db.litellm_xctcontexttable.update.call_args.kwargs["data"]
    assert update_data["updated_by"] == "u-1"


def test_delete_context_owner_succeeds():
    prisma = _mock_prisma()
    prisma.db.litellm_xctcontexttable.find_unique.return_value = _mock_row(
        context_id="ctx-001", user_id="u-1"
    )
    with patch("litellm.proxy.proxy_server.prisma_client", prisma):
        resp = _make_client(role=LitellmUserRoles.INTERNAL_USER).delete(
            "/v1/xct-context/ctx-001", headers={"Authorization": "Bearer k"}
        )
    assert resp.status_code == 200
    assert resp.json() == {"context_id": "ctx-001", "deleted": True}
    prisma.db.litellm_xctcontexttable.delete.assert_awaited_once()


def test_create_context_rejects_oversized_content():
    prisma = _mock_prisma()
    too_big = "x" * (MAX_CONTEXT_CONTENT_BYTES + 1)
    with patch("litellm.proxy.proxy_server.prisma_client", prisma):
        resp = _make_client(role=LitellmUserRoles.INTERNAL_USER).post(
            "/v1/xct-context",
            json={"title": "big", "content": too_big},
            headers={"Authorization": "Bearer k"},
        )
    assert resp.status_code == 413
    prisma.db.litellm_xctcontexttable.create.assert_not_awaited()


def test_patch_context_rejects_oversized_content():
    prisma = _mock_prisma()
    prisma.db.litellm_xctcontexttable.find_unique.return_value = _mock_row(
        context_id="ctx-001", user_id="u-1"
    )
    too_big = "x" * (MAX_CONTEXT_CONTENT_BYTES + 1)
    with patch("litellm.proxy.proxy_server.prisma_client", prisma):
        resp = _make_client(role=LitellmUserRoles.INTERNAL_USER).patch(
            "/v1/xct-context/ctx-001",
            json={"content": too_big},
            headers={"Authorization": "Bearer k"},
        )
    assert resp.status_code == 413
    prisma.db.litellm_xctcontexttable.update.assert_not_awaited()


def test_list_context_cursor_pagination():
    prisma = _mock_prisma()
    prisma.db.litellm_xctcontexttable.find_many.return_value = [
        _mock_row(context_id="ctx-1"),
        _mock_row(context_id="ctx-2"),
        _mock_row(context_id="ctx-3"),  # limit+1 sentinel row
    ]
    with patch("litellm.proxy.proxy_server.prisma_client", prisma):
        resp = _make_client(role=LitellmUserRoles.PROXY_ADMIN).get(
            "/v1/xct-context?limit=2&cursor=ctx-0",
            headers={"Authorization": "Bearer k"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert [d["context_id"] for d in body["data"]] == ["ctx-1", "ctx-2"]
    assert body["has_more"] is True
    assert body["next_cursor"] == "ctx-2"
    args = prisma.db.litellm_xctcontexttable.find_many.call_args.kwargs
    assert args["take"] == 3
    assert args["cursor"] == {"context_id": "ctx-0"}
    assert args["skip"] == 1
