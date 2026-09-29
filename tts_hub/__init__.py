"""TTS API 管家（tts_hub）

模块组成：
    registry.py  配置注册表：加载 / 占位符展开 / 运行期覆盖持久化
    manager.py   引擎进程生命周期：拉起 / 卸载 / 健康检查 / 切换 / 日志 / 空闲回收
    proxy.py     透传层：表单 / JSON / 流式 / SSE 全透传
    server.py    FastAPI 服务：查询接口、配置读写、生命周期、合成透传、可视化面板
"""

__version__ = "1.0.0"
