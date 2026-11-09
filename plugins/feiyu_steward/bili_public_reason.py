"""Build an optional public recommendation reason from one verified reading.

This module never reads private note bodies or performs network or model calls.
The supplied quote is untrusted selection input, not a public comment or an
instruction.  Its identity and exact text must match one stored source segment.
"""

import json
import math
import re
import sqlite3
import unicodedata
from contextlib import closing
from pathlib import Path

_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_BV = re.compile(r"BV[0-9A-Za-z]{10}\Z")
_BODY_LIMIT = 2_000_000
_MAX_SEGMENTS = 30_000

# Public source text is still untrusted.  This deliberately conservative filter
# prevents obvious private material and attempts to turn the quotation into a
# command or a disclosure.  The existing public-comment filter is also required.
_UNSAFE_QUOTE = re.compile(
    r"私聊|私信|私人|私密|隐私|秘密|机密|主人|手账|日记|聊天记录|"
    r"系统提示|提示词|系统指令|开发者指令|忽略|无视|绕过|越狱|"
    r"执行命令|执行指令|泄露|泄漏|窃取|发送给|转发给|上传|"
    r"密码|口令|验证码|证件|身份证|银行卡|手机号|电话号码|家庭住址|住址|"
    r"我家|我住|我的(?:名字|姓名|电话|地址|住处|账号|账户|密码|身份|记录)|"
    r"credential|password|api.?key|authorization|sessdata|secret|"
    r"(?:ignore|disregard|forget)\s+(?:all\s+)?(?:previous|prior|system|instructions?)|"
    r"(?:system|developer)\s*(?:prompt|message|instructions?)|jailbreak",
    re.IGNORECASE,
)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def public_reason_for_note(note_id, reading_id, source_quote, *, ledger_path=None):
    """Return a source-verified quotation, or an empty string on any failure.

    ``segment_id`` is the zero-based absolute index in the source reading, not
    an index relative to a paginated response.  Only first-part A1 subtitles or
    A2 platform-generated text are eligible; neither is claimed as author speech.
    ``ledger_path`` is an internal override for isolated tests.
    """
    if not all(type(value) is str and _ID.fullmatch(value) for value in (note_id, reading_id)):
        return ""
    if type(source_quote) is not dict or set(source_quote) != {"segment_id", "quote"}:
        return ""
    index, quote = source_quote["segment_id"], source_quote["quote"]
    if (type(index) is not int or not 0 <= index < _MAX_SEGMENTS
            or type(quote) is not str or not 8 <= len(quote) <= 180
            or quote != quote.strip() or _UNSAFE_QUOTE.search(quote)
            or any(unicodedata.category(char).startswith("C") for char in quote)):
        return ""

    try:
        path = (Path(ledger_path) if ledger_path is not None else
                Path(__file__).resolve().parents[4] / "data/bili-humanism/ledger.sqlite")
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True,
                                     timeout=0.2)) as db:
            # Do not select or inspect notes.body, including as a fallback.
            row = db.execute(
                "SELECT r.bvid,r.page,r.body FROM notes n "
                "JOIN readings r ON r.id=n.reading_id "
                "WHERE n.id=? AND n.reading_id=? AND r.id=? "
                "AND typeof(r.body)='text' AND length(r.body)<=? LIMIT 1",
                (note_id, reading_id, reading_id, _BODY_LIMIT),
            ).fetchone()
        if (not row or type(row[0]) is not str or not _BV.fullmatch(row[0])
                or type(row[1]) is not int or row[1] != 1 or type(row[2]) is not str):
            return ""
        reading = json.loads(row[2], object_pairs_hook=_unique_object)
        if (type(reading) is not dict or reading.get("bvid") != row[0]
                or type(reading.get("page")) is not int or reading["page"] != row[1]
                or reading.get("route") not in {"A1", "A2"}
                or "localEvidence" in reading):
            return ""
        duration, segments = reading.get("duration"), reading.get("segments")
        if (not _number(duration) or duration <= 0 or type(segments) is not list
                or not 0 < len(segments) <= _MAX_SEGMENTS or index >= len(segments)):
            return ""
        segment = segments[index]
        if (type(segment) is not dict or segment.get("source") != reading["route"]
                or not _number(segment.get("start"))
                or not 0 <= segment["start"] <= duration + 5
                or (segment.get("end") is not None and (
                    not _number(segment["end"])
                    or not segment["start"] <= segment["end"] <= duration + 5))
                or type(segment.get("text")) is not str
                or len(segment["text"]) > 20_000 or quote not in segment["text"]):
            return ""
        # Import lazily: the recommendation module imports the journal gate,
        # which may itself use this helper.
        from .bili_recommendation import public_recommendation_reason

        # Validate the quote itself as well: a fixed wrapper must not turn a
        # generic or sensitive excerpt into an apparently valid reason.
        if public_recommendation_reason(quote) != quote:
            return ""
        reason = "来源文字片段提到：“" + quote + "”；这处细节值得留意。"
        # Require the original quote to survive normalization byte-for-byte.
        return reason if public_recommendation_reason(reason) == reason else ""
    except (OSError, ValueError, TypeError, OverflowError, RecursionError,
            ImportError, sqlite3.Error):
        return ""
