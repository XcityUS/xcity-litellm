"""
xct context-document CRUD — ``/v1/xct-context``.

User-scoped context documents (preferences, org knowledge, notes) shared
across Xcity OS, xct-chat, and xct-home through tokenhub. Rows live in the
dedicated ``LiteLLM_XctContextTable``; the endpoint shapes, ACLs, and cursor
pagination deliberately mirror the xct-skills module
(``litellm.proxy.skill_endpoints.endpoints``).
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from litellm._logging import verbose_proxy_logger
from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
from litellm.types.xct_context import (
    CONTENT_PREVIEW_CHARS,
    MAX_CONTEXT_CONTENT_BYTES,
    XCTContextCreate,
    XCTContextDoc,
    XCTContextListItem,
    XCTContextListResponse,
    XCTContextPatch,
)

router = APIRouter()


# Json-typed columns on LiteLLM_XctContextTable. Prisma rejects plain dicts /
# lists and explicit None for Json columns — values must be wrapped in
# prisma.Json and None-valued keys omitted entirely.
_JSON_COLUMNS = {"tags", "xct_metadata"}


def _prisma_json_compat(data: dict) -> dict:
    import prisma  # optional dependency; only importable on the proxy

    compat = {}
    for key, value in data.items():
        if key in _JSON_COLUMNS:
            if value is None:
                continue
            value = prisma.Json(value)  # type: ignore[attr-defined]
        compat[key] = value
    return compat


def _is_admin(uak: UserAPIKeyAuth) -> bool:
    return uak.user_role in (
        LitellmUserRoles.PROXY_ADMIN,
        LitellmUserRoles.PROXY_ADMIN.value,
    )


def _enforce_content_limit(content: Optional[str]) -> None:
    """413 when the document body exceeds MAX_CONTEXT_CONTENT_BYTES."""
    if content is None:
        return
    size = len(content.encode("utf-8"))
    if size > MAX_CONTEXT_CONTENT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Context content too large: {size} bytes "
                f"(limit {MAX_CONTEXT_CONTENT_BYTES})."
            ),
        )


def _row_data(row) -> dict:
    if hasattr(row, "model_dump"):
        return row.model_dump()
    if isinstance(row, dict):
        return row
    return vars(row)


def _row_to_doc(row) -> XCTContextDoc:
    """Map a Prisma row (or dict) to the full single-doc response."""
    data = _row_data(row)
    return XCTContextDoc(
        context_id=data["context_id"],
        title=data.get("title"),
        content=data.get("content"),
        tags=data.get("tags"),
        is_public=bool(data.get("is_public", False)),
        team_id=data.get("team_id"),
        user_id=data.get("user_id"),
        created_by=data.get("created_by"),
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
        xct_metadata=data.get("xct_metadata") or {},
    )


def _row_to_list_item(row) -> XCTContextListItem:
    """Map a Prisma row to the list-row shape: content_preview, not content."""
    data = _row_data(row)
    content = data.get("content")
    preview = content[:CONTENT_PREVIEW_CHARS] if content is not None else None
    return XCTContextListItem(
        context_id=data["context_id"],
        title=data.get("title"),
        content_preview=preview,
        tags=data.get("tags"),
        is_public=bool(data.get("is_public", False)),
        team_id=data.get("team_id"),
        user_id=data.get("user_id"),
        created_by=data.get("created_by"),
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
        xct_metadata=data.get("xct_metadata") or {},
    )


async def _require_writable(context_id: str, uak: UserAPIKeyAuth):
    """Raise 404 / 403 / return row if caller may mutate this document."""
    from litellm.proxy.proxy_server import prisma_client

    if prisma_client is None:
        raise HTTPException(status_code=503, detail="DB not initialized")
    row = await prisma_client.db.litellm_xctcontexttable.find_unique(
        where={"context_id": context_id}
    )
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"Context document '{context_id}' not found"
        )
    if _is_admin(uak):
        return row
    if uak.user_id and getattr(row, "user_id", None) == uak.user_id:
        return row
    raise HTTPException(
        status_code=403,
        detail=(
            "Only the context document owner or a proxy admin may modify "
            "this document."
        ),
    )


@router.post(
    "/v1/xct-context",
    tags=["[beta] XCT Context"],
    response_model=XCTContextDoc,
)
async def create_context_doc(
    payload: XCTContextCreate,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
) -> XCTContextDoc:
    from litellm.proxy.proxy_server import prisma_client

    if prisma_client is None:
        raise HTTPException(status_code=503, detail="DB not initialized")

    _enforce_content_limit(payload.content)

    create_data: dict = {
        "title": payload.title,
        "content": payload.content,
        "tags": payload.tags,
        "is_public": payload.is_public,
        "team_id": payload.team_id or user_api_key_dict.team_id,
        "user_id": user_api_key_dict.user_id,
        "xct_metadata": payload.xct_metadata or {},
        "created_by": user_api_key_dict.user_id,
    }
    row = await prisma_client.db.litellm_xctcontexttable.create(
        data=_prisma_json_compat(create_data)
    )
    return _row_to_doc(row)


@router.get(
    "/v1/xct-context",
    tags=["[beta] XCT Context"],
    response_model=XCTContextListResponse,
)
async def list_context_docs(
    q: Optional[str] = Query(None, description="Match title (case-insensitive)."),
    team_id: Optional[str] = None,
    cursor: Optional[str] = Query(
        None, description="context_id of the previous page tail."
    ),
    limit: int = Query(50, ge=1, le=200),
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
) -> XCTContextListResponse:
    from litellm.proxy.proxy_server import prisma_client

    if prisma_client is None:
        return XCTContextListResponse(data=[], has_more=False)

    # Build a single where clause; push every filter to the DB.
    where: dict = {}
    if team_id is not None:
        where["team_id"] = team_id
    if q:
        # Postgres ILIKE wrapped by Prisma's `contains` mode.
        where["title"] = {"contains": q, "mode": "insensitive"}

    # Non-admin scoping: caller must own, share team, or document is public.
    if not _is_admin(user_api_key_dict):
        caller_clauses = [{"is_public": True}]
        if user_api_key_dict.user_id:
            caller_clauses.append({"user_id": user_api_key_dict.user_id})
        if user_api_key_dict.team_id:
            caller_clauses.append({"team_id": user_api_key_dict.team_id})
        # Combine with the base filters via AND.
        where = {"AND": [where, {"OR": caller_clauses}]}

    find_args: dict = {
        "where": where,
        "take": limit + 1,
        "order": {"context_id": "asc"},
    }
    if cursor is not None:
        find_args["cursor"] = {"context_id": cursor}
        find_args["skip"] = 1

    rows = await prisma_client.db.litellm_xctcontexttable.find_many(**find_args)
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = rows[-1].context_id if has_more and rows else None
    return XCTContextListResponse(
        data=[_row_to_list_item(r) for r in rows],
        has_more=has_more,
        next_cursor=next_cursor,
    )


@router.get(
    "/v1/xct-context/{context_id}",
    tags=["[beta] XCT Context"],
    response_model=XCTContextDoc,
)
async def get_context_doc(
    context_id: str,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
) -> XCTContextDoc:
    from litellm.proxy.proxy_server import prisma_client

    if prisma_client is None:
        raise HTTPException(status_code=503, detail="DB not initialized")
    row = await prisma_client.db.litellm_xctcontexttable.find_unique(
        where={"context_id": context_id}
    )
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"Context document '{context_id}' not found"
        )
    # Read scoping: public OR owned OR same team OR admin.
    if not _is_admin(user_api_key_dict):
        if not (
            getattr(row, "is_public", False)
            or getattr(row, "user_id", None) == user_api_key_dict.user_id
            or (
                user_api_key_dict.team_id
                and getattr(row, "team_id", None) == user_api_key_dict.team_id
            )
        ):
            raise HTTPException(
                status_code=403,
                detail=f"Context document '{context_id}' is not accessible.",
            )
    return _row_to_doc(row)


@router.patch(
    "/v1/xct-context/{context_id}",
    tags=["[beta] XCT Context"],
    response_model=XCTContextDoc,
)
async def patch_context_doc(
    context_id: str,
    patch: XCTContextPatch,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
) -> XCTContextDoc:
    from litellm.proxy.proxy_server import prisma_client

    await _require_writable(context_id, user_api_key_dict)
    _enforce_content_limit(patch.content)
    update_data = {k: v for k, v in patch.model_dump().items() if v is not None}
    if not update_data:
        # Re-fetch + return; no-op patches shouldn't error.
        return await get_context_doc(context_id, user_api_key_dict)

    update_data["updated_by"] = user_api_key_dict.user_id
    row = await prisma_client.db.litellm_xctcontexttable.update(
        where={"context_id": context_id},
        data=_prisma_json_compat(update_data),
    )
    return _row_to_doc(row)


@router.delete(
    "/v1/xct-context/{context_id}",
    tags=["[beta] XCT Context"],
)
async def delete_context_doc(
    context_id: str,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
) -> dict:
    from litellm.proxy.proxy_server import prisma_client

    await _require_writable(context_id, user_api_key_dict)
    try:
        await prisma_client.db.litellm_xctcontexttable.delete(
            where={"context_id": context_id}
        )
    except Exception as e:  # pragma: no cover — wrap unexpected DB errors
        verbose_proxy_logger.exception("delete_context_doc failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    return {"context_id": context_id, "deleted": True}
