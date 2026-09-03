"""In-process event bus with SSE fan-out.

Agents publish; the HTTP layer subscribes. Every event is persisted first, so a
subscriber that joins late (or reconnects) can replay from a sequence number
instead of losing the middle of the story.
"""

import queue
import threading


class EventBus:
    def __init__(self, store):
        self.store = store
        self._lock = threading.Lock()
        self._subscribers = set()

    def publish(self, etype, agent_id=None, task_id=None, **payload):
        event = self.store.add_event(etype, agent_id, task_id, payload)
        with self._lock:
            dead = []
            for q in self._subscribers:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self._subscribers.discard(q)
        return event

    def subscribe(self):
        q = queue.Queue(maxsize=1000)
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._subscribers.discard(q)

    @property
    def subscriber_count(self):
        with self._lock:
            return len(self._subscribers)
