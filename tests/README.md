# 小黑盒离线回归检查

检查使用合成数据、临时数据库和模拟的平台请求，验证历史未知评论的回执匹配、并发写入以及识图请求和缓存保护。检查不会读取原环境账本，也不会发送评论。

使用 Python 3.12，在独立环境中安装 `plugins/astrbot_plugin_xiaoheihe_adapter/requirements.txt` 后，从仓库根目录运行：

```text
python tests/run_heybox_offline.py
```

运行识图检查还需要已安装 AstrBot 及其依赖，并使用与仓库 `upstream/DEPENDENCIES.lock.json` 对应的 AstrBot 版本和覆盖文件。使用独立的 AstrBot 检出目录时运行：

```text
python tests/run_heybox_offline.py --vision --astrbot-root <AstrBot目录>
```

如果 AstrBot 已安装到当前 Python 环境，可省略 `--astrbot-root`。运行器为子进程建立临时工作目录并移除继承的 `PYTHONPATH`。测试通过只说明所覆盖的离线情形通过，还需要在新环境核验平台连接和正常业务。
