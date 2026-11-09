"""Draw a distribution of recorded, priced amounts from one local ledger.

The caller supplies already classified amounts.  Missing prices never become
zero, and no official-account difference is apportioned to these categories.
"""

from decimal import Decimal, InvalidOperation
import math

from .token_report_art import draw_card

COST_PIE_VERSION = "local-cost-pie-v2-current-report"
_MONEY_QUANTUM = Decimal("0.000001")
_ROUNDING_RADIUS = _MONEY_QUANTUM / 2
# Permit at most half a percentage point of uncertainty from the rounding
# performed by token_pricing._money for each priced category.
_MAX_SHARE_ERROR = Decimal("0.005")

# Each category has a stable color within the current report, including zero
# and unpriced categories; a legend always names every category explicitly.
_COLORS = (
    "#598bc6", "#b477be", "#4ca99c", "#d88867", "#8d9d54", "#db7795",
    "#677cc1", "#ba9960", "#668c90", "#aa70a2", "#76a46f", "#c57d71",
    "#5d97ac", "#ba879f", "#9484c6", "#9a9c61", "#dd9e5b", "#67a890",
    "#b36e80", "#778bb1", "#b69a89", "#699eaa",
)


def _amount(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        return None
    try:
        amount = Decimal(str(value))
        return amount if amount.quantize(_MONEY_QUANTUM) == amount else None
    except InvalidOperation:
        return None


def _shares_reliable(entries, total):
    """Bound every share using the category's six-decimal rounding interval."""
    if total <= 0:
        return False
    intervals = []
    for row in entries:
        amount = row["amount"]
        if amount is not None:
            intervals.append((amount, max(Decimal(0), amount - _ROUNDING_RADIUS), amount + _ROUNDING_RADIUS))
    lower_sum = sum((lower for _, lower, _ in intervals), Decimal(0))
    upper_sum = sum((upper for _, _, upper in intervals), Decimal(0))
    for amount, lower, upper in intervals:
        low_denominator = lower + upper_sum - upper
        high_denominator = upper + lower_sum - lower
        if low_denominator <= 0 or high_denominator <= 0:
            return False
        recorded = amount / total
        low = lower / low_denominator
        high = upper / high_denominator
        if max(abs(recorded - low), abs(high - recorded)) > _MAX_SHARE_ERROR:
            return False
    return True


def make_cost_distribution(entries, *, priced_calls, unpriced_calls, expected_amount,
                           tolerance=Decimal(0)):
    """Validate category amounts and return a presentation-only projection.

    A category without a priced call carries ``amount=None``; its numeric zero
    in an aggregation object is an empty accumulator, not a zero-cost request.
    Category amounts supply the denominator, allowing bounded rounding already
    accepted by the project report's own reconciliation to remain explicit.
    """
    unavailable = {"state": "unavailable", "entries": []}
    if (not isinstance(entries, list) or len(entries) > 64
            or type(priced_calls) is not int or priced_calls < 0
            or type(unpriced_calls) is not int or unpriced_calls < 0
            or not isinstance(tolerance, Decimal) or not tolerance.is_finite() or tolerance < 0):
        return unavailable
    expected = _amount(expected_amount) if expected_amount is not None else None
    if ((expected_amount is not None and expected is None) or (priced_calls and expected is None)
            or (not priced_calls and expected not in (None, Decimal(0)))):
        return unavailable
    clean, keys = [], set()
    for entry in entries:
        if not isinstance(entry, dict):
            return unavailable
        key, label = entry.get("key"), entry.get("label")
        calls, priced, unpriced = (entry.get(name) for name in ("calls", "priced_calls", "unpriced_calls"))
        if (not isinstance(key, str) or not key or key in keys or not isinstance(label, str) or not label
                or any(type(value) is not int or value < 0 for value in (calls, priced, unpriced))
                or priced + unpriced != calls):
            return unavailable
        keys.add(key)
        raw = entry.get("amount")
        amount = _amount(raw) if raw is not None else None
        if (priced and amount is None) or (not priced and (raw is not None and amount != 0)):
            return unavailable
        clean.append({"key": key, "label": label, "calls": calls,
                      "priced_calls": priced, "unpriced_calls": unpriced,
                      "amount": amount if priced else None})
    if (sum(row["priced_calls"] for row in clean) != priced_calls
            or sum(row["unpriced_calls"] for row in clean) != unpriced_calls):
        return unavailable
    total = sum((row["amount"] for row in clean if row["amount"] is not None), Decimal(0))
    if expected is not None and abs(total - expected) > tolerance:
        return unavailable
    if total > 0:
        state = "positive" if _shares_reliable(clean, total) else "insufficient_precision"
    else:
        state = "zero" if priced_calls else "no_priced"
    return {"state": state, "entries": clean, "total": total,
            "priced_calls": priced_calls, "unpriced_calls": unpriced_calls}


def cost_card_height(distribution):
    entries = distribution.get("entries", []) if isinstance(distribution, dict) else []
    return max(520, 170 + ((len(entries) + 1) // 2) * 57 + 65)


def _percentage(amount, total):
    if amount is None or total <= 0:
        return "—"
    percent = amount * Decimal(100) / total
    if 0 < percent < Decimal("0.01"):
        return "<0.01%"
    if Decimal("99.99") < percent < 100:
        return ">99.99%"
    return f"{percent:.2f}%"


def draw_cost_card(draw, box, distribution, theme, font, *, title):
    """Draw an explicit, separately scoped amount donut and full legend."""
    x1, y1, x2, y2 = box
    draw_card(draw, box, theme)
    ink, muted = theme["ink"], theme["muted"]

    def text(x, y, value, size, color=ink, bold=False, max_width=None):
        value = str(value)
        face = font(size, bold)
        while max_width and face.getlength(value) > max_width and size > 15:
            size -= 1
            face = font(size, bold)
        draw.text((int(x), int(y)), value, fill=color, font=face)

    text(x1 + 36, y1 + 25, title, 32, bold=True)
    state = distribution.get("state")
    if state == "unavailable":
        text(x1 + 36, y1 + 104, "金额分类暂不可用，无法计算占比。", 26, muted)
        text(x1 + 36, y1 + 156, "未计价的请求不会当作零成本。", 23, muted)
        return
    total = distribution["total"]
    priced, unpriced = distribution["priced_calls"], distribution["unpriced_calls"]
    if priced:
        text(x1 + 36, y1 + 77, f"分母：本账本各分类已计价金额记录相加 ¥{total:,.6f}",
             23, muted, max_width=x2-x1-72)
        detail = f"金额保留六位，可能舍入 · 官方单价估算 · 已计价 {priced:,} 次 · 未计价 {unpriced:,} 次不计入分母"
    else:
        text(x1 + 36, y1 + 77, "没有可用于分布的已计价金额记录。", 23, muted)
        detail = f"未计价 {unpriced:,} 次不会当作零成本" if unpriced else "这个时段没有已计价请求。"
    text(x1 + 36, y1 + 112, detail, 21, muted, max_width=x2-x1-72)

    bbox = (x1 + 58, y1 + 162, x1 + 362, y1 + 466)
    draw.ellipse(bbox, fill=theme["pale"])
    if state == "positive":
        angle = -90.0
        positive = [row for row in distribution["entries"] if row["amount"] is not None and row["amount"] > 0]
        for index, row in enumerate(distribution["entries"]):
            if row["amount"] is None or not row["amount"]:
                continue
            end = 270.0 if row is positive[-1] else angle + float(row["amount"] / total * 360)
            draw.pieslice(bbox, angle, end, fill=_COLORS[index % len(_COLORS)])
            angle = end
    draw.ellipse((x1 + 113, y1 + 217, x1 + 307, y1 + 411), fill="#ffffff")
    if state == "positive":
        text(x1 + 142, y1 + 279, "已计价", 24, muted)
        text(x1 + 133, y1 + 318, "金额占比", 24, ink, True)
    elif state == "insufficient_precision":
        text(x1 + 129, y1 + 280, "六位记录", 23, muted)
        text(x1 + 129, y1 + 321, "精度不足", 23, muted)
    else:
        text(x1 + 141, y1 + 291, "占比", 25, muted)
        text(x1 + 125, y1 + 331, "不可计算", 23, muted)

    entries = distribution["entries"]
    rows_per_column = (len(entries) + 1) // 2
    legend_x = x1 + 424
    column_width = (x2 - legend_x - 25) / 2
    for index, row in enumerate(entries):
        column, line = divmod(index, rows_per_column)
        x, y = legend_x + column * column_width, y1 + 164 + line * 57
        color = _COLORS[index % len(_COLORS)] if row["amount"] is not None else "#b8bfca"
        draw.rounded_rectangle((x, y + 5, x + 17, y + 22), radius=4, fill=color)
        text(x + 27, y - 2, row["label"], 23, bold=True, max_width=column_width-32)
        if row["amount"] is None:
            value = f"—  ·  未计价 {row['unpriced_calls']:,} 次" if row["calls"] else "无请求"
        elif not row["amount"]:
            value = "记录 ¥0.000000  ·  可能因舍入为零"
            if row["unpriced_calls"]:
                value += f"  ·  未计价 {row['unpriced_calls']:,} 次"
        else:
            percent = _percentage(row["amount"], total) if state == "positive" else "精度不足"
            value = f"¥{row['amount']:,.6f}  ·  {percent}"
            if row["unpriced_calls"]:
                value += f"  ·  未计价 {row['unpriced_calls']:,} 次"
        text(x + 27, y + 27, value, 19, muted, max_width=column_width-32)
    if state == "zero":
        text(x1 + 58, y1 + 470, "六位金额记录合计为零，实际可能为零或低于保留精度；无法计算占比。", 19, muted,
             max_width=x2-x1-100)
    elif state == "insufficient_precision":
        text(x1 + 58, y2 - 34, "六位金额舍入可能使占比偏差超过 0.5 个百分点，暂不计算占比。", 19, muted,
             max_width=x2-x1-100)
    elif state == "positive":
        text(x1 + 58, y2 - 34, "占比按已计价金额记录计算，百分比保留两位小数。", 19, muted,
             max_width=x2-x1-100)
