"""短事务领取任务，在有硬超时的子进程里执行审核。"""

import json
import logging
import multiprocessing
import os
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from multiprocessing.connection import Connection
from queue import Empty, Queue
from threading import Event, Thread

from sqlalchemy.orm import Session

from app.review.runner import DeliveryReviewInput, PipelineNotReadyError, run_delivery_review
from app.services.review_jobs import claim_next, complete_job, fail_job

logger = logging.getLogger(__name__)

_MAX_RESULT_BYTES = 64 * 1024
_POLL_SECONDS = 0.05
ReviewHandler = Callable[[DeliveryReviewInput], dict]


@dataclass(frozen=True)
class _ExecutionResult:
    result: dict | None = None
    error_code: str | None = None
    stopped: bool = False


def _error_message(code: str) -> bytes:
    return json.dumps({"kind": "error", "error_code": code}).encode("utf-8")


def _execute_child(sender: Connection, handler: ReviewHandler, review: DeliveryReviewInput) -> None:
    # 审核器及第三方库的正文、异常和密钥不能通过 stdout/stderr 进入服务日志。
    try:
        with open(os.devnull, "w") as sink:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
        result = handler(review)
        if not isinstance(result, dict):
            raise TypeError
        message = json.dumps(
            {"kind": "result", "result": result}, ensure_ascii=True, allow_nan=False
        ).encode("utf-8")
        if len(message) > _MAX_RESULT_BYTES:
            message = _error_message("execution_error")
    except PipelineNotReadyError:
        message = _error_message("pipeline_not_ready")
    except BaseException:
        message = _error_message("execution_error")
    try:
        sender.send_bytes(message)
    except (BrokenPipeError, EOFError, OSError):
        pass
    finally:
        sender.close()


def _receive_result(
    receiver: Connection, results: Queue[_ExecutionResult], finished: Event
) -> None:
    outcome = _ExecutionResult(error_code="execution_error")
    try:
        while not finished.is_set():
            if receiver.poll(_POLL_SECONDS):
                # recv_bytes 在残缺帧上可能等待；独立接收线程不会阻塞硬超时和停止信号。
                message = json.loads(receiver.recv_bytes(_MAX_RESULT_BYTES))
                if isinstance(message, dict):
                    if message.get("kind") == "result" and isinstance(message.get("result"), dict):
                        outcome = _ExecutionResult(result=message["result"])
                    elif message.get("kind") == "error" and message.get("error_code") in {
                        "pipeline_not_ready",
                        "execution_error",
                    }:
                        outcome = _ExecutionResult(error_code=message["error_code"])
                break
        else:
            return
    except (EOFError, OSError, ValueError, TypeError, RecursionError):
        pass
    if not finished.is_set():
        results.put_nowait(outcome)


def _run_handler(
    handler: ReviewHandler,
    review: DeliveryReviewInput,
    timeout_seconds: float,
    stopped: Event,
) -> _ExecutionResult:
    if stopped.is_set():
        return _ExecutionResult(stopped=True)
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_execute_child, args=(sender, handler, review))
    results: Queue[_ExecutionResult] = Queue(maxsize=1)
    finished = Event()
    reader = Thread(target=_receive_result, args=(receiver, results, finished), daemon=True)
    started = False
    reader_started = False
    deadline = time.monotonic() + timeout_seconds
    try:
        process.start()
        started = True
        # 父进程必须关闭写端；子进程崩溃或被杀后，接收端才能可靠得到 EOF。
        sender.close()
        reader.start()
        reader_started = True
        while True:
            if stopped.is_set():
                return _ExecutionResult(stopped=True)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _ExecutionResult(error_code="timeout")
            try:
                outcome = results.get(timeout=min(_POLL_SECONDS, remaining))
                if stopped.is_set():
                    return _ExecutionResult(stopped=True)
                if time.monotonic() >= deadline:
                    return _ExecutionResult(error_code="timeout")
                return outcome
            except Empty:
                pass
    except Exception:
        return _ExecutionResult(error_code="execution_error")
    finally:
        finished.set()
        sender.close()
        if started:
            if process.is_alive():
                process.terminate()
            process.join(timeout=1)
            if process.is_alive():
                process.kill()
                process.join(timeout=1)
        if reader_started:
            reader.join(timeout=1)
        receiver.close()
        if not started or not process.is_alive():
            process.close()


def run_once(
    sessions: Callable[[], Session],
    *,
    handler: ReviewHandler = run_delivery_review,
    timeout_seconds: float = 300,
    lease_grace_seconds: float = 5,
    max_attempts: int = 3,
    retry_delay_seconds: float = 5,
    stopped: Event | None = None,
) -> bool:
    stopped = stopped if stopped is not None else Event()
    if stopped.is_set():
        return False
    with sessions() as db:
        claim = claim_next(
            db,
            timeout_seconds=timeout_seconds,
            lease_grace_seconds=lease_grace_seconds,
            max_attempts=max_attempts,
            retry_delay_seconds=retry_delay_seconds,
        )
        # 即使没有可领取项，过期租约的恢复也需要提交。
        db.commit()
    if claim is None:
        return False
    review = DeliveryReviewInput(
        job_id=claim.job_id,
        delivery_id=claim.delivery_id,
        project_id=claim.project_id,
        attempt=claim.attempt,
        workflow_revision=claim.workflow_revision,
    )
    outcome = _run_handler(handler, review, timeout_seconds, stopped)
    if outcome.stopped or stopped.is_set():
        # 停止只结束子进程，保留租约；重启后由持久队列恢复，不能伪造审核失败。
        return True
    with sessions() as db:
        if outcome.result is not None:
            complete_job(db, claim, outcome.result)
        else:
            code = outcome.error_code or "execution_error"
            fail_job(
                db,
                claim,
                code,
                retryable=code != "pipeline_not_ready",
                retry_delay_seconds=retry_delay_seconds,
            )
        db.commit()
    return True


def main() -> None:
    # 导入 worker 和生成 spawn 子进程不会初始化数据库或读取本机配置。
    from app.config import get_settings
    from app.db import SessionLocal, engine

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stopped = Event()

    def stop(signum: int, frame: object) -> None:
        stopped.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    settings = get_settings()
    logger.info("后台审核 worker 已启动")
    try:
        while not stopped.is_set():
            try:
                processed = run_once(
                    SessionLocal,
                    timeout_seconds=settings.review_job_timeout_seconds,
                    lease_grace_seconds=settings.review_job_lease_grace_seconds,
                    max_attempts=settings.review_job_max_attempts,
                    retry_delay_seconds=settings.review_job_retry_delay_seconds,
                    stopped=stopped,
                )
            except Exception:
                logger.warning("后台任务暂时无法处理，将重试；详细异常不写入日志")
                processed = False
            if not processed:
                stopped.wait(settings.worker_poll_seconds)
    finally:
        engine.dispose()
        logger.info("Worker 已停止")


if __name__ == "__main__":
    main()
