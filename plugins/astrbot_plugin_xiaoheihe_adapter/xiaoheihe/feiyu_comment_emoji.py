"""Reviewed native Heybox stickers; no network, uploads or new send path.

Catalog: /bbs/app/api/emojis/list, 2026-11-05 (public read, status=ok).
Audit SHA256: 2b59f7c70048cf8cc3cf07ea0fdedc0c3ba12d16c102a8387b4df1eb1c914568.
Shortcodes follow the archived comment editor, not invented image URLs.
"""
from __future__ import annotations

import re

# Keep the reviewed subset local: a missing/changed remote catalog must not
# add network dependencies to every comment. Updates require another review.
NATIVE_EMOJIS = (
    ("[cube_开心]", 1, "开心"),
    ("[cube_笑cry]", 1, "笑出眼泪"),
    ("[cube_比心]", 1, "友善感谢"),
    ("[cube_加油]", 1, "鼓励"),
    ("[cube_惊讶]", 1, "惊讶"),
    ("[cube_吃瓜]", 1, "轻松围观"),
    ("[cube_鼓掌]", 1, "赞赏"),
    ("[heygirl_敲开心]", 3, "盒娘开心"),
    ("[heygirl_喜欢]", 3, "盒娘喜欢"),
    ("[heygirl_哈哈]", 3, "盒娘大笑"),
    ("[heygirl_记下来]", 3, "盒娘认真记录"),
    ("[heygirl_疑问]", 3, "盒娘疑问"),
    ("[bigemoji_比心]", 2, "比心感谢"),
    ("[bigemoji_哈哈]", 2, "开怀大笑"),
    ("[bigemoji_厉害]", 2, "赞叹"),
    ("[bigemoji_吃瓜]", 2, "轻松围观"),
    ("[bigemoji_羡慕]", 2, "羡慕"),
    ("[grandemoji_整挺好]", 2, "认可夸赞"),
    ("[grandemoji_摸不着头脑]", 2, "困惑"),
)
EMOJI_TYPES = {token: kind for token, kind, _ in NATIVE_EMOJIS}
MAX_NATIVE_EMOJIS = 2
MAX_BIG_EMOJIS = 1
_NATIVE_OR_PLACEHOLDER = re.compile(
    r"\[(?:(?:cube|heygirl|bigemoji|grandemoji)_[^\[\]\r\n]*"
    r"|(?:表情|emoji|face)[:：][^\[\]\r\n]*)\]",
    re.I,
)
_MARKER_PREFIX = re.compile(
    r"\[(?:(?:cube|heygirl|bigemoji|grandemoji)_|(?:表情|emoji|face)[:：])",
    re.I,
)


def comment_emoji_prompt() -> str:
    small = "；".join(f"{token}（{label}）" for token, kind, label in NATIVE_EMOJIS if kind != 2)
    big = "；".join(f"{token}（{label}）" for token, kind, label in NATIVE_EMOJIS if kind == 2)
    return "\n".join((
        '<xiaoheihe_native_emoji_policy trust="trusted">',
        "你可以在自然的小黑盒评论中按语境选用平台原生表情包，保持肥鱼原有人格。",
        "先认真回应帖子；没有合适的就不用，不要每条硬加或用表情替代有内容的回应。",
        "每条最多2个原生表情，其中大图表情包最多1个；同一表情不要重复。",
        "严肃求助、事故、哀伤或冲突话题不玩梗、不嘲讽；不适合时直接用文字。",
        "仅可原样复制下面的完整方括号代码；平台负责显示图片，不需调用任何工具。",
        "不要编造代码，不输出图片链接、Markdown图片、HTML或‘[表情:笑]’占位说明。",
        "小表情：" + small,
        "大图表情包：" + big,
        '</xiaoheihe_native_emoji_policy>',
    ))


def _bracket_pairs(text: str) -> dict[int, int]:
    """Pair brackets in one pass, without recursion or crossing a newline."""
    pairs: dict[int, int] = {}
    stack: list[int] = []
    for index, char in enumerate(text):
        if char in "\r\n":
            stack.clear()
        elif char == "[":
            stack.append(index)
        elif char == "]" and stack:
            pairs[stack.pop()] = index
    return pairs


def sanitize_native_emojis(text: str, max_chars: int) -> str:
    """Bound native tokens without cutting one in half or changing other tags.

    Unknown native/placeholder codes are discarded, never repaired by guessing.
    Plain bracketed prose, redaction markers and ordinary Unicode emoji stay as
    text. This is formatting only; authorization and send receipts stay outside.
    """
    text = str(text)
    pairs = _bracket_pairs(text)
    pieces: list[str] = []
    seen: set[str] = set()
    count = big_count = 0
    cursor = 0
    while cursor < len(text):
        prefix = _MARKER_PREFIX.match(text, cursor) if text[cursor] == "[" else None
        if prefix is None:
            pieces.append(text[cursor])
            cursor += 1
            continue

        closing = pairs.get(cursor)
        if closing is None:
            # An unfinished marker ends at the next word boundary. Keep the
            # following prose and newline instead of swallowing the whole line.
            end = prefix.end()
            while end < len(text) and not text[end].isspace():
                end += 1
            token = ""
        else:
            end = closing + 1
            # A nested marker is malformed, even if its innermost code is valid.
            token = text[cursor:end] if text.find("[", cursor + 1, closing) < 0 else ""
        kind = EMOJI_TYPES.get(token)
        if (kind is not None and token not in seen and count < MAX_NATIVE_EMOJIS
                and (kind != 2 or big_count < MAX_BIG_EMOJIS)):
            pieces.append(token)
            seen.add(token)
            count += 1
            big_count += int(kind == 2)
        elif (pieces and end < len(text)
              and not pieces[-1][-1].isspace() and not text[end].isspace()):
            # Deletion must not join e.g. '[cube' + '_开心]' into a new code.
            # This barrier also makes arbitrary nesting stable in a single pass.
            pieces.append(" ")
        cursor = end
    cleaned = "".join(pieces).strip()
    limit = max(1, int(max_chars))
    # Move a cut inside a token to its start. Never emit a broken shortcode.
    cut = min(len(cleaned), limit)
    for match in _NATIVE_OR_PLACEHOLDER.finditer(cleaned):
        if match.start() < cut < match.end():
            cut = match.start()
            break
    return cleaned[:cut].rstrip()
