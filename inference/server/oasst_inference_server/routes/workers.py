import asyncio
import datetime
from typing import cast

import fastapi
import pydantic
import websockets.exceptions
from fastapi import Depends
from loguru import logger
from oasst_inference_server import chat_repository, database, deps, models, queueing, worker_utils
from oasst_inference_server.settings import settings
from oasst_shared.schemas import inference


class WorkerDisconnectException(Exception):
    def __init__(self):
        super().__init__("Worker disconnected")


WSException = (
    websockets.exceptions.WebSocketException,
    websockets.exceptions.ConnectionClosedError,
    fastapi.WebSocketException,
    fastapi.WebSocketDisconnect,
    WorkerDisconnectException,
)

router = fastapi.APIRouter(
    prefix="/workers",
    tags=["workers"],
)


class WorkerError(Exception):
    def __init__(
        self,
        message: str,
        did_work: bool,
        original_exception: Exception | None = None,
    ):
        super().__init__(message)
        self.did_work = did_work
        self.original_exception = original_exception


async def add_worker_connect_event(
    session: database.AsyncSession,
    worker_id: str,
    worker_config: inference.WorkerConfig,
):
    event = models.DbWorkerEvent(
        worker_id=worker_id,
        event_type=models.WorkerEventType.connect,
        worker_config=worker_config,
    )
    session.add(event)
    await session.commit()


class WorkRequestContainer(pydantic.BaseModel):
    work_request: inference.WorkRequest
    message_id: str
    start_time: datetime.datetime = pydantic.Field(default_factory=datetime.datetime.utcnow)
    num_responses: int = 0

    class Config:
        arbitrary_types_allowed = True


WorkRequestContainerMap = dict[str, WorkRequestContainer]


class WorkRequestNotFound(Exception):
    def __init__(self, request_id: str):
        super().__init__(f"Work request not found: {request_id=}")
        self.request_id = request_id


def get_work_request_container(work_request_map: WorkRequestContainerMap, request_id: str) -> WorkRequestContainer:
    if request_id is None:
        raise WorkRequestNotFound(request_id)
    container = work_request_map.get(request_id)
    if container is None:
        raise WorkRequestNotFound(request_id)
    return container


@router.websocket("/work")
async def handle_worker(websocket: fastapi.WebSocket, worker_id: str = Depends(worker_utils.get_worker_id)):
    logger.info(f"handle_worker: {worker_id=}")
    await websocket.accept()
    worker_config = await worker_utils.receive_worker_config(websocket)
    logger.info(f"handle_worker: {worker_config=}")
    worker_compat_hash = worker_config.compat_hash
    work_queue = queueing.work_queue(deps.redis_client, worker_compat_hash)
    redis_client = deps.make_redis_client()
    blocking_work_queue = queueing.work_queue(redis_client, worker_compat_hash)
    worker_session = worker_utils.WorkerSession(
        worker_id=worker_id,
        config=worker_config,
    )
    work_request_map: dict[str, WorkRequestContainer] = {}
    pending_futures = set()
    try:
        async with deps.manual_create_session() as session:
            await add_worker_connect_event(session=session, worker_id=worker_id, worker_config=worker_config)
        await worker_utils.store_worker_session(worker_session)

        async def _update_session(metrics: inference.WorkerMetricsInfo):
            worker_session.requests_in_flight = len(work_request_map)
            worker_session.metrics = metrics
            await worker_utils.store_worker_session(worker_session)

        def _add_dequeue(ftrs: set):
            requests_in_progress = len(work_request_map)
            if requests_in_progress < worker_config.max_parallel_requests:
                ftrs.add(asyncio.ensure_future(blocking_work_queue.dequeue(timeout=0)))

        def _add_receive(ftrs: set):
            ftrs.add(asyncio.ensure_future(worker_utils.receive_worker_response(websocket=websocket)))

        _add_dequeue(pending_futures)
        _add_receive(pending_futures)

        logger.info(f"handle_worker: {worker_id=} started")
        while True:
            if websocket.client_state == fastapi.websockets.WebSocketState.DISCONNECTED:
                raise WorkerDisconnectException("Worker disconnected")
            (done, pending_futures) = await asyncio.wait(
                pending_futures, timeout=settings.worker_ping_interval, return_when=asyncio.FIRST_COMPLETED
            )
            ftr: asyncio.Future
            for ftr in done:
                result = ftr.result()
                if result is None:
                    logger.error(f"handle_worker: {worker_id=} received None from queue. This should never happen.")
                    raise RuntimeError("Received None from queue. This should never happen.")
                elif isinstance(result, tuple):
                    try:
                        _, message_id = result
                        work_request = await initiate_work_for_message(
                            websocket=websocket,
                            work_queue=work_queue,
                            message_id=message_id,
                            worker_id=worker_id,
                            worker_config=worker_config,
                        )
                        work_request_map[work_request.id] = WorkRequestContainer(
                            work_request=work_request, message_id=message_id
                        )
                    finally:
                        _add_dequeue(pending_futures)
                else:
                    try:
                        worker_response: inference.WorkerResponse = result
                        match worker_response.response_type:
                            case "pong":
                                worker_response = cast(inference.PongResponse, worker_response)
                                await _update_session(worker_response.metrics)
                            case "token":
                                worker_response = cast(inference.TokenResponse, worker_response)
                                await handle_token_response(
                                    work_request_map=work_request_map,
                                    response=worker_response,
                                )
                            case "generated_text":
                                worker_response = cast(inference.GeneratedTextResponse, worker_response)
                                await handle_generated_text_response(
                                    work_request_map=work_request_map,
                                    response=worker_response,
                                )
                                await _update_session(worker_response.metrics)
                            case "error":
                                worker_response = cast(inference.ErrorResponse, worker_response)
                                await handle_error_response(
                                    work_request_map=work_request_map,
                                    response=worker_response,
                                )
                                await _update_session(worker_response.metrics)
                            case "general_error":
                                worker_response = cast(inference.GeneralErrorResponse, worker_response)
                                await handle_general_error_response(
                                    response=worker_response,
                                )
                                await _update_session(worker_response.metrics)
                            case _:
                                raise RuntimeError(f"Unknown response type: {worker_response.response_type}")
                    finally:
                        if len(pending_futures) == 0:
                            _add_dequeue(pending_futures)
                        _add_receive(pending_futures)
            if not done:
                await worker_utils.send_worker_request(websocket, inference.PingRequest())

    except Exception as e:
        logger.exception(f"Error while handling worker {worker_id}: {str(e)}")
        logger.info(f"Handling {len(work_request_map)} work requests outstanding")
        for container in work_request_map.values():
            try:
                message_id = container.message_id
                if container.num_responses == 0:
                    logger.warning(f"Marking {message_id=} as pending since no work was done.")
                    async with deps.manual_chat_repository() as cr:
                        await cr.reset_work(message_id)
                    await work_queue.enqueue(message_id)
                else:
                    logger.warning(f"Aborting {message_id=}")
                    async with deps.manual_chat_repository() as cr:
                        await cr.abort_work(message_id, reason=str(e))
            except Exception as e:
                logger.exception(f"Error while trying to reset work for {message_id=}: {str(e)}")
    finally:
        logger.info(f"Worker {worker_id} disconnected")
        try:
            await redis_client.close()
        except Exception:
            logger.warning("Error while closing redis client")
        try:
            await worker_utils.delete_worker_session(worker_session.id)
        except Exception:
            logger.warning("Error while deleting worker session")
        # try closing websocket if it's still open
        logger.info(f"Cancelling {len(pending_futures)} pending futures")
        for ftr in pending_futures:
            try:
                ftr.cancel()
            except Exception:
                logger.warning("Error while cancelling pending future")
        try:
            await websocket.close()
        except Exception:
            logger.warning("Error while closing websocket")


@router.get("/sessions")
async def list_worker_sessions() -> list[worker_utils.WorkerSession]:
    redis_client = deps.redis_client
    try:
        worker_configs = []
        async for key in redis_client.scan_iter("worker_session:*"):
            worker_config_json = await redis_client.get(key)
            worker_config = worker_utils.WorkerSession.parse_raw(worker_config_json)
            worker_configs.append(worker_config)
    except Exception as e:
        logger.exception(f"Error while listing worker sessions: {str(e)}")
        raise
    return worker_configs


@router.on_event("startup")
async def clear_worker_sessions():
    redis_client = deps.redis_client
    try:
        logger.warning("Clearing worker sessions")
        async for key in redis_client.scan_iter("worker_session:*"):
            await redis_client.getdel(key)
        logger.warning("Successfully cleared worker sessions")
    except Exception as e:
        logger.exception(f"Error while clearing worker sessions: {str(e)}")
        raise


async def initiate_work_for_message(
    *,
    websocket: fastapi.WebSocket,
    work_queue: queueing.RedisQueue,
    message_id: str,
    worker_id: str,
    worker_config: inference.WorkerConfig,
) -> inference.WorkRequest:
    async with deps.manual_create_session() as session:
        cr = chat_repository.ChatRepository(session=session)

        message = await cr.start_work(
            message_id=message_id,
            worker_id=worker_id,
            worker_config=worker_config,
        )
        work_request = await worker_utils.build_work_request(session, message.id)

    logger.info(f"Created {work_request=} with {len(work_request.thread.messages)=}")
    try:
        await worker_utils.send_worker_request(websocket, work_request)
    except Exception as e:
        logger.exception(f"Error while sending work request to worker: {str(e)}")
        async with deps.manual_create_session() as session:
            await cr.reset_work(message_id)
        await work_queue.enqueue(message_id)
        raise

    return work_request


async def handle_token_response(
    response: inference.TokenResponse,
    work_request_map: WorkRequestContainerMap,
):
    work_response_container = get_work_request_container(work_request_map, response.request_id)
    message_queue = queueing.message_queue(
        deps.redis_client,
        message_id=work_response_container.message_id,
    )
    await message_queue.enqueue(response.json(), expire=settings.message_queue_expire)
    work_response_container.num_responses += 1


async def handle_generated_text_response(
    response: inference.GeneratedTextResponse,
    work_request_map: WorkRequestContainerMap,
):
    work_response_container = get_work_request_container(work_request_map, response.request_id)
    message_id = work_response_container.message_id
    async with deps.manual_create_session() as session:
        cr = chat_repository.ChatRepository(session=session)
        message = await cr.complete_work(
            message_id=message_id,
            content=response.text,
        )
        logger.info(f"Completed work for {message_id=}")
    message_packet = inference.InternalFinishedMessageResponse(
        message=message.to_read(),
    )
    message_queue = queueing.message_queue(
        deps.redis_client,
        message_id=message_id,
    )
    await message_queue.enqueue(message_packet.json(), expire=settings.message_queue_expire)
    del work_request_map[response.request_id]


async def handle_error_response(
    response: inference.ErrorResponse,
    work_request_map: WorkRequestContainerMap,
):
    logger.warning(f"Got error {response=}")
    work_response_container = get_work_request_container(work_request_map, response.request_id)
    async with deps.manual_chat_repository() as cr:
        await cr.abort_work(work_response_container.message_id, reason=str(response.error))
    del work_request_map[response.request_id]


async def handle_general_error_response(
    response: inference.GeneralErrorResponse,
):
    logger.warning(f"Got general error {response=}")
