"""
Loguru 日志配置

同步自 foxline-pro-backend-server/utils/logger.py（简化版：无 request_id / 集群角色）。
Gateway 是无状态执行服务，不需要 ClusterLeaderLease 和 request_context。
未来引入 request_id 追踪时，添加 ContextVar + patcher 即可。
"""
import os as sync_os
import sys
from pathlib import Path

from aiofiles import os
from loguru import logger

_PID = sync_os.getpid()

LOG_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<magenta>{extra[pid]}</magenta> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)
"""Log format: timestamp | level | PID | module:function:line - message"""


async def setup_logging(
        log_level: str = "INFO",
        log_file_path: Path | None = None,
) -> None:
    """配置 loguru 日志输出格式、级别和 sink"""
    if log_file_path is None:
        log_file_path = Path('./temp/logs/gateway.log')
    await os.makedirs(sync_os.path.dirname(log_file_path), exist_ok=True)

    logger.remove()
    _ = logger.configure(
        extra={'pid': _PID},
    )

    _ = logger.add(sys.stdout, level=log_level, format=LOG_FORMAT, diagnose=False)
    _ = logger.add(
        log_file_path,
        level=log_level,
        format=LOG_FORMAT,
        rotation="10 MB",
        retention="30 days",
        compression=None,
        encoding="utf-8",
        enqueue=True,
        backtrace=True,
        diagnose=False,  # Disabled for file sink: locals may contain presigned URLs, tokens, etc.
    )
    logger.info(f"Logging initialized | PID={_PID}")
