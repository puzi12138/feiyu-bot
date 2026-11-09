# 来源与分发范围

读取路线与问题驱动的手账方式源自使用者已有的本地 WorkBuddy / bilibili-knowledge-pipeline 工作。公开版重新实现为 JavaScript DSH 插件；没有把私人工作区、人格指令、账号档案、历史日记、登录状态、模型权重或角色素材复制进仓库。

WBI 参数签名的排列和字段是接口协议事实。实现参考原工作中的协议观察；公开协议研究入口：[bilibili-API-collect](https://github.com/SocialSisterYi/bilibili-API-collect)。B站相关接口不是本项目与平台达成的稳定公共服务协议，字段和可用性可能变化。

DSH 插件契约依据 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 官方文档和已安装宿主 0.2.1-rc.2。依赖的许可保留在各自 npm 包内。

1.2.0 历史、收藏、点赞与投币的字段及路径参照社区原始接口调查，链接见 [写入配置](docs/WRITE_CONFIG.md)；执行器与授权控制由本项目实现。社区调查不是平台稳定性承诺，合成测试不能代替真实账号写入验收。

仓库头图 `docs/assets/header.png` 由项目使用者提供，并明确要求作为项目头图公开。代码的 MIT 许可不自动适用于这张图或其中角色；不据此声明其作者或第三方角色授权。其他私人角色素材、案例、第三方视频、字幕与声音仍不随仓库和 release 分发。
