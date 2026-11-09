"""Read back uncertain writes without ever attempting a second comment."""

import asyncio
import importlib
import math
import re
import time


PROFILE_READ = "feiyu_verified_profile_comments"
CHECK_INTERVAL = 300
MAX_AGE = 7 * 86400


def _identifier(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    value = str(value)
    return value if re.fullmatch(r"[1-9]\d{0,24}", value) else ""


def _uid(value):
    if not isinstance(value, dict):
        return ""
    return next((_identifier(value[key]) for key in ("userid", "uid", "heybox_id", "heyboxid", "user_id", "id")
                 if key in value and _identifier(value[key])), "")


def _stamp(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number / 1000 if number > 10_000_000_000 else number


def normalize_profile_comments(payload, self_uid):
    """Scope comes from the returned profile identity, never the requested ID."""
    if not isinstance(payload, dict) or not self_uid or _uid(payload.get("user")) != self_uid:
        raise ValueError("unverified_comment_profile")
    items = payload.get("result")
    if not isinstance(items, list) or len(items) > 100:
        raise ValueError("unverified_comment_list")
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        explicit_author = _uid(item.get("user", item.get("sender"))) or _identifier(item.get("userid"))
        if explicit_author and explicit_author != self_uid:
            continue
        link = item.get("link")
        link = link if isinstance(link, dict) else {}
        parent = item.get("parent_comment")
        parent = parent if isinstance(parent, dict) else {}
        cid = _identifier(item.get("comment_id", item.get("commentid", item.get("id"))))
        post = _identifier(link.get("id", link.get("linkid", link.get("link_id"))))
        root = _identifier(item.get("root_comment_id", item.get("root_id")))
        parent_id = _identifier(parent.get("id", parent.get("comment_id", parent.get("commentid"))))
        # A top-level row names itself as its root in the current official
        # profile response. Missing root evidence is never treated as top-level.
        if not cid or not post or not root or (parent and not parent_id):
            continue
        result.append({"comment_id": cid, "post_id": post, "root_comment_id": root,
                       "parent_comment_id": parent_id, "user": {"uid": self_uid},
                       "content": item.get("text", item.get("content")),
                       "created_at": item.get("create_at", item.get("created_at", item.get("timestamp")))})
    return result


def matching_comment_id(comments, route, self_uid, content, attempted_at, *, now=None):
    """Require one exact account/post/floor/text match in a fixed send window."""
    attempted, current = _stamp(attempted_at), time.time() if now is None else now
    if not attempted or not isinstance(content, str) or not content.strip() or not self_uid:
        return None
    matches = set()
    for item in comments:
        if not isinstance(item, dict):
            continue
        cid = _identifier(item.get("comment_id", item.get("commentid", item.get("id"))))
        post = _identifier(item.get("post_id", item.get("link_id", item.get("linkid"))))
        parent = _identifier(item.get("parent_comment_id", item.get("replyid", item.get("reply_id"))))
        root = _identifier(item.get("root_comment_id", item.get("root_id")))
        author = _uid(item.get("user", item.get("sender"))) or _identifier(item.get("userid"))
        stamp = _stamp(item.get("created_at", item.get("create_at", item.get("timestamp", item.get("time")))))
        candidate = item.get("content", item.get("text"))
        expected_root, expected_parent = str(route.root_comment_id or ""), str(route.parent_comment_id or "")
        if expected_root:
            floor_matches = root == expected_root and parent == expected_parent
        else:
            floor_matches = root == cid and not parent and not expected_parent
        if (cid and post == str(route.post_id) and author == self_uid and floor_matches
                and isinstance(candidate, str) and candidate.strip() == content.strip()
                and stamp is not None and attempted - 15 <= stamp <= min(attempted + 180, current + 120)):
            matches.add(cid)
    return next(iter(matches)) if len(matches) == 1 else None


def install_uncertain_reconciliation(plugin, *, start_monitor=True):
    """Patch native readback and run a bounded, read-only unknown-state check."""
    runtime = plugin.runtime
    original_check = getattr(runtime, "_check_uncertain_send", None)
    if not callable(original_check):
        return lambda: None
    package = plugin.__class__.__module__.rsplit(".", 1)[0] + ".xiaoheihe"
    api = importlib.import_module(package + ".api_client")
    endpoints = importlib.import_module(package + ".endpoints")
    models = importlib.import_module(package + ".models")
    security = importlib.import_module(package + ".security")
    original_recent = api.XiaoheiheApiClient.recent_comments
    contract = endpoints.Endpoint("GET", "/bbs/web/profile/post/comments", True, True)
    previous_contract = endpoints.ENDPOINTS.get(PROFILE_READ)
    endpoints.ENDPOINTS[PROFILE_READ] = contract

    async def recent(client, route, *, limit=30):
        self_uid = str(client.credentials.uid) if client.credentials else ""
        response = await client._request(PROFILE_READ, params={
            "userid": self_uid, "limit": max(1, min(int(limit), 50)), "offset": 0,
        })
        try:
            return normalize_profile_comments(response.payload, self_uid)
        except ValueError as error:
            raise api.ResponseContractError("评论核验的账号或结构不完整", category="response_shape") from error

    api.XiaoheiheApiClient.recent_comments = recent

    async def check(client, route, content, outgoing_id, *, attempted_at):
        try:
            comments = await client.recent_comments(route, limit=50)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Missing/failed evidence remains unknown; it never means permission
            # is missing, and never authorizes another send.
            return None
        self_uid = str(client.credentials.uid) if client.credentials else ""
        normalized = security.sanitize_reply_text(content, int(runtime.config.snapshot()["reply"]["max_reply_chars"]))
        cid = matching_comment_id(comments, route, self_uid, normalized, attempted_at)
        if cid:
            confirmation = await runtime.repository.confirm_outgoing(outgoing_id, cid)
            runtime._log_visual_context_binding(outgoing_id, cid, confirmation)
        return cid

    runtime._check_uncertain_send = check
    state_path = runtime.data_dir / "feiyu-uncertain-rechecks.json"
    state = {}
    try:
        import json
        loaded = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            state = {key: value for key, value in loaded.items() if re.fullmatch(r"[1-9]\d{0,15}", key)
                     and isinstance(value, dict) and type(value.get("last_check")) in (int, float)
                     and math.isfinite(value["last_check"])}
    except (OSError, ValueError, TypeError):
        pass
    stopped = False

    async def reconcile_once(*, now=None):
        current = time.time() if now is None else now
        # Finish an interrupted local confirmation without a network write. The
        # authoritative native outgoing receipt may have committed immediately
        # before a reload prevented its incoming event from being marked sent.
        recovered = await runtime.repository.db.fetchall(
            "SELECT o.incoming_event_id,o.content FROM outgoing_replies o "
            "JOIN incoming_events i ON i.id=o.incoming_event_id "
            "WHERE o.status='sent' AND o.external_comment_id<>'' AND o.attempted_at>=? "
            "AND i.status IN ('send_unknown','generated','dispatched','claimed','context_ready') "
            "AND i.profile_id=o.profile_id AND i.post_id=o.post_id "
            "AND i.root_comment_id=o.root_comment_id AND i.parent_comment_id=o.parent_comment_id "
            "AND NOT EXISTS(SELECT 1 FROM outgoing_replies newer "
            "WHERE newer.incoming_event_id=o.incoming_event_id AND newer.id>o.id) LIMIT 20",
            (current - MAX_AGE,),
        )
        repaired = 0
        for row in recovered:
            await runtime.repository.mark_event(row["incoming_event_id"], models.EventState.SENT,
                                                reply_text=str(row["content"] or ""))
            repaired += 1
        rows = await runtime.repository.db.fetchall(
            "SELECT * FROM outgoing_replies WHERE status IN ('sending','send_unknown') "
            "AND attempted_at>=? AND attempted_at<=? ORDER BY attempted_at ASC LIMIT 200",
            (current - MAX_AGE, current - 60),
        )
        rows = sorted(rows, key=lambda row: state.get(str(row["id"]), {}).get("last_check", 0))
        checked, confirmed = 0, 0
        for raw in rows:
            if checked >= 20:
                break
            row = dict(raw)
            key = str(row["id"])
            if current - state.get(key, {}).get("last_check", 0) < CHECK_INTERVAL:
                continue
            # Reserve the read before network activity, including across reloads.
            state[key] = {"last_check": current}
            security.atomic_write_json(state_path, dict(list(state.items())[-200:]))
            route = models.RoutingTarget(**{field: str(row.get(field) or "") for field in
                ("profile_id", "post_id", "root_comment_id", "parent_comment_id")})
            client = await runtime.get_client(route.profile_id)
            async with runtime._floor_lock(route):
                latest = await runtime.repository.db.fetchone("SELECT status FROM outgoing_replies WHERE id=?", (row["id"],))
                if latest is None or latest["status"] not in {"sending", "send_unknown"}:
                    continue
                checked += 1
                cid = await check(client, route, str(row.get("content") or ""), row["id"], attempted_at=row["attempted_at"])
                if cid:
                    event_id = row.get("incoming_event_id")
                    if event_id is not None:
                        await runtime.repository.mark_event(event_id, models.EventState.SENT, reply_text=str(row.get("content") or ""))
                    confirmed += 1
        return {"checked": checked, "confirmed": confirmed, "repaired_events": repaired, "writes_attempted": 0}

    original_reconcile = getattr(runtime, "feiyu_reconcile_unknown_comments", None)
    runtime.feiyu_reconcile_unknown_comments = reconcile_once

    async def monitor():
        while not stopped and not getattr(runtime, "_closed", False):
            try:
                await asyncio.wait_for(reconcile_once(), timeout=90)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # No raw upstream status or comments are sent as owner prose.
            await asyncio.sleep(60)

    task = asyncio.create_task(monitor(), name="feiyu-heybox-unknown-readback") if start_monitor else None

    def restore():
        nonlocal stopped
        stopped = True
        if task:
            task.cancel()
        if runtime._check_uncertain_send is check:
            runtime._check_uncertain_send = original_check
        if getattr(runtime, "feiyu_reconcile_unknown_comments", None) is reconcile_once:
            if original_reconcile is None:
                del runtime.feiyu_reconcile_unknown_comments
            else:
                runtime.feiyu_reconcile_unknown_comments = original_reconcile
        if api.XiaoheiheApiClient.recent_comments is recent:
            api.XiaoheiheApiClient.recent_comments = original_recent
        if endpoints.ENDPOINTS.get(PROFILE_READ) is contract:
            if previous_contract is None:
                endpoints.ENDPOINTS.pop(PROFILE_READ, None)
            else:
                endpoints.ENDPOINTS[PROFILE_READ] = previous_contract

    return restore
