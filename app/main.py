from __future__ import annotations

import asyncio
import contextlib
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.bot_service import BotService
from app.config import Settings
from app.llm import DeepSeekClient
from app.memory import MemoryStore
from app.models import GroupMessageEvent
from app.logging_ctx import install_context_filter, new_context, request_ctx
from app.natcapqq_adapter import NatcapQQAdapter
from app.rag import RagStore
from app.qq_dispatcher import EventDeduplicator, QQMessageDispatcher
from app.storage_executor import StorageExecutor
from app.task_runtime import MessageTaskRuntime
from app.usage_budget import BudgetController, BudgetLimits, UsageBudgetStore

# Verified working models as of 2026-08-17 (probed concurrently, timeout 20s)
# meta/llama-3.1-8b-instruct is registered separately via NVIDIA_MODEL env var
NVIDIA_CHAT_MODELS = (
    "nvidia/nemotron-mini-4b-instruct",   # 0.8s — fastest, weaker Chinese
    "stepfun-ai/step-3.7-flash",          # 1.0s — good Chinese
    "meta/llama-3.1-70b-instruct",        # 5s   — stronger reasoning
    "openai/gpt-oss-120b",                # 12s  — largest available
)

QWEN_CHAT_MODELS = (
    "qwen3.7-flash",
    "deepseek-v4-flash",
    "qwen-flash",
    "qwen3.5-flash",
)

NEWAPI_CHAT_MODELS = (
    "gpt-5.4-mini",
    "gpt-5.5",
    "gpt-5.6-luna",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-6-astra",
)

CCTQ_CHAT_MODELS = (
    "gpt-6-luna",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-6-astra",
)


def _setup_logging(level: str) -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(level)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(log_dir / "bot.log", maxBytes=5_000_000, backupCount=5)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    install_context_filter()


def create_app() -> FastAPI:
    settings = Settings.from_env()
    _setup_logging(settings.log_level)
    logger = logging.getLogger(__name__)

    memory = MemoryStore(settings.sqlite_path, history_limit=settings.message_history_limit)
    rag = RagStore(settings.sqlite_path) if settings.rag_enabled else None
    from app.vector_rag import VectorRagStore
    vector_rag = VectorRagStore(settings.sqlite_path.parent / "chromadb") if settings.rag_enabled else None
    storage_executor = StorageExecutor(
        sqlite_workers=settings.storage_sqlite_workers,
        vector_workers=settings.storage_vector_workers,
        queue_capacity=settings.storage_queue_capacity,
    )
    budget_controller = BudgetController(
        UsageBudgetStore(settings.sqlite_path),
        storage_executor,
        BudgetLimits(
            global_requests_per_day=settings.llm_budget_global_requests_per_day,
            group_requests_per_day=settings.llm_budget_group_requests_per_day,
            user_requests_per_day=settings.llm_budget_user_requests_per_day,
            global_tokens_per_day=settings.llm_budget_global_tokens_per_day,
            group_tokens_per_day=settings.llm_budget_group_tokens_per_day,
            user_tokens_per_day=settings.llm_budget_user_tokens_per_day,
            default_max_completion_tokens=settings.llm_budget_default_max_completion_tokens,
            reservation_lease_seconds=settings.llm_budget_reservation_lease_seconds,
        ),
        max_concurrent_model_requests=settings.llm_max_concurrent_requests,
    )

    def _new_llm_client(api_key: str, base_url: str, model: str, timeout: float) -> DeepSeekClient:
        return DeepSeekClient(
            api_key=api_key,
            base_url=base_url,
            model=model,
            timeout=timeout,
            budget_controller=budget_controller,
        )

    llm = _new_llm_client(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        timeout=settings.deepseek_timeout,
    )
    llms = {"deepseek": llm}
    if settings.nvidia_api_key:
        default_nvidia = _new_llm_client(
            api_key=settings.nvidia_api_key,
            base_url=settings.nvidia_base_url,
            model=settings.nvidia_model,
            timeout=settings.nvidia_timeout,
        )
        llms["nvidia"] = default_nvidia
        llms[settings.nvidia_model.lower()] = default_nvidia
        for model in NVIDIA_CHAT_MODELS:
            if model.lower() not in llms:
                llms[model.lower()] = _new_llm_client(
                    api_key=settings.nvidia_api_key,
                    base_url=settings.nvidia_base_url,
                    model=model,
                    timeout=settings.nvidia_timeout,
                )
    if settings.qwen_api_key:
        default_qwen = _new_llm_client(
            api_key=settings.qwen_api_key,
            base_url=settings.qwen_base_url,
            model=settings.qwen_model,
            timeout=settings.qwen_timeout,
        )
        llms["qwen"] = default_qwen
        llms[settings.qwen_model.lower()] = default_qwen
        for model in QWEN_CHAT_MODELS:
            if model.lower() not in llms:
                llms[model.lower()] = _new_llm_client(
                    api_key=settings.qwen_api_key,
                    base_url=settings.qwen_base_url,
                    model=model,
                    timeout=settings.qwen_timeout,
                )
    if settings.newapi_api_key:
        default_newapi = _new_llm_client(
            api_key=settings.newapi_api_key,
            base_url=settings.newapi_base_url,
            model=settings.newapi_model,
            timeout=settings.newapi_timeout,
        )
        llms["newapi"] = default_newapi
        llms[settings.newapi_model.lower()] = default_newapi
        for model in NEWAPI_CHAT_MODELS:
            if model.lower() not in llms:
                llms[model.lower()] = _new_llm_client(
                    api_key=settings.newapi_api_key,
                    base_url=settings.newapi_base_url,
                    model=model,
                    timeout=settings.newapi_timeout,
                )
    if settings.cctq_api_key:
        default_cctq = _new_llm_client(
            api_key=settings.cctq_api_key,
            base_url=settings.cctq_base_url,
            model=settings.cctq_model,
            timeout=settings.cctq_timeout,
        )
        llms["cctq"] = default_cctq
        llms[f"cctq/{settings.cctq_model.lower()}"] = default_cctq
        for model in CCTQ_CHAT_MODELS:
            alias = f"cctq/{model.lower()}"
            if alias not in llms:
                llms[alias] = _new_llm_client(
                    api_key=settings.cctq_api_key,
                    base_url=settings.cctq_base_url,
                    model=model,
                    timeout=settings.cctq_timeout,
                )
    adapter = NatcapQQAdapter(
        api_base_url=settings.natcapqq_api_base_url,
        api_token=settings.natcapqq_api_token,
        bot_qq_id=settings.bot_qq_id,
        callback_token=settings.natcapqq_callback_token,
        timeout=settings.http_timeout,
    )
    service = BotService(
        settings=settings,
        memory=memory,
        llm=llm,
        rag=rag,
        llms=llms,
        vector_rag=vector_rag,
        storage_executor=storage_executor,
    )
    dispatcher = QQMessageDispatcher(adapter=adapter, max_chars=settings.qq_message_max_chars)
    event_deduplicator = EventDeduplicator()

    app = FastAPI(title="qq-deepseek-bot")
    app.state.settings = settings
    app.state.memory = memory
    app.state.rag = rag
    app.state.llm = llm
    app.state.llms = llms
    app.state.adapter = adapter
    app.state.service = service
    app.state.storage_executor = storage_executor
    app.state.usage_budget_controller = budget_controller
    app.state.callback_auth_ready = adapter.callback_auth_configured
    message_runtime: MessageTaskRuntime | None = None

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        scheduler_task = getattr(app.state, "scheduler_task", None)
        if scheduler_task:
            scheduler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await scheduler_task
        if message_runtime is not None:
            await message_runtime.aclose(settings.shutdown_drain_timeout)
        await storage_executor.aclose(settings.shutdown_drain_timeout)
        await adapter.aclose()
        closed: set[int] = set()
        for client in llms.values():
            if id(client) not in closed:
                closed.add(id(client))
                await client.aclose()
        logger.info("adapter closed")

    @app.on_event("startup")
    async def _startup() -> None:
        if not adapter.callback_auth_configured:
            logger.error(
                "OneBot callback authentication is not configured; callbacks will be rejected"
            )
        if not any(
            (
                settings.llm_budget_global_requests_per_day,
                settings.llm_budget_group_requests_per_day,
                settings.llm_budget_user_requests_per_day,
                settings.llm_budget_global_tokens_per_day,
                settings.llm_budget_group_tokens_per_day,
                settings.llm_budget_user_tokens_per_day,
            )
        ):
            logger.warning(
                "model usage accounting is active, but no request or token admission caps are configured; cost remains unknown"
            )
        try:
            recovered = await budget_controller.recover_expired_reservations()
            if recovered:
                logger.warning("recovered %s expired model usage reservations as unknown", recovered)
        except Exception:  # noqa: BLE001
            logger.exception("model usage reservation recovery failed")
        if message_runtime is not None:
            message_runtime.start()
        app.state.scheduler_task = asyncio.create_task(_scheduled_task_loop())

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok"}

    @app.get("/ready")
    async def readiness() -> JSONResponse:
        if not adapter.callback_auth_configured:
            return JSONResponse(
                {
                    "status": "not_ready",
                    "reason": "onebot_callback_auth_not_configured",
                },
                status_code=503,
            )
        return JSONResponse({"status": "ready"})

    @app.post("/onebot/event")
    async def onebot_event(request: Request) -> JSONResponse:
        body = await request.body()
        if not adapter.verify_callback_token(request.headers, body):
            raise HTTPException(status_code=401, detail="unauthorized")
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="invalid payload")

        event = adapter.parse_group_event(payload)
        if event is None:
            return JSONResponse({"status": "ignored"})
        if event_deduplicator.contains(event.message_id):
            return JSONResponse({"status": "duplicate"})

        if message_runtime is None or not message_runtime.try_submit(event):
            return JSONResponse(
                {"status": "busy"},
                status_code=503,
                headers={"Retry-After": "1"},
            )
        # Mark only after successful admission so a rejected event can be retried.
        event_deduplicator.mark(event.message_id)
        return JSONResponse({"status": "accepted"})

    async def _resolve_bot_names(group_id: str) -> tuple[str, ...]:
        try:
            names = await asyncio.wait_for(
                adapter.get_group_bot_names(
                    group_id,
                    cache_seconds=settings.bot_name_cache_seconds,
                ),
                timeout=1.0,
            )
        except Exception:  # noqa: BLE001
            names = ()
        if settings.bot_default_name not in names:
            names = (*names, settings.bot_default_name)
        return tuple(name for name in names if name)

    async def _process_group_message(event) -> None:
        event = await adapter.hydrate_quote(event)
        event.bot_names = await _resolve_bot_names(event.group_id)
        ctx = new_context(group_id=str(event.group_id), user_id=str(event.user_id))
        token = request_ctx.set(ctx)
        expected_reply = service.expects_reply(event)
        network_notice_sent = False

        # Report work when the agent actually chooses a tool.
        async def on_tool_call(status_text: str = "正在联网查询，稍等。") -> None:
            nonlocal network_notice_sent
            network_notice_sent = True
            try:
                await dispatcher.send(event.group_id, status_text, status=True)
            except Exception:  # noqa: BLE001
                logger.exception("failed to send tool call notice")

        task = asyncio.create_task(service.handle_group_message(event, on_tool_call=on_tool_call))
        try:
            try:
                reply = await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=settings.reply_notice_delay,
                )
            except asyncio.TimeoutError:
                if expected_reply and not network_notice_sent:
                    try:
                        await dispatcher.send(event.group_id, "正在处理，稍等。", status=True)
                    except Exception:  # noqa: BLE001
                        logger.exception("failed to send wait notice")
                reply = await task

            if reply and reply.should_send:
                await dispatcher.send(event.group_id, reply.text, reply_to=event.message_id)
                if not service.is_command_event(event):
                    assert message_runtime is not None
                    message_runtime.spawn_background(
                        service.maybe_update_group_summary(event.group_id),
                        name=f"group-summary-{event.group_id}",
                    )
        except asyncio.CancelledError:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        except Exception:  # noqa: BLE001
            logger.exception("failed to process event")
        finally:
            request_ctx.reset(token)

    async def _scheduled_task_loop() -> None:
        while True:
            try:
                now = int(time.time())
                due_tasks = await storage_executor.run_sqlite(
                    memory.claim_due_scheduled_tasks,
                    now,
                    limit=3,
                )
                for task_row in due_tasks:
                    await _run_scheduled_task(task_row)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("scheduled task loop failed")
            await asyncio.sleep(settings.scheduler_poll_interval)

    async def _run_scheduled_task(task_row: dict[str, Any]) -> None:
        task_id = int(task_row["id"])
        group_id = str(task_row["group_id"])
        now = int(time.time())
        if await service.is_group_busy(group_id):
            await storage_executor.run_sqlite(memory.release_scheduled_task, task_id)
            return

        task_notice_sent = False

        async def on_tool_call(status_text: str = "正在联网查询，稍等。") -> None:
            nonlocal task_notice_sent
            task_notice_sent = True
            try:
                await dispatcher.send(group_id, f"定时任务 #{task_id}：{status_text}", status=True)
            except Exception:  # noqa: BLE001
                logger.exception("failed to send scheduled task tool notice")

        try:
            reply_text = await service.run_scheduled_task(task_row, on_tool_call=on_tool_call)
            if reply_text is None:
                await storage_executor.run_sqlite(memory.release_scheduled_task, task_id)
                return
            await dispatcher.send(group_id, f"定时任务 #{task_id}：\n{reply_text}")
            await storage_executor.run_sqlite(memory.finish_scheduled_task, task_id, int(time.time()))
            if message_runtime is not None:
                message_runtime.spawn_background(
                    service.maybe_update_group_summary(group_id),
                    name=f"group-summary-{group_id}",
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("scheduled task %s failed", task_id)
            await storage_executor.run_sqlite(
                memory.fail_scheduled_task,
                task_id,
                int(time.time()),
                str(exc),
            )
            with contextlib.suppress(Exception):
                await dispatcher.send(
                    group_id,
                    f"定时任务 #{task_id} 执行失败，已自动停用，不会继续重试扣费。",
                )

    message_runtime = MessageTaskRuntime(
        _process_group_message,
        worker_count=settings.qq_message_workers,
        queue_capacity=settings.qq_message_queue_capacity,
    )
    app.state.message_runtime = message_runtime
    return app


app = create_app()
