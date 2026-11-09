"""Gate automatic Bili owner shares on committed local evidence journals."""

import re
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

from .peak_policy import manual_bili_request, manual_owner_event

_BVID = re.compile(r"BV[0-9A-Za-z]{10}")
_RECORD_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_NEGATION = re.compile(r"不要|别(?:再|给|向|把|发|转|分享|推荐|\s*[@＠]|艾特|at)|不用|不必|不需要|禁止|停止|取消|撤销|不是要|无需|不想|没(?:有)?(?:让|要|叫|说|要求|想)", re.IGNORECASE)
_HISTORY_OR_DISCUSSION = re.compile(
    r"以前|之前|曾经|过去|分享过|转发过|发送过|发过|分享了吗|分享了没|转发了吗|"
    r"你会不会|你会(?:怎么|如何|分享|转发|[@＠]|艾特|at)|是否|是不是|有没有|"
    r"解释|什么意思|什么含义|举例|示例|演示|教程"
)
_OTHER_RECIPIENT = re.compile(
    r"(?:给|发给|转给|分享给|推荐给|艾特|[@＠])\s*(?:朋友|别人|他|她|他们|她们|群友|群里|其他人)"
)
_DIRECTED_SHARE = re.compile(
    r"(?:分享|推荐|转发).{0,30}(?:给我|给主人)|(?:给我|给主人).{0,30}(?:分享|推荐|转发)"
)
_EXPLICIT_SHARE = re.compile(
    r"^(?:(?:请|麻烦|帮我|可以|能不能|可不可以|你|肥鱼|把|将|给我|这(?:个|条|部|段|支)|"
    r"视频|链接|B站|bilibili|小破站|看|刷|浏览|找|去|然后|并|\s|[，,：:])+.{0,45})?"
    r"(?:分享(?:给我|给主人|一下)|转发(?:给我|给主人|一下)|推荐(?:给我|给主人|一下)|"
    r"发给我|发我|给我发|转给我|贴给我|发送给我|@[我]|＠我|艾特我)(?:[\s，,。.!！?？]|$|.{0,30}$)",
    re.IGNORECASE,
)


def bvid_from_source(value):
    """Extract only an exact public Bili video identity without resolving URLs."""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if _BVID.fullmatch(value):
        return value
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
                or parsed.port not in (None, 80, 443)
                or (parsed.hostname or "").lower().rstrip(".") not in
                    {"bilibili.com", "www.bilibili.com", "m.bilibili.com"}):
            return ""
        match = re.fullmatch(r"/video/(BV[0-9A-Za-z]{10})/?", parsed.path)
        return match.group(1) if match else ""
    except ValueError:
        return ""


def _is_bili_source(value):
    if bvid_from_source(value):
        return True
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value.strip())
        return (parsed.scheme in {"http", "https"} and not parsed.username and not parsed.password
                and parsed.port in (None, 80, 443)
                and (parsed.hostname or "").lower().rstrip(".") in
                    {"bilibili.com", "www.bilibili.com", "m.bilibili.com", "b23.tv", "www.b23.tv"})
    except ValueError:
        return False


def _explicit_share_text(event):
    text = str(getattr(event, "message_str", "") or "").strip()
    return bool(1 <= len(text) <= 500 and not _NEGATION.search(text)
                and not _HISTORY_OR_DISCUSSION.search(text) and not _OTHER_RECIPIENT.search(text)
                and (_EXPLICIT_SHARE.search(text) or _DIRECTED_SHARE.search(text)))


def manual_bili_share_request(event, source_url):
    """Exempt only a verified human owner's explicit request to share a Bili source."""
    if not _is_bili_source(source_url):
        return False
    try:
        if not manual_owner_event(event) or not _explicit_share_text(event):
            return False
        # If the user named a specific BV, the selected source must be it.
        mentioned = set(_BVID.findall(str(getattr(event, "message_str", "") or "")))
        return not mentioned or mentioned == {bvid_from_source(source_url)}
    except Exception:
        return False


def manual_bili_owner_mention_request(event):
    """Recognize an explicit verified-human request to mention the owner."""
    try:
        text = str(getattr(event, "message_str", "") or "").strip()
        return bool(manual_owner_event(event) and 1 <= len(text) <= 500
                    and not _NEGATION.search(text) and not _HISTORY_OR_DISCUSSION.search(text)
                    and not re.search(r"@我(?:了|过)|艾特我(?:了|过)", text)
                    and re.search(r"(?:[@＠]\s*(?:我|主人|一团普子|0)|艾特我|at我)",
                                  text, re.IGNORECASE))
    except Exception:
        return False


def manual_bili_browse_share_request(event):
    """Browsing on request alone does not authorize a recommendation or mention."""
    try:
        return bool(manual_bili_request(event) and _explicit_share_text(event))
    except Exception:
        return False


def _ledger_path():
    return Path(__file__).resolve().parents[4] / "data/bili-humanism/ledger.sqlite"


def journaled_bvid(bvid, *, ledger_path=None):
    """Read only existence of a committed note joined to this exact BV."""
    if not isinstance(bvid, str) or not _BVID.fullmatch(bvid):
        return False
    try:
        path = Path(ledger_path) if ledger_path is not None else _ledger_path()
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True,
                                     timeout=0.1)) as db:
            return db.execute(
                "SELECT 1 FROM readings r JOIN notes n ON n.reading_id=r.id "
                "WHERE r.bvid=? LIMIT 1", (bvid,),
            ).fetchone() is not None
    except (OSError, ValueError, sqlite3.Error):
        return False


def bvid_for_committed_note(note_id, reading_id, *, ledger_path=None):
    """Resolve a successful journal receipt to its exact BV without note text."""
    if not all(isinstance(v, str) and _RECORD_ID.fullmatch(v) for v in (note_id, reading_id)):
        return ""
    try:
        path = Path(ledger_path) if ledger_path is not None else _ledger_path()
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True,
                                     timeout=0.1)) as db:
            row = db.execute(
                "SELECT r.bvid FROM notes n JOIN readings r ON r.id=n.reading_id "
                "WHERE n.id=? AND n.reading_id=? LIMIT 1", (note_id, reading_id),
            ).fetchone()
        return row[0] if row and isinstance(row[0], str) and _BVID.fullmatch(row[0]) else ""
    except (OSError, ValueError, sqlite3.Error):
        return ""


async def notify_new_committed_note(steward, event, receipt, reading_id, *, source_quote=None):
    """One chance after a verified autonomous journal; never retry an unknown send."""
    if (not isinstance(receipt, dict) or receipt.get("status") != "local_reviewable_draft"
            or receipt.get("deduplicated") is not False or receipt.get("readingId") != reading_id
            or receipt.get("automatic_followup_blocked") is True):
        return
    # Trust only an occurrence of the known owner-scoped autonomous job, not
    # user-supplied event metadata or an arbitrary owner-private turn.
    try:
        from .bili_cron_guard import (
            _CRON_SHARE_SCOPE, committed_cron_scope, cron_scope_allowed, reading_receipt, share_hook_ready,
        )
        from .bili_journal_review import _RESUME_SCOPE
        from .bili_public_reason import public_reason_for_note

        scope = await committed_cron_scope(steward, event, receipt, reading_id)
        row = reading_receipt(steward, scope, reading_id)
        if scope is None or row is None or row.get("share_claimed") is True:
            return
        # Consume the one new-note opportunity even when no public reason is
        # available. A later tool callback cannot replay this receipt.
        row["share_claimed"] = True
        bvid = bvid_for_committed_note(receipt.get("id"), reading_id)
        if not bvid or row.get("bvid") != bvid:
            return
        reason = public_reason_for_note(receipt["id"], reading_id, source_quote)
        if not reason or not await cron_scope_allowed(steward, event, scope):
            return
        hooks = steward._incident_hooks
        features = ("bili_journal", "bili_share")
        generation = hooks._resume_snapshot(features)
        if (hooks._blocked(*features) is not False or generation is None
                or (generation[0] and scope.started <= generation[0])):
            return
        if not share_hook_ready(steward):
            await hooks._report("bili_share", "guard_failed", "bili_share_once")
            return
        token = _RESUME_SCOPE.set((steward, scope.generation))
        cron_token = _CRON_SHARE_SCOPE.set((steward, event, scope))
        try:
            await steward.retry_bili_recommendation(
                bvid, dry_run=False, new_journal=(receipt["id"], reading_id), public_reason=reason)
        finally:
            _CRON_SHARE_SCOPE.reset(cron_token)
            _RESUME_SCOPE.reset(token)
    except Exception:
        # The committed journal remains successful; platform outcomes stay in
        # the existing queue and receipt ledgers and are not retried here.
        return
