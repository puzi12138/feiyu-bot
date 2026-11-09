"""Song-name lookup and per-request chorus/full-song delivery."""
# ruff: noqa: BLE001

import asyncio
import importlib
import json
import re
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.message_components import Plain

FULL_SCOPE = r"整首(?:歌|曲)?|整曲|全曲|全首|全歌|完整版|完整(?:地|的)?(?:翻唱|唱)|从头(?:到|唱到)尾|\bfull(?:_song)?\b"
CHORUS_SCOPE = r"副歌(?:部分|片段)?|高潮(?:部分|片段)?|片段|一段|节选|\bchorus\b"
SCOPE_WORD = re.compile(f"(?P<full>{FULL_SCOPE})|(?P<chorus>{CHORUS_SCOPE})", re.IGNORECASE)


def explicit_cover_mode(text):
    """Prefer the last scope instruction, respecting nearby explicit negation."""
    text = re.sub(r"《[^》]*》|「[^」]*」|『[^』]*』", lambda m: " " * len(m[0]), str(text))
    chosen = None
    for match in SCOPE_WORD.finditer(text):
        value = "full" if match.group("full") else "chorus"
        prefix, suffix = text[max(0, match.start() - 10):match.start()], text[match.end():match.end() + 6]
        negative = (re.search(r"(?:不(?:要|用|必|想)?|别)(?:只|再|给我)?(?:翻唱|唱|来|做|用|要)?$", prefix)
                    or re.match(r"(?:就)?(?:不要|不用|不唱|别唱)", suffix))
        chosen = ("chorus" if value == "full" else "full") if negative else value
    return chosen


def clean_song_scope(song):
    """Remove command scope at the edges, retaining words inside a song title."""
    song = song.strip()
    # Quoted titles are unambiguous and may themselves contain '副歌'/'整首'.
    quoted = re.fullmatch(r"(?P<before>[^《]*)《(?P<title>[^》]+)》(?P<after>.*)", song)
    if quoted and not re.sub(SCOPE_WORD, "", quoted["before"] + quoted["after"]).strip(" ，,:：只唱要就的来请"):
        return quoted["title"].strip()
    scope = f"(?:{FULL_SCOPE}|{CHORUS_SCOPE})"
    song = re.sub(rf"^(?:(?:只要|只唱|就唱|唱|翻唱|来)\s*)?{scope}(?:翻唱)?[\s，,:：-]*", "", song, flags=re.IGNORECASE)
    song = re.sub(rf"(?:\s+|[，,:：]|只唱|只要|就唱|唱){scope}(?:就(?:好|行)|即可|就可以了)?[\s，。!！]*$", "", song, flags=re.IGNORECASE)
    if re.fullmatch(r"BV[A-Za-z0-9]{10}", song.strip()):
        return song.strip()
    return song.strip().removeprefix("《").removesuffix("》")


def normalize_cover_mode(value):
    value = str(value or "").strip().casefold()
    if not value:
        return "chorus"
    if value in {"full", "full_song", "整首", "整首歌", "全曲", "完整版"}:
        return "full"
    if value in {"chorus", "副歌", "片段", "一段", "高潮"}:
        return "chorus"
    raise ValueError("翻唱范围请选择副歌（chorus）或整首（full）。")


def choose_song(videos, song, artist=""):
    """Prefer clearly attributed originals, retaining ambiguity for the owner."""
    compact = lambda text: re.sub(r"[\W_]", "", text).casefold()
    title = compact(song)
    matches = [
        item
        for item in videos
        if title
        and title in compact(item.get("title", ""))
        and re.fullmatch(r"BV[A-Za-z0-9]{10}", item.get("bvid", ""))
        and not re.search(
            r"AI翻唱|伴奏|纯音乐|教学|合集|翻唱教程|总谱|鼓谱|编舞|Phira|SUNO",
            item.get("title", ""),
            re.IGNORECASE,
        )
    ]
    if artist:
        specific = [
            v
            for v in matches
            if compact(artist) in compact(v.get("title", "") + v.get("author", ""))
        ]
        if not specific:
            return None, matches
        matches = specific
    originals = [
        v
        for v in matches
        if re.search(
            r"官方|本家|原唱|Studio Live|Official|BML",
            v.get("title", ""),
            re.IGNORECASE,
        )
        and "翻唱" not in v.get("title", "")
    ]
    if originals:
        return originals[0], matches
    return (matches[0] if len(matches) == 1 else None), matches


class NamedCover:
    """Keep media lookup outside the language-model tool timeout."""

    def resolve_cover_request(self, event, song, mode=""):
        original = getattr(event, "get_extra", lambda *_: None)("feiyu_owner_original_text")
        text = original or getattr(event, "message_str", "")
        explicit = explicit_cover_mode(text)
        cleaned = clean_song_scope(song)
        song_mode = explicit_cover_mode(song) if cleaned != song.strip() else None
        song = cleaned
        pending = getattr(self, "state", {}).get("cover_pending_choice", {})
        # A bare BV selection retains the scope of the question it answers.
        previous = (pending.get("mode") if pending.get("session") == str(event.unified_msg_origin)
                    and time.time() < pending.get("expires", 0) and song in pending.get("bvids", []) else "")
        return song, normalize_cover_mode(explicit or song_mode or previous or mode)

    async def named_cover_job(self, event, plugin, song, artist, cover_mode="chorus"):
        """Resolve public audio, invoke native conversion and send a real record."""
        source = None
        try:
            if (await plugin._get_json("/health")).get("busy"):
                await event.send(
                    event.plain_result("翻唱服务正在处理歌曲，等完成后再点这一首吧。")
                )
                return
            if re.fullmatch(r"BV[A-Za-z0-9]{10}", song):
                chosen = {"bvid": song}
            else:
                found = await self.bili_read_tool(
                    "bili_search", {"keyword": f"{song} {artist} 真人", "num": 8}
                )
                chosen, candidates = choose_song(found.get("videos", []), song, artist)
                if not chosen:
                    lines = [
                        f"{v['title']}（{v.get('author', '')}）\n{v['bvid']}"
                        for v in candidates[:3]
                    ]
                    scope = "整首" if cover_mode == "full" else "副歌"
                    if candidates:
                        self.state["cover_pending_choice"] = {
                            "session": str(event.unified_msg_origin), "mode": cover_mode,
                            "bvids": [v["bvid"] for v in candidates[:3]], "expires": time.time() + 1800}
                        self.save()
                    text = (
                        f"找到多个版本，本次按{scope}翻唱；回复“点歌翻唱 {scope} BV号”选择：\n" + "\n".join(lines)
                        if lines
                        else "没有找到明确可用的歌曲版本，请补充歌手或 B 站 BV 号。"
                    )
                    await event.send(event.plain_result(text))
                    return
            process = await asyncio.create_subprocess_exec(
                "D:/QQBot/runtime/dsh-bili-scroll-venv/Scripts/python.exe",
                "D:/QQBot/scripts/BiliCoverSource.py",
                chosen["bvid"],
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                stdout, _ = await asyncio.wait_for(process.communicate(), 180)
            except TimeoutError:
                process.kill()
                await process.communicate()
                raise RuntimeError("Audio lookup timed out") from None
            receipt = json.loads(stdout.decode("utf-8").strip().splitlines()[-1])
            if process.returncode or not receipt.get("ok"):
                raise RuntimeError("Public audio unavailable")
            source = Path(receipt["path"]).resolve()
            root = Path("D:/QQBot/data/cover-input").resolve()
            if source.parent != root or source.suffix != ".m4a" or not source.is_file():
                raise ValueError("Invalid media receipt")
            await event.send(
                event.plain_result(
                    f"找到《{receipt['title']}》，"
                    + ("用荧翻唱整首，完成后发语音。" if cover_mode == "full" else "正在识别副歌，用荧只唱其中一段，完成后发语音。")
                    + f"\nhttps://www.bilibili.com/video/{chosen['bvid']}/"
                )
            )
            vocal, backing = plugin._resolve_gains(-1.0, -1.0)
            output, model, _, _ = await plugin._request_cover(
                str(source),
                "song.m4a",
                plugin._configured_model(),
                0,
                0,
                vocal,
                backing,
                1.0,
                cover_mode=cover_mode,
            )
            record = importlib.import_module(
                plugin.__class__.__module__
            ).OriginalFormatRecord
            if hasattr(self, "root"):
                try:
                    self.remember_cover(output, title=receipt["title"], mode=cover_mode,
                                        model=model, source_bvid=chosen["bvid"])
                except (OSError, ValueError):
                    logger.warning("Cover archive unavailable; original delivery continues")
            await event.send(
                event.chain_result(
                    [
                        Plain(f"《{receipt['title']}》{'整首' if cover_mode == 'full' else '副歌片段'}翻唱完成 · {model}"),
                        record.fromFileSystem(output),
                    ]
                )
            )
        except Exception as error:
            logger.warning("Feiyu named cover failed (%s)", type(error).__name__)
            if "chorus_not_found" in str(error):
                await event.send(
                    event.plain_result(
                        "这首歌暂时没可靠识别到副歌，不会拿别的段落代替。请直接发一段你想听的副歌音频，我再用荧翻唱。"
                    )
                )
                return
            await event.send(
                event.plain_result(
                    "这次点歌翻唱没完成：可能是音源不可用、超过10分钟或服务繁忙。可以换个版本/BV号，或直接发音频给我；没有生成成功的成品。"
                )
            )
        finally:
            if source and source.parent == Path("D:/QQBot/data/cover-input").resolve():
                source.unlink(missing_ok=True)
            self.cover_running = False
