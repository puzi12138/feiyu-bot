"""Resolve owner usage requests without performing I/O or trusting model scope.

The model may identify a request and relay an unsupported filter, but source,
time, and feature selections come from the current utterance or a verified
immediately preceding owner query. An omitted source on a new request defaults
to both local ledgers; an anaphoric follow-up never guesses a missing scope.
"""
import re
import unicodedata

from .cockpit_window_request import (_DURATION, _EXTRA_SELECTOR, _RANGE,
                                     _TIME_HINT, parse_window_request)


WINDOW_ROUTE_PATTERN = (r"(?is)^(?=.*(?:tokens?|用量|开销|消耗|账单|费用|花费|花了|用了|"
                        r"项目账本|两边|两者|昨天呢|今天呢|(?:那|再).{0,20}(?:呢|查|看|列|多少))).*$")
_USAGE = re.compile(r"tokens?|用量|项目账本|消耗|开销|账单|费用|花费|耗费|成本|用费|用掉", re.I)
_ACTION = re.compile(r"请问|查询|查|看看|看下|看一眼|看|瞅|瞧|统计|算算|核实|确认|总结|生成|做一份|给我|发我|发给我|"
                     r"列|展示|告诉|说说|知道|了解|多少|几个|几次|怎么样|怎样|咋样|如何", re.I)
_QUESTIONED_FACT = re.compile(r"(?:用了|花了|消耗了|耗了).*(?:吗|么|呢|[?？])|"
                              r"(?:吗|么|呢|[?？]).*(?:用了|花了|消耗了|耗了)", re.I)
_NO_ACTION = re.compile(
    r"不要|别(?:再|给我|帮我)?(?:查|看|发|列|统计|算)|(?:不|没|没有|未|从未)(?:要|用|必|想|需要|是让你|是要你|要你|给我|让你|叫你|说|曾)?"
    r".{0,12}(?:查|看|发|列|统计|算|报告)"
    r"|(?:查|看|发|列|统计|算).{0,12}(?:不需要|不用|无需|没必要|没有必要|算了|作罢)"
    r"|无需|停止|取消|禁止|暂缓|推迟|算了|不要发|不用发|没让你|没有让你"
    r"|为什么|为何|如何(?:查|看|算|统计|实现|获取|查询|计算|使用)|怎么(?!样)|查不到|查询不了|报错|故障|有问题"
    r"|如果|假如|假设|假想|要是|例如|比如|举例|讨论|解释|科普|教程"
    r"|[‘’“”\"'「」『』]", re.I)
_REQUEST_MODAL = re.compile(r"能不能|可不可以|能否")
_FOLLOW = re.compile(r"^(?:(?:那|那么|再|还有|同样|换成|换为|改成|改为|这次|这回|按(?:照)?|分别|都列|都查)"
                     r"|.{0,8}(?:两边|两者|两个都|都查|一起查)|.{0,16}(?:今天|今日|昨天|昨日|前天|最近|过去).{0,8}呢)", re.I)
_FEATURES = (
    (r"小黑盒(?:的)?评论", "heybox_comment"), (r"小黑盒(?:的)?(?:图片识别|识图)", "heybox_vision"),
    (r"小黑盒", "heybox"), (r"B站(?:的)?评论", "bili_comment"),
    (r"B站(?:的)?(?:阅读|摘要)", "bili_summary"), (r"B站(?:的)?(?:图片识别|识图)", "bili_vision"),
    (r"B站", "bili"), (r"QQ空间(?:的)?(?:文案|草稿)", "qzone_draft"),
    (r"QQ空间(?:的)?(?:评论|接话)", "qzone_reply"), (r"QQ空间", "qzone"),
    (r"(?:QQ)?私聊(?:对话)?", "private_chat"), (r"(?:QQ)?群聊(?:对话)?", "group_chat"),
    (r"QQ聊天", "qq_chat"), (r"自主决策", "autonomous_plan"),
    (r"记忆整理", "memory"), (r"对话压缩", "context_compression"),
    (r"头像识图", "qq_profile_vision"), (r"表情包识别", "meme_analysis"),
    (r"语音文本", "voice_text"), (r"视频音画", "video_media"), (r"HLTV", "hltv"),
)
_ALL_FEATURES = re.compile(r"全部功能|所有功能|各(?:个)?功能|每(?:个|项)功能|不同功能")
_ALL_PURPOSES = re.compile(r"全部用途|所有用途|各(?:个)?用途|每(?:个|项)用途|全部项目|所有项目|各(?:个)?项目")
_FILTER = re.compile(r"排除|剔除|去掉|不含|不算|不包括|不需要|不用|不必|除外|除去|除了|以外|以外的|以外都|仅限|"
                     r"大于|小于|高于|低于|超过|不少于|至少|至多|多于|少于|排名|排序|最高|最低|最贵|最便宜|占比|百分比|比例|"
                     r"(?:按|按照|区分)(?:照)?(?:模型|供应商|厂商|价格|金额|费用|状态|密钥|key|账户|账号|调用方|线程|会话)|"
                     r"(?:按|按照|每|各)(?:个|种|不同)?(?:时间段|时段|时辰|时刻)|"
                     r"(?:异常|错误)(?:的)?(?:请求|调用|用量)|(?:语音|音频)(?:生成|合成|转写)|耗时|"
                     r"模型|供应商|厂商|密钥|账户|账号|调用方|线程|会话|失败|成功|取消|未回传|已回传|非缓存|缓存(?:的|请求|调用|部分|用量|token)", re.I)
# These details are not independently represented by the platform groups.
# Check them before matching broad aliases so a narrower request stays narrow.
_UNSUPPORTED_FEATURE_DETAIL = re.compile(
    r"小黑盒\s*(?:的\s*)?(?:(?:功能|方面|相关)(?:的)?\s*)?"
    r"(?:发帖|发贴|投稿|发文|发布|发表|发动态|帖子发布|文章发布|浏览|阅读|点赞|文案|图片生成)"
    r"|B站\s*(?:的\s*)?(?:点赞|投币|收藏|关注|浏览|投稿|发帖|弹幕)", re.I)
_SOURCE_NAME = r"(?:cockpit|codex|项目用量|项目账本|官方网站|官方网页|官方账单|官网|official|肥鱼|feiyu|qqbot)"
_SELF_USAGE = re.compile(
    r"(?:你自己的?|你自身的?|你本人的?|你的|肥鱼自己的?)"
    r"(?:(?![，,。！？?!:：;；]|查|看|统计|帮|试试).){0,12}?"
    r"(?:tokens?|用量|开销|消耗|账单|费用|花费|成本)"
    r"|(?:你|肥鱼)(?:自己|自身)?(?:今天|今日|昨天|昨日)(?:的)?"
    r"(?:tokens?|用量|开销|消耗|账单|费用|花费|成本)"
    r"|(?:你|肥鱼)(?:自己|自身)?\s*(?:(?:这|最近|过去|近)\s*)?"
    r"[零〇一二两三四五六七八九十百\d]+\s*(?:个\s*)?(?:小时|钟头|分钟|天)\s*(?:的)?\s*"
    r"(?:tokens?|用量|开销|消耗|账单|费用|花费|成本)"
    r"|(?:你|肥鱼)(?:自己|自身)?(?:(?![，,。！？?!:：;；]|查|看|统计|帮|试试).){0,20}?"
    r"(?:花了|花费|花掉|消耗了?|耗了|用了|使用了?|用掉)"
    r"(?:(?![，,。！？?!:：;；]).){0,20}?(?:tokens?|用量|消耗|开销|账单|费用|花费|成本)", re.I)
_NEGATED_SOURCE = re.compile(
    _SOURCE_NAME + r"\s*(?:的)?\s*(?:不需要|不要|不用|不必|不查|不看|除外|不是|无关|跳过|忽略|免了)"
    r"|(?:(?<!是)不是|并非|非|不需要|不要|不用|无需|不必|不查|不看|不计|不含|不包括|"
    r"排除|去掉|除去|忽略|跳过|别把|不把)\s*(?:查|看|来自)?\s*" + _SOURCE_NAME,
    re.I)
_TIME_UNKNOWN = re.compile(r"前天|明天|明日|后天|本周|这周|上周|下周|本月|上月|下月|今年|去年|明年|"
                           r"上个月|这个月|下个月|上午|下午|早上|晚上|中午|凌晨|昨晚|今晚|今早|"
                           r"(?:上|下|本|这|前|后)(?:一|个)?(?:季(?:度)?|周|星期|礼拜|月|半年|年)|"
                           r"(?:周|星期|礼拜)[一二三四五六日天]|周末|工作日|"
                           r"上半年|下半年|[春夏秋冬](?:季|天)|"
                           r"白天|白昼|夜晚|夜间|夜里|半夜|午夜|清晨|早晨|傍晚|黄昏|深夜|早间|晚间|日间|半天|工作时间|"
                           r"历史|累计|之前|以后|将来|未来|接下来|刚才|刚刚|这段时间|那段时间|前一阵|一会儿|"
                           r"[一二两三四五六七八九十百\d]+(?:天|日|周|星期|个月|年)|"
                           r"(?:最近|过去|近期|近来)(?!\s*(?:的)?\s*$)", re.I)


def _fail(reason, source=None):
    return {"ok": False, "reason": reason, **({"source": source} if source else {})}


def _source(text):
    # A name addressing the bot is not a ledger selector.
    subject = re.sub(r"^\s*(?:大肥鱼|小肥鱼|肥鱼)(?=[，,：:\s]|你|请|麻烦|帮|查|看|今天|今日|昨天|昨日)", "", text, flags=re.I)
    cockpit = bool(re.search(r"cockpit|codex|项目用量|项目账本", subject, re.I))
    official = bool(re.search(r"官网|官方网站|官方网页|官方账单|official", subject, re.I))
    if re.search(r"deepseek|chatgpt|openai|claude|gemini|copilot|豆包|千问|qwen|kimi|智谱", subject, re.I) and not official:
        return _fail("ambiguous_source")
    paper = bool(re.search(r"论文(?:项目|用途)?", subject))
    project_fish = bool(re.search(r"肥鱼(?:相关|项目|用途)", subject))
    source_text = re.sub(r"肥鱼(?:相关|项目|用途)", "", subject, flags=re.I)
    fish_match = re.search(r"(?:肥鱼|feiyu|qqbot)", source_text, re.I)
    cockpit_match = re.search(r"(?:cockpit|codex|项目用量|项目账本)", source_text, re.I)
    fish_name = bool(fish_match)
    if fish_match and cockpit_match:
        first, second = sorted((fish_match, cockpit_match), key=lambda item: item.start())
        between_names = source_text[first.end():second.start()]
        joined_names = bool(re.search(r"和|与|以及|及|、|,|，|还有|跟|同", between_names))
    else:
        joined_names = False
    self_usage = bool(_SELF_USAGE.search(text))
    joined_self = bool(re.search(r"(?:你|你的|你自己).{0,4}(?:和|与|及).{0,4}(?:cockpit|codex|项目用量|项目账本)|"
                                 r"(?:cockpit|codex|项目用量|项目账本).{0,4}(?:和|与|及).{0,4}(?:你|你的|你自己)", subject, re.I))
    fish = fish_name or self_usage or joined_self
    no_duration = _RANGE.sub(" ", _DURATION.sub(" ", subject))
    both = bool(re.search(r"两边|两者|两(?:个|种)(?:来源|账本)|双方", no_duration))
    if cockpit and self_usage and not (fish_name or both or joined_self):
        return _fail("ambiguous_source")
    if cockpit and fish_name and not (both or joined_names):
        return _fail("ambiguous_source")
    if re.search(r"两份", no_duration) and not both:
        return _fail("ambiguous_source")
    if re.search(r"(?:都查|一起查|同时查|联合查询)", no_duration) and not (both or cockpit and fish or paper and project_fish):
        return _fail("ambiguous_source")
    if official and (cockpit or fish or paper or both or project_fish):
        return _fail("ambiguous_source")
    if (re.search(r"肥鱼相关", subject) and not (cockpit or paper or both)
            and not re.search(r"肥鱼相关(?:的)?(?:项目|用途)", subject)):
        return _fail("ambiguous_source")
    if paper and fish and not (cockpit or both):
        return _fail("ambiguous_source")
    if both and paper and project_fish and not cockpit:
        return _fail("ambiguous_source")
    if official:
        return {"source": "official", "explicit": True}
    if both or (cockpit and fish):
        return {"source": "both", "explicit": True}
    if cockpit:
        return {"source": "cockpit", "explicit": True}
    if fish:
        return {"source": "feiyu", "explicit": True}
    if paper or project_fish:
        return {"source": "cockpit", "explicit": False}
    return {"source": None, "explicit": False}


def _period(text):
    ranges, durations = list(_RANGE.finditer(text)), list(_DURATION.finditer(text))
    if len(ranges) + len(durations) > 1:
        return _fail("ambiguous_period")
    if ranges or durations:
        match = (ranges or durations)[0]
        outside = text[:match.start()] + " " + text[match.end():]
        before, after = text[:match.start()].rstrip(), text[match.end():].lstrip()
        if (_TIME_HINT.search(outside) or _EXTRA_SELECTOR.search(outside) or _TIME_UNKNOWN.search(outside)
                or re.search(r"(?:约|大约|大概|超过|少于|多于|至少|至多|之前|以后|未来|再过|下(?:一)?个|前)$", before)
                or re.search(r"(?:[0-9一二两三四五六七八九十.]\s*(?:[/+\-×*x]|到|至|或|和|加|减|乘|除|又)|[上下前后])$", before, re.I)
                or re.match(r"(?:前|后|以前|以后|之前|之后|半|左右|上下|多|余|以上|以下|[\d.:])", after)
                or re.match(r"(?:[/+\-~—–×*]|到|至|或|和|及|、|加|减|乘|除|又)\s*[0-9一二两三四五六七八九十半]", after)):
            return _fail("ambiguous_period")
        return parse_window_request(match.group(0)) or _fail("unsupported_period")
    selectors = []
    for period, pattern in (("today", r"今天|今日"), ("yesterday", r"昨天|昨日"),
                            ("last7", r"近\s*(?:7|七)\s*天|(?:最近|过去)\s*(?:的)?\s*(?:7|七)\s*天")):
        selectors.extend({"ok": True, "period": period} for _ in re.finditer(pattern, text))
    for match in re.finditer(r"\d{4}-\d{2}-\d{2}", text):
        selectors.append({"ok": True, "period": "day", "day": match.group()})
    residue = re.sub(r"今天|今日|昨天|昨日|近\s*(?:7|七)\s*天|(?:最近|过去)\s*(?:的)?\s*(?:7|七)\s*天|\d{4}-\d{2}-\d{2}", " ", text)
    if len(selectors) > 1:
        return _fail("ambiguous_period")
    if _TIME_HINT.search(residue) or _TIME_UNKNOWN.search(residue) or re.search(r"\d", residue):
        return _fail("unsupported_period")
    return selectors[0] if selectors else None


def _features(text):
    rest, items = text, []
    for pattern, name in _FEATURES:
        if re.search(pattern, rest, re.I):
            items.append(name)
            rest = re.sub(pattern, " ", rest, flags=re.I)
    if _ALL_FEATURES.search(text) and (not items or re.search(r"等(?:各(?:个)?|全部|所有)功能", text)):
        items = []
    return ",".join(items)


def _purposes(text):
    if _ALL_PURPOSES.search(text):
        return []
    found = []
    if re.search(r"论文(?:项目|用途)?", text):
        found.append("academic_poc")
    if re.search(r"肥鱼(?:相关|项目|用途)", text):
        found.append("feiyu_bot")
    return found


def _direct_confident(text):
    """Defer uncommon wording to the existing conversation model, no extra call."""
    residual = text
    for pattern, _ in _FEATURES:
        residual = re.sub(pattern, " ", residual, flags=re.I)
    residual = _RANGE.sub(" ", _DURATION.sub(" ", residual))
    residual = re.sub(r"\d{4}-\d{2}-\d{2}|近\s*(?:7|七)\s*天|(?:最近|过去)\s*(?:的)?\s*(?:7|七)\s*天|今天|今日|昨天|昨日", " ", residual)
    residual = re.sub(
        r"tokens?|cockpit|codex|项目用量|项目账本|deepseek|官网|官方网站|官方网页|官方账单|official|qqbot|feiyu|"
        r"大肥鱼|小肥鱼|肥鱼相关|肥鱼项目|肥鱼用途|肥鱼|论文项目|论文用途|论文|"
        r"你自己的|我自己的|你自己|自己的|你的|我的|你|我|本机|本地|"
        r"麻烦你|麻烦|帮我|帮忙|给我|发我|发给我|请问|请|现在|立即|马上|我想问问|我想|想知道|想了解|能不能|可不可以|能否|试试|再|先|那|那么|"
        r"查询|查看|查一查|看看|看一看|看一下|看下|看一眼|查|看|瞅一眼|瞅瞅|瞧瞧|瞧一眼|统计|算一算|算算|核实|总结|生成|展示|列出来|列出|列|做|给|发|告诉|说说|知道|了解|"
        r"按照|分别|分开|分项|明细|分类|种类|全部|所有|功能|用途|项目|等|各项|各个|各自|各|每个|"
        r"两个来源|两种来源|两个账本|两种账本|两边|两者|两份|双方|一起|同时|联合|都|只|仅|来源|"
        r"用量|开销|消耗|账单|报告|手账|图表|花费|费用|成本|使用|情况|多少|几个|几次|用了|花了|消耗了|耗了|"
        r"对账差额|标出来|总计|总共|总数|总量|一共|汇总|总|一下|并|把|的|了|是否|是|和|与|及|以及|按|呀|啊|呢|吧|嘛|么|吗|呗|这|一眼|个|次|怎么样|怎样|咋样|如何|状态|"
        r"正常|运行|服务|记录|健康|可用|谢谢|多谢|稍微|大概说说|帮我看看",
        " ", residual, flags=re.I)
    return not re.search(r"[^\s,，。！？?!:：;；]", residual)


def resolve_usage_intent(text, *, recent=None, via_model=False, request_kind="query", unsupported_filter=""):
    if not isinstance(text, str) or not text.strip() or len(text) > 1000:
        return None
    text = unicodedata.normalize("NFKC", text).strip()
    # Remove only question modals for the negation check: the remaining text
    # still rejects requests such as "能不能不要查".
    if _NO_ACTION.search(_REQUEST_MODAL.sub("", text)) or request_kind in ("discussion", "uncertain"):
        return None
    if request_kind not in ("query", "status"):
        return None
    references_prior = bool(_FOLLOW.search(text))
    follow = bool(recent and references_prior)
    usage = bool(_USAGE.search(text))
    amount_question = bool(re.search(r"多少|用了|花了|消耗了|耗了", text))
    # Bare ability questions are left for the model to classify; a concrete
    # polite request can still take the direct route.
    if (not via_model and _REQUEST_MODAL.search(text)
            and not re.search(r"一下|今天|今日|昨天|昨日|最近|过去|近\s*(?:\d|[一二两三四五六七八九十])|多少|明细", text)):
        return None
    short_time_followup = bool(follow and re.fullmatch(
        r"(?:那|那么)?\s*(?:今天|今日|昨天|昨日)(?:的)?\s*(?:呢|吗|么)[？?！!。\s]*", text))
    short_source_followup = bool(follow and re.fullmatch(
        r"(?:那|那么)?\s*(?:cockpit|codex|项目用量|项目账本|肥鱼|肥鱼本机|feiyu|qqbot|官网)(?:的)?\s*(?:呢|吗|么)[？?！!。\s]*", text, re.I))
    detail_aliases = "|".join("(?:" + pattern + ")" for pattern, _ in _FEATURES)
    short_detail_followup = bool(follow and re.fullmatch(
        r"(?:那|那么)?\s*(?:" + detail_aliases + r"|论文(?:项目|用途)?|肥鱼(?:相关|项目|用途))"
        r"(?:的)?\s*(?:(?:tokens?|用量|消耗|账单)(?:的)?\s*)?(?:呢|吗|么)[？?！!。\s]*", text, re.I))
    anchored_followup = bool(follow and re.search(r"查|看|瞅|瞧|统计|核实|算|列|展示", text)
                             and re.search(r"两边|两者|两(?:个|种)(?:来源|账本)|双方|cockpit|codex|项目用量|项目账本|官网|"
                                           r"feiyu|qqbot|肥鱼(?:本机|功能|相关|项目|用途)|按(?:照)?功能|功能明细|"
                                           r"按(?:照)?用途|用途明细|按(?:照)?项目|项目明细", text, re.I))
    # A bare question about knowing what tokens are is conversation, not an
    # instruction to query an accounting ledger. Verified short follow-ups
    # remain eligible even without restating the accounting context.
    if (not re.search(r"查|看|瞅|瞧|统计|算|核实|确认|总结|生成|做一份|给我|发我|发给我|列|展示|告诉|说说", text)
            and not amount_question and not _QUESTIONED_FACT.search(text)
            and not re.search(r"用量|项目账本|消耗|开销|账单|费用|花费|耗费|成本|用费|用掉", text)
            and not (short_time_followup or short_source_followup or short_detail_followup or anchored_followup)):
        return None
    if not _ACTION.search(text) and not (short_time_followup or short_source_followup or short_detail_followup) and not _QUESTIONED_FACT.search(text):
        return None
    if not usage and not (short_time_followup or short_source_followup or short_detail_followup or anchored_followup
                          or (re.search(r"两边|两者", text) and amount_question)):
        return None
    if _NEGATED_SOURCE.search(text):
        return _fail("ambiguous_source")
    source_info = _source(text)
    if source_info.get("ok") is False:
        return source_info
    source = source_info["source"] or (recent["source"] if follow else "both")
    if isinstance(unsupported_filter, str) and unsupported_filter.strip():
        return _fail("unsupported_feature_filter", source)
    if not isinstance(unsupported_filter, str) or _FILTER.search(text) or _UNSUPPORTED_FEATURE_DETAIL.search(text):
        return _fail("unsupported_feature_filter", source)
    # The recognisable feature names and the two validated project buckets are
    # the only explicit selectors supported by the local readers.
    feature, purposes = _features(text), _purposes(text)
    names_feature = any(re.search(pattern, text, re.I) for pattern, _ in _FEATURES)
    names_purpose = bool(re.search(r"论文(?:项目|用途)?|肥鱼(?:相关|项目|用途)", text))
    if not source_info["explicit"]:
        if names_feature and purposes:
            return _fail("source_feature_unavailable")
        if names_feature:
            source = "feiyu"
        elif purposes:
            source = "cockpit"
    if names_feature and source != "feiyu" or purposes and source != "cockpit":
        return _fail("source_feature_unavailable", source)
    if ((_ALL_FEATURES.search(text) and names_feature and not re.search(r"等(?:各(?:个)?|全部|所有)功能", text))
            or (_ALL_PURPOSES.search(text) and names_purpose)
            or (re.search(r"只|仅", text) and (_ALL_FEATURES.search(text) or _ALL_PURPOSES.search(text)))):
        return _fail("unsupported_feature_filter", source)
    status = bool(re.search(r"(?:用量|账单|token).{0,20}(?:状态|是否正常|正常运行|健康|可用)|"
                            r"(?:状态|是否正常|健康|可用).{0,20}(?:用量|账单|token)", text, re.I))
    if status:
        if feature or purposes or _period(text) is not None:
            return _fail("ambiguous_period", source)
        if not via_model and not _direct_confident(text):
            return None
        return {"ok": True, "period": "status", "source": source, "feature": "", "purposes": []}
    if request_kind == "status":
        return _fail("status_scope_unverified", source)
    period_text = (re.sub(r"刚才(?:的)?(?:那段|时段)|上次(?:的)?(?:那段|时段)|同样(?:的)?时段", " ", text)
                   if follow else text)
    period = _period(period_text)
    if period and period.get("ok") is not True:
        return {**period, "source": source}
    if references_prior and not follow and (not source_info["explicit"] or period is None):
        return _fail("context_unavailable", source)
    if not via_model and not _direct_confident(text):
        return None
    if period is None:
        if follow:
            period = recent.get("period")
            if not isinstance(period, dict):
                return _fail("ambiguous_period", source)
        else:
            period = {"ok": True, "period": "today"}
    if follow and not feature and not purposes and not (_ALL_FEATURES.search(text) or _ALL_PURPOSES.search(text)):
        feature = recent.get("feature", "")
        purposes = list(recent.get("purposes", []))
    if feature and source != "feiyu" or purposes and source != "cockpit":
        return _fail("source_feature_unavailable", source)
    if source == "cockpit" and re.search(r"按(?:照)?功能|各(?:个)?功能|功能(?:明细|分项|分类)", text):
        return _fail("source_feature_unavailable", source)
    if source == "official" and period.get("period") in ("window", "range"):
        return _fail("unsupported_period", source)
    if re.search(r"只|仅", text):
        only_text = re.sub(r"只(?:查|看|要|列)?(?:看|查)?\s*(?:cockpit|codex|项目用量|项目账本|肥鱼|feiyu|qqbot|官网)|"
                           r"仅(?:查|看|要|列)?(?:看|查)?\s*(?:cockpit|codex|项目用量|项目账本|肥鱼|feiyu|qqbot|官网)|只(?:想|是|不过)", "", text, flags=re.I)
        if re.search(r"只|仅", only_text) and not (feature or purposes):
            return _fail("unsupported_feature_filter", source)
    return {**period, "source": source, "feature": feature, "purposes": purposes}
