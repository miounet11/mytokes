#!/usr/bin/env python3
"""启动脚本

用于启动 AI History Manager 服务。
"""

import os
import sys

# 确保项目根目录在 Python 路径中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    """主入口"""
    import uvicorn
    from app.config import get_settings

    settings = get_settings()

    print(f"Starting AI History Manager v2.0.0")
    print(f"Environment: {settings.environment}")
    print(f"Listening on: {settings.host}:{settings.port}")

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.environment == "development",
        workers=1 if settings.environment == "development" else settings.workers,
        log_level=settings.log_level.lower(),
        access_log=settings.environment != "production",
    )


if __name__ == "__main__":
    main()
