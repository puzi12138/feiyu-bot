"""Strict local identity and content checks for uncertain comment receipts."""

import hashlib
import math
import re
import time


def identifier(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    value = str(value)
    return value if re.fullmatch(r"[1-9]\d{0,24}", value) else ""


def uid(value):
    if not isinstance(value, dict):
        return ""
    return next((identifier(value[key]) for key in ("userid", "uid", "heybox_id", "heyboxid", "user_id", "id")
                 if key in value and identifier(value[key])), "")


def stamp(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number / 1000 if number > 10_000_000_000 else number


async def stored_attempt(repository, route, content, outgoing_id):
    row = await repository.db.fetchone(
        "SELECT incoming_event_id,profile_id,post_id,root_comment_id,parent_comment_id,content,content_hash,status,attempted_at,external_comment_id,confirmed_at "
        "FROM outgoing_replies WHERE id=?", (outgoing_id,),
    )
    if row is None:
        return None
    row = dict(row)
    stored = row.get("content")
    fields = ("profile_id", "post_id", "root_comment_id", "parent_comment_id")
    if (not isinstance(stored, str) or not stored.strip() or stored != content
            or not isinstance(row.get("content_hash"), str)
            or hashlib.sha256(stored.encode("utf-8")).hexdigest() != row["content_hash"]
            or row.get("status") not in {"sending", "send_unknown"}
            or row.get("external_comment_id") != "" or row.get("confirmed_at") is not None
            or stamp(row.get("attempted_at")) is None
            or any(str(row.get(field) or "") != str(getattr(route, field, "") or "") for field in fields)):
        return None
    return row


def matching_comment_id(comments, route, self_uid, content, attempted_at, *, now=None):
    """Require one exact account, post, floor, text and send-window match."""
    attempted, current = stamp(attempted_at), time.time() if now is None else now
    if not attempted or not isinstance(content, str) or not content.strip() or not self_uid:
        return None
    matches = set()
    for item in comments:
        if not isinstance(item, dict):
            continue
        cid = identifier(item.get("comment_id", item.get("commentid", item.get("id"))))
        post = identifier(item.get("post_id", item.get("link_id", item.get("linkid"))))
        parent = identifier(item.get("parent_comment_id", item.get("replyid", item.get("reply_id"))))
        root = identifier(item.get("root_comment_id", item.get("root_id")))
        author = uid(item.get("user", item.get("sender"))) or identifier(item.get("userid"))
        created = stamp(item.get("created_at", item.get("create_at", item.get("timestamp", item.get("time")))))
        candidate = item.get("content", item.get("text"))
        expected_root, expected_parent = str(route.root_comment_id or ""), str(route.parent_comment_id or "")
        if expected_root:
            floor_matches = root == expected_root and parent == expected_parent
        else:
            floor_matches = root == cid and not parent and not expected_parent
        if (cid and post == str(route.post_id) and author == self_uid and floor_matches
                and isinstance(candidate, str) and candidate == content
                and created is not None and attempted - 15 <= created <= min(attempted + 180, current + 120)):
            matches.add(cid)
    return next(iter(matches)) if len(matches) == 1 else None
