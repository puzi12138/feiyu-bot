"""Independent SQLite race checks for the isolated receipt candidate."""
import asyncio
import importlib.util
from pathlib import Path
import tempfile
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
TEST = HERE / "test_heybox_reconciliation.py"
spec = importlib.util.spec_from_file_location("receipt_fixture", TEST)
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
from astrbot_plugin_xiaoheihe_adapter.xiaoheihe.database import Database
from astrbot_plugin_xiaoheihe_adapter.xiaoheihe.repository import Repository
from astrbot_plugin_xiaoheihe_adapter.xiaoheihe.unknown_receipts import stored_attempt


async def main():
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "fixture.sqlite"
        first, second = Database(path), Database(path)
        await first.open()
        await second.open()
        a, b = Repository(first), Repository(second)
        route = fixture.RoutingTarget("default", "12345")
        async def seed():
            outgoing = await a.record_outgoing_attempt(
                "default", None, route, "ABCDE", "send_unknown"
            )
            expected = await stored_attempt(a, route, "ABCDE", outgoing)
            assert expected is not None
            return outgoing, expected

        outgoing, expected = await seed()
        replies = await asyncio.gather(
            a.confirm_outgoing(outgoing, "101", expected_attempt=expected),
            b.confirm_outgoing(outgoing, "102", expected_attempt=expected),
        )
        assert sum(result is not None for result in replies) == 1
        stored = await first.fetchone(
            "SELECT status,external_comment_id FROM outgoing_replies WHERE id=?", (outgoing,)
        )
        assert stored["status"] == "sent" and stored["external_comment_id"] in {"101", "102"}
        ids = await first.fetchall("SELECT external_comment_id FROM self_comment_ids")
        assert [row["external_comment_id"] for row in ids] == [stored["external_comment_id"]]

        for field, value in (("external_comment_id", "103"), ("confirmed_at", 1700000000)):
            outgoing, expected = await seed()
            await second.execute(f"UPDATE outgoing_replies SET {field}=? WHERE id=?", (value, outgoing))
            result = await a.confirm_outgoing(outgoing, "104", expected_attempt=expected)
            assert result is None
            stored = await first.fetchone(
                "SELECT status,external_comment_id,confirmed_at FROM outgoing_replies WHERE id=?",
                (outgoing,),
            )
            assert stored["status"] == "send_unknown" and stored[field] == value
            assert not await first.fetchone(
                "SELECT * FROM self_comment_ids WHERE external_comment_id='104'"
            )
        await second.close()
        await first.close()
    print("atomic_receipt_races_ok")


if __name__ == "__main__":
    async def guarded():
        with patch("socket.socket.connect", side_effect=AssertionError("network blocked")), \
             patch("socket.socket.connect_ex", side_effect=AssertionError("network blocked")):
            await main()
    asyncio.run(guarded())
