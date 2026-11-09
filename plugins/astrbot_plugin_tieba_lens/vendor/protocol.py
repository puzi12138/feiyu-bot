# MIT-licensed adaptations, copyright 2026 mizuku003. See NOTICE.md and LICENSE.mizuku003.
"""贴吧客户端接口（protobuf）的字段表与解析。

贴吧手机版走的是 `tiebac.baidu.com` 上的两个客户端接口，收发都是 protobuf：

  * 列表：`POST /c/f/frs/page?cmd=301001`
  * 楼层：`POST /c/f/pb/page?cmd=302001`

相比抓 HTML（旧路线），这条路有三个好处，都是实测出来的，不是推测：

1. **点赞数真的拿得到** —— HTML 详情页里根本没有 `agree_num`（实测 3 个样本
   命中数都是 0），列表页的内联 JSON 里才有；而 protobuf 的 `ThreadInfo.agree`
   和 `Post.agree` 两个层级都有，跟 HTML 列表页给的数字逐一相同。
2. **能翻页** —— HTML 的 `kz=` 页面加 `pn=` 完全无效（实测 pn=1/2/99 返回同一批
   楼层），protobuf 的 `pn` 是真的页码，`total_page` 也给了。
3. **不需要登录** —— 连楼中楼（`with_floor=1`）都不用带 BDUSS。网上流传的
   「取楼中楼必须登录」是拿 aiotieba 的通用注释当结论，实测无 BDUSS 一样拿得到。

字段号全部来自对真实响应的逐字段比对，**不是从 .proto 文件抄的**：那些 .proto
在 aiotieba 仓库里，字段号对不上时以实测为准。响应里出现过的字段在下面都有注释，
没确认过的字段一律不猜 —— 读不到就是默认值，宁可少显示也不要显示错的。
"""

from __future__ import annotations

from typing import Any

from .proto import Message, ProtobufError

# --- 请求字段号 ---------------------------------------------------------------

# CommonReq（两个接口共用）

# PbPageReqIdl：楼层页请求
PAGE_CMD = 302001
PAGE_URL = "https://tiebac.baidu.com/c/f/pb/page"
REQ_PAGE_DATA = 1        # PbPageReqIdl.data
REQ_PAGE_COMMON = 25     # DataReq.common
REQ_PAGE_COMMON_TYPE = 1       # CommonReq._client_type
REQ_PAGE_COMMON_VERSION = 2    # CommonReq._client_version
REQ_PAGE_KZ = 4          # DataReq.kz（帖子 id，int64）
REQ_PAGE_LZ = 5          # DataReq.lz（只看楼主）
REQ_PAGE_SORT = 6        # DataReq.r（楼层排序）
REQ_PAGE_PID = 7         # DataReq.pid
REQ_PAGE_WITH_FLOOR = 8  # DataReq.with_floor（1 = 带楼中楼）
REQ_PAGE_FLOOR_RN = 9    # DataReq.floor_rn（每层带几条楼中楼）
REQ_PAGE_RN = 13         # DataReq.rn（一页几层）
REQ_PAGE_PN = 18         # DataReq.pn（第几页）
REQ_PAGE_FLOOR_SORT = 74 # DataReq.floor_sort_type（1 = 楼中楼按热度）

# FrsPageReqIdl：列表页请求
FRS_CMD = 301001
FRS_URL = "https://tiebac.baidu.com/c/f/frs/page"
REQ_FRS_DATA = 1         # FrsPageReqIdl.data
REQ_FRS_KW = 1           # DataReq.kw（吧名，不带「吧」字）
REQ_FRS_RN = 2           # DataReq.rn
REQ_FRS_RN_NEED = 3      # DataReq.rn_need
REQ_FRS_IS_GOOD = 4      # DataReq.is_good（1 = 只要精品帖）
REQ_FRS_PN = 15          # DataReq.pn（第几页）
REQ_FRS_COMMON = 39      # DataReq.common
REQ_FRS_SORT = 47        # DataReq.sort_type（0=回复时间 1=发布时间；**2 会返回 0 条**）

# --- 响应字段号 ---------------------------------------------------------------

# 最外层：error / data
RES_ERROR = 1            # Error（errno 在 1）
RES_DATA = 2
ERROR_ERRNO = 1          # Error.errno，0 才算成功
ERROR_MSG = 2            # Error.errmsg

# FrsPageResIdl.DataRes
FRS_FORUM = 2            # forum
FRS_FORUM_NAME = 2       # Forum.name
FRS_FORUM_MEMBERS = 9    # Forum.member_num
FRS_PAGE = 4             # page（Page 消息）
FRS_THREAD_LIST = 7      # thread_list（重复）
FRS_USER_LIST = 17       # user_list（重复）

# ThreadInfo
TH_ID = 1
TH_TITLE = 3
TH_REPLY_NUM = 4
TH_VIEW_NUM = 5
TH_IS_TOP = 9
TH_IS_GOOD = 10
TH_THREAD_TYPE = 26      # 40 = 视频帖
TH_AUTHOR = 18           # 作者（内嵌 User，实测**可能为空**，要用 author_id 去 user_list 里找）
TH_CREATE_TIME = 45
TH_AUTHOR_ID = 56
TH_VIDEO_INFO = 79
TH_AGREE = 126           # Agree 消息
TH_SHARE_NUM = 135

# PbPageResIdl.DataRes
PAGE_FORUM = 2
PAGE_PAGE = 3            # page（Page 消息）
PAGE_POST_LIST = 6       # post_list（重复）
PAGE_THREAD = 8          # thread（这个帖子的 ThreadInfo）
PAGE_USER_LIST = 13      # user_list（重复）

# Post（一层楼）
POST_ID = 1              # 楼层 id（不是楼层号！）
POST_FLOOR = 3           # 楼层号 —— **实测几乎全是 0，不能用作楼层号**
POST_TIME = 4
POST_TITLE = 2           # 标题 / 摘要（首楼是标题，其余楼层是「回复：标题」）—— **不是正文**
POST_CONTENT = 5         # 重复的 PbContent —— **这才是正文**
POST_SUB_POST_NUMBER = 13 # 楼中楼总数
POST_SUB_POST_LIST = 15  # 楼中楼（重复 SubPost）
POST_SIGNATURE = 21
POST_AUTHOR_ID = 19      # 作者 user_id —— **这才是拿昵称的正路**
POST_AGREE = 37          # Agree
POST_TID = 46

# SubPost / SubPostList（楼中楼）
SUB_PID = 1              # SubPost.pid
SUB_LIST = 2             # SubPost.sub_post_list（重复 SubPostList）
SUBL_ID = 1              # SubPostList.id
SUBL_SHELL = 2           # **内嵌 protobuf**（不是 PbContent 列表），见 comment_text
SUBL_TIME = 3
SUBL_AUTHOR_ID = 4       # 作者 user_id
SUBL_TITLE = 5           # 实测恒为空
SUBL_FLOOR = 6           # 实测恒为 0
SUBL_AGREE = 9           # Agree —— **实测该字段不存在**，楼中楼没有点赞数

# 楼中楼那层内嵌壳（SUBL_SHELL 解开后）
SHELL_TYPE = 1           # 0=文本
SHELL_TEXT = 2           # 正文（**直接是字符串**，不是 PbContent）

# PbContent（正文里的一段）
PC_TYPE = 1              # 0=文本(proto3 省零值) 2=表情 3=图片，实测分布 {0:28, 2:12, 3:2}
PC_TEXT = 2
PC_LINK = 3
PC_SRC = 4               # 缩略图
PC_BSIZE = 5
PC_CDN_SRC = 8           # 720 宽那档
PC_BIG_CDN_SRC = 9       # 大图
PC_C = 11
PC_EMOJI_NAME = 11      # 表情中文名（如「阴险」）—— field2 只是 image_emoticonNN
PC_VOICE_MD5 = 12
PC_DURING_TIME = 13
PC_UID = 15
PC_WIDTH = 18
PC_HEIGHT = 19
PC_ORIGIN_SRC = 25       # 原图（最清晰，见 pick_image_url）
PC_ORIGIN_SIZE = 27
PC_COUNT = 28

# Agree
AGREE_NUM = 1
AGREE_DISAGREE = 4

# User（在顶层 user_list 里，靠 id 关联）
USER_ID = 2
USER_NAME = 3            # 用户名（登录名）
USER_NAME_SHOW = 4       # 显示名（昵称）—— 优先用它
USER_PORTRAIT = 5
USER_LEVEL = 23

# Page —— 实测 field1=每页条数(30) field3=当前页 field9=总页数 field12=总条数(90)
PAGE_RN = 1
PAGE_CURRENT = 3
PAGE_TOTAL = 9
PAGE_TOTAL_COUNT = 12

# 正文片段类型（PbContent.type）—— 实测取值只有 0/2/3
PC_TYPE_TEXT = 0         # proto3 省略零值，文本段通常连 field1 都不写
PC_TYPE_EMOJI = 2        # field2 是英文名 image_emoticonNN，field11 才是中文名
PC_TYPE_IMAGE = 3
PC_TYPE_AT = 4           # 实测样本里没出现，保留兼容
PC_TYPE_VIDEO = 5        # 实测样本里没出现，保留兼容

# 客户端标识：这两个值是从真实 App 抓包里抄下来的固定串，换掉会被服务端拒。
CLIENT_TYPE = 2
CLIENT_VERSION = "12.64.1.1"
PROTO_UA = "miku/39"
PROTO_BOUNDARY = "-*_r1999"
from .proto import (
    Message,
    ProtobufError,
    encode_bytes,
    encode_message,
    encode_uint,
)

def build_frs_request(
    forum: str, *, page: int = 1, rn: int = 30, sort: int = 0, is_good: bool = False
) -> bytes:
    """打包一个「取某吧帖子列表」的请求。

    Args:
        forum: 吧名，**不带末尾的「吧」字**（带「吧」实测返回 0 条）。
        page: 第几页，从 1 开始。
        rn: 一页几条。
        sort: 0=按回复时间、1=按发布时间。**别用 2（热门）**：实测返回 0 条，
            贴吧的热门只在有热门分区的吧生效，这里没有可用的判据。
        is_good: 只要精品帖。
    """
    data = encode_bytes(REQ_FRS_KW, forum)
    data += encode_uint(REQ_FRS_RN, rn)
    data += encode_uint(REQ_FRS_RN_NEED, rn)
    data += encode_uint(REQ_FRS_IS_GOOD, 1 if is_good else 0)
    data += encode_uint(REQ_FRS_PN, max(1, int(page)))
    data += encode_message(REQ_FRS_COMMON, _common())
    data += encode_uint(REQ_FRS_SORT, int(sort))
    return encode_message(REQ_FRS_DATA, data)


def build_page_request(
    tid: str,
    *,
    page: int = 1,
    rn: int = 30,
    with_floor: bool = False,
    floor_rn: int = 0,
    sort: int = 0,
    only_author: bool = False,
) -> bytes:
    """打包一个「取某帖楼层」的请求。

    Args:
        tid: 帖子 id。
        page: 第几页（从 1 开始）。protobuf 的 pn 是真页码，跟 HTML 那条死路不同。
        rn: 一页几层。
        with_floor: 要不要带楼中楼。**不带 BDUSS 也能拿**，别被网上「必须登录」的说法劝退。
        floor_rn: 每层带几条楼中楼。只在 with_floor 为真时有意义。
        sort: 楼层排序，0 = 按时间。
        only_author: 只看楼主。
    """
    data = encode_message(REQ_PAGE_COMMON, _common())
    data += encode_uint(REQ_PAGE_KZ, int(tid))
    data += encode_uint(REQ_PAGE_LZ, 1 if only_author else 0)
    data += encode_uint(REQ_PAGE_SORT, int(sort))
    data += encode_uint(REQ_PAGE_WITH_FLOOR, 1 if with_floor else 0)
    data += encode_uint(REQ_PAGE_FLOOR_RN, max(0, int(floor_rn)))
    data += encode_uint(REQ_PAGE_RN, rn)
    data += encode_uint(REQ_PAGE_PN, max(1, int(page)))
    # 楼中楼按热度排，服务端原生支持。实测不带这个参数也返回楼中楼，
    # 但顺序是时间序；带上才可能把高赞的顶上来。拿不到高赞数时也无害。
    data += encode_uint(REQ_PAGE_FLOOR_SORT, 1)
    return encode_message(REQ_PAGE_DATA, data)


def _common() -> bytes:
    """两个接口共用的 CommonReq 包头。版本串是写死的，改了服务端会拒。"""
    return encode_uint(REQ_PAGE_COMMON_TYPE, CLIENT_TYPE) + encode_bytes(
        REQ_PAGE_COMMON_VERSION, CLIENT_VERSION
    )


def multipart_body(payload: bytes) -> tuple[bytes, str]:
    """把请求体包成 multipart/form-data，返回 (字节, Content-Type)。

    客户端就是 multipart 发这个 protobuf 的：字段名叫 ``data``、文件名叫 ``file``、
    boundary 是固定的 ``-*_r1999``。三样都对不上服务端就当没收到，实测过。
    """
    boundary = PROTO_BOUNDARY
    head = (
        "--%s\r\n"
        'Content-Disposition: form-data; name="data"; filename="file"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ) % boundary
    tail = "\r\n--%s--\r\n" % boundary
    body = head.encode("utf-8") + payload + tail.encode("utf-8")
    return body, "multipart/form-data; boundary=%s" % boundary


def proto_headers() -> dict[str, str]:
    """客户端接口要的请求头。少一个都会被当成普通网页请求。"""
    return {
        "User-Agent": PROTO_UA,
        "x_bd_data_type": "protobuf",
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }


PROFILE_CMD = 303012
PROFILE_URL = "https://tiebac.baidu.com/c/u/user/profile"


def build_profile_request(identifier: str) -> bytes:
    """Public profile by legacy numeric UID or portrait, without account data."""
    data = encode_uint(2, 1) + encode_uint(6, 1)
    data += encode_message(9, _common())
    if identifier.isdigit():
        data += encode_uint(1, int(identifier))
    else:
        data += encode_bytes(16, identifier)
    return encode_message(1, data)
