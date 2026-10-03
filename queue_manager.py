import asyncio
import time
import uuid
from enum import Enum
from typing import Callable, Optional, Any
from dataclasses import dataclass, field
from astrbot.api import logger


class TaskStatus(Enum):
    """任务状态枚举"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


@dataclass
class QueueTask:
    """队列任务数据类"""
    task_id: str
    user_id: str
    priority: int
    created_at: float
    coroutine_func: Callable
    args: tuple = ()
    kwargs: dict = field(default_factory=dict)
    status: TaskStatus = TaskStatus.PENDING
    result: Any = None
    error: Optional[str] = None
    retry_count: int = 0
    max_retries: int = 2
    timeout_seconds: int = 600
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    # 任务声明的进度上报参数名；声明后才注入回调（保持旧协程兼容）
    progress_arg: Optional[str] = None
    progress: int = 0
    worker: Optional[asyncio.Task] = None
    # 执行代次：每次重试自增。旧执行者的收尾凭它识别自己已过时，
    # 避免把新一代执行误判为"任务异常退出"或误删其运行登记。
    generation: int = 0
    # 取消意图标记：协程的清理代码若在取消后抛出普通异常，据此按取消而非失败结算
    cancel_requested: bool = False
    # 该任务当前运行登记对应的执行代次：旧执行者收尾时凭它判断登记是否还是自己的
    registered_generation: int = -1


class VideoExportQueue:
    """视频导出队列管理器（高并发优化版）"""

    def __init__(self, max_concurrent: int = 2, max_queue_size: int = 30,
                 default_timeout: int = 600, default_max_retries: int = 1,
                 cleanup_interval: int = 180, timeout_grace_seconds: int = 180,
                 progress_check_seconds: int = 30):
        self._queue: list[QueueTask] = []
        self._running_tasks: dict[str, QueueTask] = {}
        self._completed_tasks: dict[str, QueueTask] = {}
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._max_concurrent = max_concurrent
        self._max_queue_size = max_queue_size
        self._default_timeout = default_timeout
        self._default_max_retries = default_max_retries
        self._task_events: dict[str, asyncio.Event] = {}
        self._cleanup_interval = cleanup_interval
        self._timeout_grace_seconds = timeout_grace_seconds
        # 超时后的进度检查步长：一个步长内没有任何进展才判超时
        self._progress_check_seconds = max(1, progress_check_seconds)
        self._cleanup_task: Optional[asyncio.Task] = None
        self._processor_task: Optional[asyncio.Task] = None
        self._running = False
        # 显式停止标记：stop() 之后拒绝新任务入队；首次 start 之前允许预入队
        self._stopped = False
        self._stats = {
            "total_enqueued": 0,
            "total_completed": 0,
            "total_failed": 0,
            "total_cancelled": 0
        }
        self._start_lock = asyncio.Lock()

    async def start(self):
        """启动队列处理器和清理任务（并发调用安全）"""
        async with self._start_lock:
            if self._running:
                return
            self._running = True
            self._stopped = False
            self._processor_task = asyncio.create_task(self.process_queue())
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            logger.info(f"视频导出队列已启动（并发={self._max_concurrent}, 容量={self._max_queue_size}）")

    async def stop(self):
        """停止队列处理器，并收尾所有排队/运行中的任务（等待方会被唤醒）。

        与 start 共用生命周期锁：并发 start/stop 不会互相覆盖后台任务引用。
        """
        async with self._start_lock:
            self._stopped = True
            self._running = False
            if self._processor_task:
                self._processor_task.cancel()
                try:
                    await self._processor_task
                except asyncio.CancelledError:
                    pass
                self._processor_task = None
            if self._cleanup_task:
                self._cleanup_task.cancel()
                try:
                    await self._cleanup_task
                except asyncio.CancelledError:
                    pass
                self._cleanup_task = None

            async with self._lock:
                pending = [t for t in self._queue if t.status == TaskStatus.PENDING]
                self._queue = [t for t in self._queue if t.status != TaskStatus.PENDING]
                running = list(self._running_tasks.values())
            for task in pending:
                self._settle(task, TaskStatus.CANCELLED, error="队列已停止")
            workers = []
            for task in running:
                if task.worker and not task.worker.done():
                    task.cancel_requested = True
                    task.worker.cancel()
                    workers.append(task.worker)
                elif task.worker is None:
                    # 已登记执行但尚未启动：直接结算，执行入口的终态守卫会阻止其稍后启动
                    self._settle(task, TaskStatus.CANCELLED, error="队列已停止")
            # 已 done 的 worker 由其外层执行者收尾：失败处理会看到停止标记并按取消结算
            if workers:
                await asyncio.gather(*workers, return_exceptions=True)
            logger.info("视频导出队列已停止")

    async def add_task(self, user_id: str, coroutine_func: Callable,
                       priority: int = 0, args: tuple = (), kwargs: dict = None,
                       timeout_seconds: int = None, max_retries: int = None,
                       progress_arg: str = None) -> str:
        """添加任务到队列（优化版：支持用户任务聚合）

        progress_arg：任务协程中用于接收进度回调的参数名。声明后，队列会在执行时
        注入 ``async def report(value)`` 回调；超时后的收尾宽限据此判断任务是否仍在推进。
        """
        async with self._lock:
            # 停止后拒绝新任务；start 之前允许预入队（处理器启动后即消费）
            if self._stopped and not self._running:
                raise QueueFullError("导出队列已停止，请稍后再试")
            pending_and_running = (
                sum(1 for t in self._queue if t.status == TaskStatus.PENDING)
                + len(self._running_tasks)
            )
            if pending_and_running >= self._max_queue_size:
                raise QueueFullError(
                    f"队列已满（{self._max_queue_size}），请稍后重试"
                )

            task_id = str(uuid.uuid4())
            timeout = timeout_seconds or self._default_timeout
            retries = max_retries if max_retries is not None else self._default_max_retries

            task = QueueTask(
                task_id=task_id,
                user_id=user_id,
                priority=priority,
                created_at=time.time(),
                coroutine_func=coroutine_func,
                args=args,
                kwargs=kwargs or {},
                timeout_seconds=timeout,
                max_retries=retries,
                progress_arg=progress_arg
            )

            # 高并发优化：检查同一用户是否已有pending任务，进行智能排序
            user_pending_count = sum(1 for t in self._queue if t.user_id == user_id and t.status == TaskStatus.PENDING)
            
            # 如果同一用户已有多个pending任务，降低优先级避免垄断
            adjusted_priority = priority + min(user_pending_count * 2, 10)
            task.priority = adjusted_priority

            self._queue.append(task)
            
            # 优化排序：优先级 > 创建时间 > 用户公平性
            self._queue.sort(key=lambda t: (t.priority, t.created_at))

            self._task_events[task_id] = asyncio.Event()
            self._stats["total_enqueued"] += 1

            logger.info(f"任务已加入队列: {task_id[:8]}, 用户={user_id}, "
                        f"队列长度={len(self._queue)}, 优先级={adjusted_priority}(原{priority}), "
                        f"用户pending={user_pending_count}")

            return task_id

    async def update_progress(self, task_id: str, progress: int) -> None:
        """上报任务进度（0-100）。超时后的进度监控据此判断导出是否仍在推进。"""
        async with self._lock:
            task = self._find_task(task_id)
            if task and task.status == TaskStatus.RUNNING:
                task.progress = max(0, min(100, int(progress)))

    async def get_task_status(self, task_id: str) -> Optional[dict]:
        """获取任务状态"""
        async with self._lock:
            task = self._find_task(task_id)
            if not task:
                return None

            position = None
            if task.status == TaskStatus.PENDING:
                # 计算前面有多少个任务：正在运行的任务 + 队列中排在前面的pending任务
                running_count = len(self._running_tasks)
                pending_before = sum(1 for t in self._queue
                                     if t.created_at < task.created_at and t.status == TaskStatus.PENDING)
                position = running_count + pending_before

            return {
                "task_id": task_id,
                "status": task.status.value,
                "user_id": task.user_id,
                "position": position,
                "created_at": task.created_at,
                "started_at": task.started_at,
                "completed_at": task.completed_at,
                "retry_count": task.retry_count,
                "error": task.error,
                "result": task.result
            }

    async def wait_for_task(self, task_id: str, timeout: float = None) -> Optional[dict]:
        """等待任务完成"""
        if task_id not in self._task_events:
            return None

        try:
            await asyncio.wait_for(
                self._task_events[task_id].wait(),
                timeout=timeout
            )
        except asyncio.TimeoutError:
            return {"status": "timeout", "message": "等待任务超时"}

        return await self.get_task_status(task_id)

    async def cancel_task(self, task_id: str) -> bool:
        """取消任务

        排队中的任务直接置为取消；运行中的任务取消其协程（由执行协程负责结算终态）。
        """
        async with self._lock:
            task = self._find_task(task_id)
            if not task:
                return False

            if task.status == TaskStatus.PENDING:
                for index, queued in enumerate(self._queue):
                    if queued.task_id == task.task_id:
                        del self._queue[index]
                        break
                self._settle(task, TaskStatus.CANCELLED, error="任务已取消")
                logger.info(f"任务已取消: {task_id[:8]}")
                return True
            if task.status == TaskStatus.RUNNING:
                # 先记下取消意图：即使协程清理代码随后抛普通异常，也按取消结算
                task.cancel_requested = True
                if task.worker and not task.worker.done():
                    task.worker.cancel()
                    logger.info(f"运行中任务已请求取消: {task_id[:8]}")
                    return True
                self._settle(task, TaskStatus.CANCELLED, error="任务已取消")
                logger.info(f"运行中任务已取消: {task_id[:8]}")
                return True

            return False

    async def get_queue_stats(self) -> dict:
        """获取队列统计信息"""
        async with self._lock:
            pending = [t for t in self._queue if t.status == TaskStatus.PENDING]
            running = len(self._running_tasks)
            completed = len([t for t in self._completed_tasks.values()
                             if t.status == TaskStatus.COMPLETED])
            failed = len([t for t in self._completed_tasks.values()
                          if t.status in (TaskStatus.FAILED, TaskStatus.TIMEOUT)])

            return {
                "pending_count": len(pending),
                "running_count": running,
                "completed_count": completed,
                "failed_count": failed,
                "total_processed": len(self._completed_tasks),
                "total_enqueued": self._stats["total_enqueued"],
                "total_completed": self._stats["total_completed"],
                "total_failed": self._stats["total_failed"],
                "total_cancelled": self._stats["total_cancelled"],
                "max_concurrent": self._max_concurrent,
                "queue_size_limit": self._max_queue_size
            }

    async def estimate_wait_time(self, task_id: str) -> int:
        """预估任务等待时间（秒）"""
        async with self._lock:
            task = self._find_task(task_id)
            if not task:
                return 0

            if task.status != TaskStatus.PENDING:
                return 0

            position = sum(1 for t in self._queue
                          if t.created_at < task.created_at and t.status == TaskStatus.PENDING)

            if position == 0:
                return 0

            completed_tasks = [t for t in self._completed_tasks.values()
                              if t.status == TaskStatus.COMPLETED 
                              and t.started_at and t.completed_at]

            if completed_tasks:
                recent_tasks = completed_tasks[-5:]
                avg_duration = sum(t.completed_at - t.started_at for t in recent_tasks) / len(recent_tasks)
            else:
                avg_duration = 60

            estimated_seconds = int(position * avg_duration)

            running_count = len(self._running_tasks)
            if running_count > 0:
                estimated_seconds = int(estimated_seconds / (running_count + 1))

            return max(estimated_seconds, position * 10)

    async def process_queue(self):
        """处理队列中的任务（高并发优化版：批量处理+智能调度）"""
        while self._running:
            try:
                async with self._lock:
                    available_slots = self._max_concurrent - len(self._running_tasks)
                    if available_slots <= 0:
                        await asyncio.sleep(0.05)
                        continue

                    tasks_to_start = []
                    processed_users = set()
                    still_pending: list[QueueTask] = []

                    for task in self._queue:
                        if task.status != TaskStatus.PENDING:
                            continue

                        if (
                            len(tasks_to_start) >= available_slots
                            or task.user_id in processed_users
                        ):
                            still_pending.append(task)
                            continue

                        processed_users.add(task.user_id)
                        task.status = TaskStatus.RUNNING
                        task.started_at = time.time()
                        task.registered_generation = task.generation
                        self._running_tasks[task.task_id] = task
                        tasks_to_start.append(task)

                    self._queue = still_pending

                if tasks_to_start:
                    for task in tasks_to_start:
                        asyncio.create_task(self._execute_with_semaphore(task))
                    logger.info(f"批量启动 {len(tasks_to_start)} 个任务，当前运行: {len(self._running_tasks)}")

                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"队列处理异常: {e}")
                await asyncio.sleep(1)

    async def _execute_with_semaphore(self, task: QueueTask):
        """使用信号量执行任务"""
        async with self._semaphore:
            await self._execute_task(task)

    async def _invoke(self, task: QueueTask):
        """调用任务协程；仅在任务声明了 progress_arg 时注入进度回调（旧协程不受影响）。"""
        kwargs = dict(task.kwargs)
        if task.progress_arg:
            kwargs[task.progress_arg] = self._progress_callback(task.task_id)
        return await task.coroutine_func(*task.args, **kwargs)

    def _progress_callback(self, task_id: str):
        async def report(value: int):
            await self.update_progress(task_id, value)
        return report

    def _settle(self, task: QueueTask, status: TaskStatus, error: Optional[str] = None) -> bool:
        """结算任务终态（幂等）。调用方不得持有会与 _settle 内部操作冲突的假设；
        本方法只做同步字段写入与事件置位，不获取 _lock。"""
        if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED,
                           TaskStatus.TIMEOUT, TaskStatus.CANCELLED):
            return False
        task.status = status
        if error is not None:
            task.error = error
        task.completed_at = time.time()
        if status == TaskStatus.COMPLETED:
            self._stats["total_completed"] += 1
        elif status in (TaskStatus.FAILED, TaskStatus.TIMEOUT):
            self._stats["total_failed"] += 1
        elif status == TaskStatus.CANCELLED:
            self._stats["total_cancelled"] += 1
        self._completed_tasks[task.task_id] = task
        self._running_tasks.pop(task.task_id, None)
        event = self._task_events.get(task.task_id)
        if event:
            event.set()
        return True

    async def _await_grace(self, task: QueueTask, worker: asyncio.Task):
        """看门狗到点后的收尾宽限。

        视频导出常在合流/下载阶段越过时限：只要任务仍在推进（进度上报递增）就继续等；
        静默超过一个宽限窗口、或到达绝对上限才取消，避免误杀即将完成的导出。
        """
        grace = self._timeout_grace_seconds
        step = self._progress_check_seconds
        # 绝对上限按单调时钟计算（不受系统校时影响）；已消耗的执行时长按墙钟折算
        started_mono = time.monotonic()
        consumed = max(0.0, time.time() - (task.started_at or time.time()))
        hard_deadline = started_mono + max(0.0, task.timeout_seconds + 3 * grace - consumed)
        last_seen = task.progress
        last_change = started_mono
        landing_done = False
        logger.warning(
            f"任务到达执行时限，进入收尾宽限（{grace}s 静默判停，有进度则顺延）: "
            f"{task.task_id[:8]}, 用户={task.user_id}"
        )
        while not worker.done():
            now = time.monotonic()
            if now >= hard_deadline:
                break
            if now - last_change >= grace:
                if not landing_done:
                    # 落点窗口：让在途的进度上报先结算，再判定是否真的停滞
                    landing_done = True
                    await asyncio.sleep(0)
                    if task.progress > last_seen:
                        last_seen = task.progress
                        last_change = time.time()
                        landing_done = False
                        logger.info(
                            f"任务超时后仍在推进（进度 {task.progress}%），宽限顺延: "
                            f"{task.task_id[:8]}"
                        )
                        continue
                break
            remaining = min(step, hard_deadline - now, last_change + grace - now)
            try:
                result = await asyncio.wait_for(
                    asyncio.shield(worker), timeout=max(remaining, 0.001)
                )
                task.result = result
                self._settle(task, TaskStatus.COMPLETED)
                logger.info(
                    f"任务在宽限期内完成: {task.task_id[:8]}, "
                    f"耗时={int((task.completed_at or time.time()) - started)}秒"
                )
                return
            except asyncio.TimeoutError:
                if worker.done() and not worker.cancelled():
                    worker.result()  # 任务自身抛错（含其内部超时）→ 交由失败/重试路径
                if task.progress > last_seen:
                    last_seen = task.progress
                    last_change = time.time()
                    landing_done = False
                    logger.info(
                        f"任务超时后仍在推进（进度 {task.progress}%），宽限顺延: "
                        f"{task.task_id[:8]}"
                    )
        if not worker.done():
            task.cancel_requested = True
            worker.cancel()
            try:
                await worker
            except (asyncio.CancelledError, Exception):
                pass
        if worker.cancelled() or not worker.done():
            self._settle(task, TaskStatus.TIMEOUT, error=f"任务超时（{task.timeout_seconds}秒）")
            logger.warning(f"任务超时且无进展: {task.task_id[:8]}, 用户={task.user_id}")
        elif worker.exception() is not None:
            raise worker.exception()
        else:
            task.result = worker.result()
            self._settle(task, TaskStatus.COMPLETED)
            logger.info(f"任务在宽限期末尾完成: {task.task_id[:8]}")

    async def _handle_failure(self, task: QueueTask, error: Exception):
        """任务协程抛错：按重试预算重新入队或结算为失败。"""
        logger.error(f"任务执行失败: {task.task_id[:8]}, 错误={error}")
        if isinstance(error, PermanentTaskError):
            # 重试必然复现的失败（如任务自身超时、下载终败）不重试
            self._settle(task, TaskStatus.FAILED, error=str(error))
            logger.error(f"任务永久失败（不重试）: {task.task_id[:8]}, 错误={error}")
            return
        async with self._lock:
            # 终态守卫：取消/停机结算过的任务不得在这里复活重入队
            if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED,
                               TaskStatus.TIMEOUT, TaskStatus.CANCELLED):
                return
            if self._stopped:
                self._settle(task, TaskStatus.CANCELLED, error="队列已停止")
                return
            if task.retry_count < task.max_retries:
                task.retry_count += 1
                task.started_at = None
                task.progress = 0
                task.error = None
                # 清掉上一代已结束的 worker，取消路径不再被旧引用误导；
                # 代次自增后，旧执行者的 finally 凭代次识别自己已过时
                task.worker = None
                task.generation += 1
                # 状态改写与入队同锁原子完成：取消在窗口内要么走 RUNNING 分支、
                # 要么在队列中找到它，不会再触发 list.remove 的 ValueError
                task.status = TaskStatus.PENDING
                if not any(t.task_id == task.task_id for t in self._queue):
                    self._queue.append(task)
                    self._queue.sort(key=lambda t: (t.priority, t.created_at))
                logger.info(f"任务将重试: {task.task_id[:8]}, 第{task.retry_count}次重试")
                return
        self._settle(task, TaskStatus.FAILED, error=str(error))
        logger.error(f"任务最终失败: {task.task_id[:8]}, 已重试{task.retry_count}次")

    async def _execute_task(self, task: QueueTask):
        """执行单个任务。

        超时只从任务实际开始执行起计（排队等待不占用）；到点后进入收尾宽限而非立刻取消，
        已下载完成的视频不会被误杀。取消（用户/停止队列）会真正中断协程并释放并发槽。
        """
        worker = None
        gen = task.generation
        # 入口终态守卫：取消/停止先于执行启动时（调度与 worker 赋值之间的窗口），
        # 已结算的任务不再真正执行，避免"取消成功却照样渲染"
        if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED,
                           TaskStatus.TIMEOUT, TaskStatus.CANCELLED):
            return
        try:
            worker = asyncio.create_task(self._invoke(task))
            task.worker = worker
            try:
                result = await asyncio.wait_for(
                    asyncio.shield(worker), timeout=task.timeout_seconds
                )
                task.result = result
                self._settle(task, TaskStatus.COMPLETED)
                logger.info(f"任务完成: {task.task_id[:8]}, "
                            f"耗时={int(time.time() - (task.started_at or time.time()))}秒")
            except asyncio.TimeoutError:
                if worker.done() and not worker.cancelled():
                    # 任务自身恰好抛出 TimeoutError：与看门狗超时区分，走失败/重试路径
                    exc = worker.exception()
                    if exc is None:
                        task.result = worker.result()
                        self._settle(task, TaskStatus.COMPLETED)
                    else:
                        raise exc
                else:
                    await self._await_grace(task, worker)
            except asyncio.CancelledError:
                raise
        except asyncio.CancelledError:
            if worker and not worker.done():
                task.cancel_requested = True
                worker.cancel()
                try:
                    await worker
                except (asyncio.CancelledError, Exception):
                    pass
            if task.generation == gen:
                self._settle(task, TaskStatus.CANCELLED, error="任务已取消")
            logger.info(f"任务已取消: {task.task_id[:8]}")
        except Exception as e:
            if task.cancel_requested or (worker is not None and worker.cancelled()):
                # 取消后协程清理代码抛出的普通异常：按取消结算，绝不复活重试
                if task.generation == gen:
                    self._settle(task, TaskStatus.CANCELLED, error="任务已取消")
                logger.info(f"任务已取消: {task.task_id[:8]}")
            else:
                await self._handle_failure(task, e)
        finally:
            async with self._lock:
                # 运行登记按"登记代次"匹配：重试已让任务被新一代重新登记时，
                # 旧执行者不得移除新登记、也不得结算；自己的登记自己清
                if task.registered_generation == gen:
                    self._running_tasks.pop(task.task_id, None)
                    # 防御：正常路径都会先行结算；若仍处于 RUNNING 说明有未覆盖的退出路径
                    if task.status == TaskStatus.RUNNING:
                        self._settle(task, TaskStatus.FAILED, error="任务异常退出")

    async def _cleanup_loop(self):
        """定期清理已完成的任务"""
        while True:
            try:
                await asyncio.sleep(self._cleanup_interval)
                await self._cleanup_completed_tasks()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"清理任务异常: {e}")

    async def _cleanup_completed_tasks(self, max_age_seconds: int = 1800):
        """清理已完成的任务记录（30分钟）"""
        async with self._lock:
            now = time.time()
            to_remove = []

            for task_id, task in self._completed_tasks.items():
                if task.completed_at and (now - task.completed_at) > max_age_seconds:
                    to_remove.append(task_id)

            for task_id in to_remove:
                del self._completed_tasks[task_id]
                if task_id in self._task_events:
                    del self._task_events[task_id]

            if to_remove:
                logger.info(f"清理了 {len(to_remove)} 个已完成的任务记录")

    def _find_task(self, task_id: str) -> Optional[QueueTask]:
        """查找任务（需在锁内调用）"""
        for task in self._queue:
            if task.task_id == task_id:
                return task

        if task_id in self._running_tasks:
            return self._running_tasks[task_id]

        return self._completed_tasks.get(task_id)

    @property
    def max_queue_size(self) -> int:
        """队列容量（pending + running 的入队上限），供调用方在生成前做背压预检。"""
        return self._max_queue_size


class QueueFullError(Exception):
    """队列已满异常"""
    pass


class PermanentTaskError(Exception):
    """不可重试的任务失败：重试也必然复现（任务自身超时、下载终败等），直接结算失败。"""
    pass
