# Protocol provenance

`proto.py` is adapted from `st_proto.py` in
[mizuku003/maibot-tieba-share](https://github.com/mizuku003/maibot-tieba-share),
copyright (c) 2026 mizuku003, MIT. The request packing and field table in
`protocol.py`, and the bounded recognition algorithm in `../links.py`, are
adapted from the same project's `st_client.py` and `st_link.py`. The complete
MIT notice is retained in `LICENSE.mizuku003`.

Modifications include removing the upstream framework dependency, bounding
network access, reporting fetch failures explicitly, preserving ordered mixed
content, and leaving absent user levels unknown.

Additional field-number references:

- https://github.com/lumina37/aiotieba/blob/master/src/aiotieba/api/_protobuf/User.proto
- https://github.com/lumina37/aiotieba/blob/master/src/aiotieba/api/_protobuf/ThreadInfo.proto
- https://github.com/lumina37/aiotieba/blob/master/src/aiotieba/api/_protobuf/PostInfoList.proto
- https://github.com/lumina37/aiotieba/blob/master/src/aiotieba/api/profile/protobuf/ProfileReqIdl.proto
- https://github.com/lumina37/aiotieba/blob/master/src/aiotieba/api/profile/protobuf/ProfileResIdl.proto

The endpoints are unofficial public read endpoints and can change.
