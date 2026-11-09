"""Annotate actual local reading candidates with confirmed public receipts."""

import re
import sqlite3
from contextlib import closing
from pathlib import Path

from .heybox_journal_store import CANONICAL, _resolved_path


def comment_receipts(root, urls):
    """Read at most three posts' latest sent-comment metadata without a client.

    Published comments are selection context, never replacement evidence for
    the original post. Failed, uncertain and dry-run attempts are excluded.
    """
    if not isinstance(urls, list) or len(urls) > 3:
        return {}
    post_urls = {}
    for url in urls:
        match = CANONICAL.fullmatch(url) if isinstance(url, str) else None
        if match is None:
            return {}
        post_urls[match.group(1)] = url
    if not post_urls:
        return {}
    trusted_root = _resolved_path(Path(root))
    database_path = trusted_root / "data" / "astrbot" / "plugin_data" / "astrbot_plugin_xiaoheihe_adapter" / "xiaoheihe.db"
    if not database_path.exists():
        return {}
    for path in (database_path, *database_path.parents):
        if path == trusted_root:
            break
        if (
            path.is_symlink() or getattr(path, "is_junction", lambda: False)()
            or not _resolved_path(path).is_relative_to(trusted_root)
        ):
            return {}
    markers = ",".join("?" for _ in post_urls)
    try:
        with closing(sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True, timeout=5)) as database:
            database.row_factory = sqlite3.Row
            rows = database.execute(
                "SELECT id,post_id,content,external_comment_id,confirmed_at "
                "FROM outgoing_replies WHERE status='sent' "
                "AND confirmed_at IS NOT NULL AND external_comment_id!='' "
                "AND post_id IN (" + markers + ") ORDER BY id DESC LIMIT 60",
                tuple(post_urls),
            ).fetchall()
        result = {}
        for row in rows:
            post_id, comment_id = row["post_id"], row["external_comment_id"]
            if (
                post_id not in post_urls or post_urls[post_id] in result
                or not isinstance(comment_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", comment_id)
            ):
                continue
            content = row["content"]
            result[post_urls[post_id]] = {
                "confirmed_own_comment": True,
                "comment_receipt_id": row["id"],
                "external_comment_id": comment_id,
                "confirmed_at": row["confirmed_at"],
                "own_comment_excerpt": content[:600] if isinstance(content, str) else "",
                "evidence_scope": "Own published comment only; not original-post fact evidence.",
            }
        return result
    except (OSError, sqlite3.Error):
        return {}
