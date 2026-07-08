import asyncio
import logging

from .jobs import JobManager


logger = logging.getLogger(__name__)


async def cleanup_loop(manager: JobManager, interval_seconds: int = 300) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await asyncio.to_thread(manager.cleanup_expired)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Temporary download cleanup failed")
