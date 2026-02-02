"""AI History Manager

Anthropic Messages API 兼容代理服务。

功能特性：
- 智能模型路由
- 响应续传
- 协议转换
- 速率限制
- 请求追踪
"""

__version__ = "2.0.0"
__author__ = "AI History Manager Team"

# 延迟导入以避免循环依赖
def get_app():
    """获取应用实例"""
    from .main import app
    return app

def get_create_app():
    """获取应用工厂函数"""
    from .main import create_app
    return create_app

__all__ = ["get_app", "get_create_app", "__version__"]
