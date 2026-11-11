# 肥鱼 · Feiyu Bot

基于 AstrBot 的个人助手扩展源码归档，包含插件、运行脚本、本地服务封装与 QQ 原生桥接代码。功能涉及会话与记忆、公开内容阅读、媒体处理，以及带权限、时窗和发送回执保护的自动业务。

本仓库是经过隐私过滤的公开源码归档，用于审阅与后续迁移。它不是完整运行备份，也不是开箱即用安装包；新的环境需要重新取得上游、安装依赖、填写本地配置并逐项授权。本次归档没有修改现有运行服务，也没有验证从本仓库完成部署。

## 目录

```text
plugins/                      10 个插件的受审源码及现有许可文件
scripts/                      运行、健康检查与服务封装脚本
integrations/native-bridge/   原生桥接模块与启动辅助源码
upstream/
  DEPENDENCIES.lock.json       来源、版本、基线与已知缺口
  overlays/                   有已知基线的上游替换文件
  snapshots/                  必要的非 Git 依赖源码快照
  local/                      本地账单、音乐等服务封装
examples/                     重新构造的安全配置模板
docs/                         架构、重建与隐私说明
THIRD_PARTY_NOTICES.md         组件来源、许可证及待确认项
```

源码归档不包含 SnowLuma、QQ 客户端、打包的原生运行时、模型权重、语音素材或运行数据。按需从各自合法渠道单独取得依赖。

## 插件范围

| 组件 | 源码职责 | 本仓库范围 |
| --- | --- | --- |
| `astrbot_plugin_ai_cover` | 翻唱服务接入 | 仅记录外部来源，不含源码或本地修改 |
| `astrbot_plugin_bilibili_ai_bot` | B站解析、阅读与平台接入 | 包含源码 |
| `astrbot_plugin_image_companion` | 图片生成与图片工作流 | 仅记录外部来源，不含源码或本地修改 |
| `astrbot_plugin_private_companion` | 私聊陪伴、备忘与日程 | 仅记录外部来源，不含源码或本地修改 |
| `astrbot_plugin_proactive_chat` | 通用主动聊天 | 包含源码 |
| `astrbot_plugin_qzone` | QQ 空间接入 | 包含源码 |
| `astrbot_plugin_remember_you` | 记忆检索与管理 | 仅记录外部来源，不含源码或本地修改 |
| `astrbot_plugin_self_learning` | 消息学习与管理界面 | 包含源码 |
| `astrbot_plugin_tieba_lens` | 贴吧内容解析 | 包含源码 |
| `astrbot_plugin_virtual_life` | 虚拟生活 | 包含源码 |
| `astrbot_plugin_xiaoheihe_adapter` | 小黑盒平台适配 | 包含源码 |
| `feiyu_agent_soul` | 人格与记忆服务桥接 | 包含源码 |
| `feiyu_steward` | 权限、工具、原生投递与自动业务协调 | 包含源码 |
| `meme_manager` | 表情管理 | 包含源码 |

这里描述组件职责，不表示任意功能已启用、获得平台授权或在新环境验证成功。四个仅记录外部来源的插件尚未确认再分发许可，因此不复制其源码和本地修改；相关完整功能无法从本仓库恢复。外部来源见 [组件声明](THIRD_PARTY_NOTICES.md)，获取和使用前应核对其条款。

## 阅读入口

- [架构与依赖边界](docs/ARCHITECTURE.md)
- [B站视频画面分析与证据边界](docs/BILI_VIDEO_PIPELINE.md)
- [小黑盒与 B站故障恢复边界](docs/FAILURE_RECOVERY.md)
- [重新安装与配置](docs/SETUP.md)
- [隐私排除与提交检查](docs/PRIVACY.md)
- [组件许可和来源](THIRD_PARTY_NOTICES.md)

模板中的身份、路径与凭据均须由使用者重新配置。归档不携带原有授权；发送消息、公开评论、发帖、定时任务、资料修改和账号恢复等行为需要使用者分别决定并授权。

## 已知限制

- 仅部分依赖有可核对的 Git 基线；非 Git 源码快照不代表已知、可复现的上游提交。
- Python 等依赖未形成覆盖所有环境的完整版本锁。第三方接口、模型、平台客户端与二进制兼容性仍需核实。
- 脱敏后的身份占位、路径、来源校验和完整性哈希需要在新环境重新配置和审阅。原生桥模块不能独立重建缺失的上游运行包。
- 媒体、字体、参考素材和构建产物的排除可能使可选渲染或界面功能需要额外安装或构建。
- 自学习插件未分发旧的 `web_res/static/dashboard/` 编译界面，保留 `web_src/` 的 TSX 源码和依赖锁供重建；RVC 未分发许可待核的旧 `tools/uvr5/` 子树，翻唱封装使用 `pymss` 路径。
- 发送保护只能覆盖接入相应检查的路径，不能据此保证任意插件或直接接口都受同样约束。结果未知时应核对记录，避免自动重发。

`SOURCE_MANIFEST.sha256` 对清单所列的 Git blob 内容计算 SHA-256，清单自身不在其中。文本按 `.gitattributes` 的规则归一化，`.patch`、`.diff` 等标为 `-text` 的文件保持原样。Windows 检出时部分文件会转为 CRLF，直接对工作区文件计算哈希不保证得到相同结果。

## 许可证

各组件保留自己的许可证、版权与第三方声明；本仓库不对全部源码统一授予 MIT 或其他许可。四个未确认再分发许可的第三方插件仅列外部来源，本地代码及其他组件的权利边界见 [组件声明](THIRD_PARTY_NOTICES.md)。公开归档不扩大任何组件的复制、分发或商业使用权利。
