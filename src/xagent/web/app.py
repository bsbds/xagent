import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config as web_config
from .dynamic_memory_store import get_memory_store
from .models.database import init_db

# 配置日志
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# 导出全局 memory store 供其他模块使用
__all__ = ["create_app"]


def create_app(uploads_dir: str | Path | None = None) -> FastAPI:
    """Create the xagent web FastAPI app."""
    uploads_root = web_config.configure_uploads_dir(uploads_dir)

    from .api.admin_users import router as admin_users_router
    from .api.agents import router as agents_router
    from .api.auth import auth_router
    from .api.chat import chat_router
    from .api.files import file_router
    from .api.kb import kb_router
    from .api.mcp import mcp_router
    from .api.memory import MemoryManagementRouter
    from .api.model import model_router
    from .api.monitor import monitor_router
    from .api.skills import router as skills_router
    from .api.templates import router as templates_router
    from .api.text2sql import text2sql_router
    from .api.tools import tools_router
    from .api.websocket import ws_router

    app = FastAPI(
        title="xagent", description="The Agent Operating System", redirect_slashes=False
    )

    @app.get("/health")
    async def health_check() -> dict[str, str]:
        """Health check endpoint for container probes."""
        return {"status": "ok"}

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Handle request validation errors, especially those containing binary data"""
        import traceback

        logger.error(f"Validation error in {request.url}: {str(exc)}")
        logger.error(f"Traceback: {traceback.format_exc()}")

        sanitized_errors = []
        for error in exc.errors():
            sanitized_error = error.copy()
            if "input" in sanitized_error:
                try:
                    import json

                    json.dumps(sanitized_error["input"])
                except (TypeError, UnicodeDecodeError):
                    sanitized_error["input"] = "<binary or non-serializable data>"
            sanitized_errors.append(sanitized_error)

        return JSONResponse(
            status_code=422,
            content={"detail": sanitized_errors},
        )

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception) -> None:
        """全局异常处理器，确保所有错误都被记录"""
        import traceback

        logger.error(f"Unhandled exception in {request.url}: {str(exc)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise exc

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.mount(
        "/uploads",
        StaticFiles(directory=str(uploads_root)),
        name="uploads",
    )

    memory_router = MemoryManagementRouter(get_memory_store).get_router()

    app.include_router(auth_router)
    app.include_router(chat_router)
    app.include_router(file_router)
    app.include_router(kb_router)
    app.include_router(model_router)
    app.include_router(ws_router)
    app.include_router(monitor_router)
    app.include_router(memory_router)
    app.include_router(mcp_router)
    app.include_router(text2sql_router)
    app.include_router(tools_router)
    app.include_router(admin_users_router)
    app.include_router(skills_router)
    app.include_router(templates_router)
    app.include_router(agents_router)

    @app.on_event("startup")
    async def startup_event() -> None:
        logger.info("Initializing database...")
        init_db()
        logger.info("Database initialized successfully")

        from ..skills.utils import create_skill_manager

        skill_manager = create_skill_manager()
        await skill_manager.initialize()
        app.state.skill_manager = skill_manager
        logger.info(
            f"Skill manager initialized with {len(await skill_manager.list_skills())} skills"
        )

        from ..templates.utils import create_template_manager

        template_manager = create_template_manager()
        await template_manager.initialize()
        app.state.template_manager = template_manager
        logger.info(
            f"Template manager initialized with {len(await template_manager.list_templates())} templates"
        )

        from .dynamic_memory_store import get_memory_store_manager

        manager = get_memory_store_manager()
        store_info = manager.get_store_info()

        if store_info["is_lancedb"]:
            logger.info("Using LanceDB memory store with vector search capabilities")
            logger.info(f"Embedding model ID: {store_info['embedding_model_id']}")
        else:
            logger.info("Using in-memory store (no vector search capabilities)")

        logger.info(
            f"Memory store similarity threshold: {store_info['similarity_threshold']}"
        )

    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=8000)
