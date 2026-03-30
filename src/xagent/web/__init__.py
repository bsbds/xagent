"""xagent Web 模块

这个模块提供了xagent的Web界面，包括：
- REST API接口
- WebSocket实时通信
- 前端用户界面
- 监控和管理功能

使用方式:
    # 命令行启动
    python -m xagent.web

    # 程序中启动
    from xagent.web import run_server
    run_server(host="0.0.0.0", port=8000)
"""

from importlib.metadata import version
from typing import Any

try:
    __version__ = version("xagent")
except Exception:
    __version__ = "0.0.0+unknown"


def create_app(*args: Any, **kwargs: Any):
    from .app import create_app as _create_app

    return _create_app(*args, **kwargs)


def __getattr__(name: str) -> Any:
    if name == "app":
        return create_app()
    if name == "create_app":
        return create_app
    raise AttributeError(name)


def run_server(
    host: str = "127.0.0.1",
    port: int = 8000,
    reload: bool = False,
    uploads_dir: str | None = None,
    **kwargs: Any,
) -> None:
    """快速启动Web服务器

    Args:
        host: 服务器主机地址
        port: 服务器端口
        reload: 是否启用自动重载
        uploads_dir: 上传目录；默认使用xagent配置默认值
        **kwargs: 其他uvicorn参数
    """
    import uvicorn

    if uploads_dir is not None:
        import os

        os.environ["XAGENT_UPLOADS_DIR"] = uploads_dir

    if reload:
        uvicorn.run(
            "xagent.web.app:create_app",
            host=host,
            port=port,
            reload=reload,
            factory=True,
            **kwargs,
        )
    else:
        uvicorn.run(
            create_app(uploads_dir=uploads_dir),
            host=host,
            port=port,
            reload=reload,
            **kwargs,
        )


__all__ = ["app", "create_app", "run_server", "__version__"]
