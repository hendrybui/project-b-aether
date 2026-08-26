from __future__ import annotations

from queue import Empty, Queue


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[str, list[Queue]] = {}

    def subscribe(self, job_id: str) -> Queue:
        queue: Queue = Queue()
        self._subscribers.setdefault(job_id, []).append(queue)
        return queue

    def unsubscribe(self, job_id: str, queue: Queue) -> None:
        subscribers = self._subscribers.get(job_id, [])
        if queue in subscribers:
            subscribers.remove(queue)
        if not subscribers and job_id in self._subscribers:
            self._subscribers.pop(job_id, None)

    def publish(self, job_id: str, event: str, data: str) -> None:
        for queue in self._subscribers.get(job_id, []):
            queue.put({"event": event, "data": data})

    @staticmethod
    def next_event(queue: Queue, timeout: float = 15.0) -> dict | None:
        try:
            return queue.get(timeout=timeout)
        except Empty:
            return None


event_bus = EventBus()
