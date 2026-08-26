import asyncio

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from services.event_bus import event_bus
from services.job_service import job_service

router = APIRouter(tags=["streams"])


@router.get("/jobs/{job_id}/events")
async def job_events(job_id: str) -> EventSourceResponse:
    async def event_generator():
        snapshot = job_service.get_job(job_id)
        if snapshot is None:
            yield {"event": "job_failed", "data": '{"detail": "Job not found"}'}
            return

        yield {"event": "job_state", "data": snapshot.model_dump_json()}
        queue = event_bus.subscribe(job_id)
        try:
            while True:
                event = await asyncio.to_thread(event_bus.next_event, queue, 15.0)
                if event is None:
                    latest = job_service.get_job(job_id)
                    if latest is None:
                        yield {"event": "job_failed", "data": '{"detail": "Job not found"}'}
                        return
                    yield {"event": "heartbeat", "data": latest.model_dump_json()}
                    if latest.status.value in {"done", "failed", "cancelled"}:
                        return
                    continue

                yield event
                if event["event"] in {"job_done", "job_failed", "job_cancelled"}:
                    return
        finally:
            event_bus.unsubscribe(job_id, queue)

    return EventSourceResponse(event_generator())
