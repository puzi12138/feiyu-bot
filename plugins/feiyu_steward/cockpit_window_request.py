"""Parse an explicit Cockpit usage interval from a single request.

This module has no I/O. Calendar-only requests are left to the caller.
Ambiguous or unsupported clock/duration expressions fail closed rather than
silently becoming a different period.
"""

import re
import unicodedata
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation


LOCAL_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")
_INVALID = {"ok": False, "reason": "invalid_usage_window"}
_DATE = r"(?:[0-9]{4}-[0-9]{2}-[0-9]{2}|今天|今日|昨天|昨日|前天)"
_CLOCK = r"[0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?"
_RANGE = re.compile(
    rf"(?P<start_date>{_DATE})\s*[Tt]?\s*(?P<start_clock>{_CLOCK})"
    rf"\s*(?:到|至|~|—|–|-|to)\s*"
    rf"(?:(?P<end_date>{_DATE})\s*[Tt]?\s*)?(?P<end_clock>{_CLOCK})",
    re.IGNORECASE,
)
_NUMBER = r"(?:[0-9]+(?:\.[0-9]+)?|[零〇一二两三四五六七八九十百千万半]+)"
_UNIT = (
    r"(?:(?:小时|钟头|分钟)(?=$|[^A-Za-z0-9]|(?:tokens?|cockpit)(?![A-Za-z0-9]))|"
    r"(?:h(?:ours?|rs?)?|m(?:in(?:utes?)?s?)?)(?=$|[^A-Za-z0-9]|tokens?(?![A-Za-z0-9])))"
)
_DURATION = re.compile(
    rf"(?<![0-9A-Za-z._+\-/])(?:cockpit\s*)?(?:(?:过去|最近|(?<!最)近)\s*(?:的|这)?\s*)?"
    rf"(?P<number>{_NUMBER})\s*(?:个\s*(?P<half>半)?\s*)?(?P<unit>{_UNIT})",
    re.IGNORECASE,
)
_TIME_HINT = re.compile(
    r"小时|钟头|分钟|刻钟|时辰|秒|多久|几点|[0-9]{1,2}\s*:|[0-9一二两三四五六七八九十]+\s*点"
    r"|(?:[0-9]+(?:\.[0-9]+)?|[零〇一二两三四五六七八九十百]+)\s*(?:分|时)"
    r"|[0-9]+(?:\.[0-9]+)?\s*(?:h(?:ours?|rs?)?|m(?:in(?:utes?)?s?)?)(?=$|[^A-Za-z]|tokens?(?![A-Za-z]))"
    r"|[0-9]+(?:\.[0-9]+)?\s*(?:minutes?|mins?|hours?|hrs?)[A-Za-z]+"
    r"|(?<![A-Za-z])(?:hours?|hrs?|minutes?|mins?|seconds?|secs?)(?![A-Za-z])",
    re.IGNORECASE,
)
# Any remaining selector after removing the matched one makes the intent
# ambiguous. A date alone is deliberately not a clock/duration request.
_EXTRA_SELECTOR = re.compile(
    r"今天|今日|昨天|昨日|前天|明天|明日|后天|上周|本周|这周|下周|本月|上月|下月"
    r"|上午|下午|早上|晚上|中午|凌晨|昨晚|今晚|今早|过去|最近|近来|星期[一二三四五六日天]|周[一二三四五六日天]"
    r"|[0-9]{4}(?:-[0-9]{2}-[0-9]{2}|\s*[/\.]\s*[0-9]{1,2}\s*[/\.]\s*[0-9]{1,2})"
    r"|[0-9一二两三四五六七八九十百]+\s*(?:天|周|星期|个月|年)"
    r"|(?:未来|将来|接下来|后续|每隔|每过|每[个]?|大约|大概|约莫|至少|至多|超过|不超过|小于|大于|少于|多于)"
    r"|(?<![A-Za-z])(?:next|future|every|approximately|about|over|under|more\s+than|less\s+than)(?![A-Za-z])"
    r"|[0-9一二两三四五六七八九十]+\s*(?:到|至|~|—|–|-|/|\\+)\s*$",
    re.IGNORECASE,
)
_CHINESE_NUMBER = re.compile(
    r"(?:(?P<wan>[一二两三四五六七八九])万)?"
    r"(?:(?P<qian>[一二两三四五六七八九])千)?"
    r"(?:(?:零|〇)?(?P<bai>[一二两三四五六七八九])百)?"
    r"(?:(?:零|〇)?(?P<shi>[一二两三四五六七八九]?)十)?"
    r"(?:(?:零|〇)?(?P<ge>[一二两三四五六七八九]))?"
)
_CHINESE_DIGITS = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9,
}


def _number(value):
    if value == "半":
        return Decimal("0.5")
    if value[0] in "0123456789":
        return Decimal(value)
    if value in ("零", "〇"):
        return Decimal(0)
    match = _CHINESE_NUMBER.fullmatch(value)
    if not match or not any(part is not None for part in match.groupdict().values()):
        raise ValueError("invalid_number")
    # Chinese abbreviations such as 一百二 can mean 120 or 102. Require an
    # explicit zero when positions are skipped instead of choosing a value.
    groups = match.groupdict()
    for name, higher, skipped in (
        ("bai", ("wan",), ("qian",)),
        ("shi", ("wan", "qian"), ("bai",)),
        ("ge", ("wan", "qian", "bai"), ("shi",)),
    ):
        if groups[name] is not None and any(groups[key] is not None for key in higher):
            if all(groups[key] is None for key in skipped):
                if match.start(name) == 0 or value[match.start(name) - 1] not in "零〇":
                    raise ValueError("ambiguous_number")
    result = 0
    for name, place in (("wan", 10000), ("qian", 1000), ("bai", 100), ("shi", 10), ("ge", 1)):
        digit = match.group(name)
        if digit is not None:
            result += (_CHINESE_DIGITS[digit] if digit else 1) * place
    return Decimal(result)


def _date(value, current):
    if value in ("今天", "今日"):
        return current.date()
    if value in ("昨天", "昨日"):
        return current.date() - timedelta(days=1)
    if value == "前天":
        return current.date() - timedelta(days=2)
    return datetime.strptime(value, "%Y-%m-%d").date()


def _stamp(day, clock):
    pieces = [int(piece) for piece in clock.split(":")]
    return datetime(day.year, day.month, day.day, *pieces, tzinfo=LOCAL_TIMEZONE)


def _remaining(text, match):
    """Keep a separator so material on opposite sides cannot form a token."""
    return text[:match.start()] + " " + text[match.end():]


def parse_window_request(text, *, now=None):
    """Return a single bounded duration/range, an error, or None.

    Ranges require a date on the first endpoint; a missing second date shares
    that date. Times are local UTC+8. Durations must be 1 minute to 30 days,
    and ranges must also end no later than ``now``. The caller decides whether
    the message is a request and handles source and permission selection.
    """
    if not isinstance(text, str) or not text:
        return None
    text = unicodedata.normalize("NFKC", text)
    if not _TIME_HINT.search(text):
        return None
    if len(text) > 1000:
        return dict(_INVALID)
    try:
        current = datetime.now(LOCAL_TIMEZONE) if now is None else now
        if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
            return dict(_INVALID)
        current = current.astimezone(LOCAL_TIMEZONE)

        ranges = list(_RANGE.finditer(text))
        durations = list(_DURATION.finditer(text))
        if len(ranges) + len(durations) != 1:
            return dict(_INVALID)
        match = (ranges or durations)[0]
        remaining = _remaining(text, match)
        if _TIME_HINT.search(remaining) or _EXTRA_SELECTOR.search(remaining):
            return dict(_INVALID)
        # A recognized interval cannot absorb an extra date, model selector
        # or other requested filter which this endpoint does not implement.
        residue = re.sub(
            r"cockpit|tokens?|肥鱼|小肥鱼|帮我|帮忙|请|麻烦你|麻烦|给我|发我|发给我|再|现在|先|立即|马上|我想"
            r"|查询|查看|查|看看|看|一下|统计|总结|生成|做|一份|一个|多少|花了|用了|的|了|把"
            r"|用量|开销|消耗|账单|报告|图表|手账|汇总|使用|成本|费用|情况|结果|图"
            r"|[\s,，。！？?!:：;；]", "", remaining, flags=re.IGNORECASE)
        if residue:
            return dict(_INVALID)
        # A suffix such as "前" turns a duration into a point in time, and
        # an arithmetic prefix or dangling digit makes the value ambiguous.
        before = text[:match.start()].rstrip()
        after = text[match.end():].lstrip()
        if re.search(r"(?:[0-9一二两三四五六七八九十.]\s*(?:[/+\-×*x]|到|至|或|和|加|减|乘|除|又)|[上下前后])$", before, re.IGNORECASE):
            return dict(_INVALID)
        if re.search(r"(?:下(?:一)?个|(?:之|以)后的|等(?:待)?|再过)\s*$", before):
            return dict(_INVALID)
        if re.search(r"(?<![A-Za-z])(?:in|before|after|or|and|plus|minus|to)$", before, re.IGNORECASE):
            return dict(_INVALID)
        if re.match(r"(?:前|后|之前|之后|以前|以后|半|左右|上下|多|余|以上|以下|[0-9.:]|(?:ago|after|later|ahead|from\s+now)\b)", after, re.IGNORECASE):
            return dict(_INVALID)
        if re.match(r"(?:[/+\-~—–×*]|到|至|或|和|及|、|加|减|乘|除|又)\s*[0-9一二两三四五六七八九十]", after):
            return dict(_INVALID)

        if durations:
            amount = _number(match.group("number"))
            unit = match.group("unit").lower()
            is_hours = unit in ("小时", "钟头") or unit.startswith("h")
            if match.group("half"):
                if not is_hours or amount != amount.to_integral_value():
                    return dict(_INVALID)
                amount += Decimal("0.5")
            seconds = amount * (Decimal(3600) if is_hours else Decimal(60))
            if not Decimal(60) <= seconds <= Decimal(30 * 86400):
                return dict(_INVALID)
            numeric = int(amount) if amount == amount.to_integral_value() else float(amount)
            return {"ok": True, "period": "window", "hours" if is_hours else "minutes": numeric}

        start_date = _date(match.group("start_date"), current)
        end_date = _date(match.group("end_date"), current) if match.group("end_date") else start_date
        start = _stamp(start_date, match.group("start_clock"))
        end = _stamp(end_date, match.group("end_clock"))
        seconds = (end - start).total_seconds()
        if end > current or not 60 <= seconds <= 30 * 86400:
            return dict(_INVALID)
        return {"ok": True, "period": "range", "start": start.isoformat(), "end": end.isoformat()}
    except (ValueError, TypeError, OverflowError, InvalidOperation):
        return dict(_INVALID)
