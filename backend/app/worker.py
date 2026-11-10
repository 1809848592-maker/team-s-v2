"""独立 worker 进程骨架；数据库任务表及执行逻辑在 M3-02 实现。"""

import logging
import signal
from threading import Event

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.db import SessionLocal, engine

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stopped = Event()

    def stop(signum: int, frame: object) -> None:
        stopped.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    logger.info("Worker 骨架已启动；任务调度尚未实现")
    try:
        while not stopped.is_set():
            try:
                with SessionLocal() as session:
                    session.execute(text("SELECT 1"))
            except SQLAlchemyError:
                logger.warning("Worker 暂时无法连接数据库，将再次检查")
            stopped.wait(get_settings().worker_poll_seconds)
    finally:
        engine.dispose()
        logger.info("Worker 已停止")


if __name__ == "__main__":
    main()
