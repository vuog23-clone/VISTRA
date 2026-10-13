"""Bounded browser prefetch; each worker owns its Playwright context."""
from concurrent.futures import Future, ThreadPoolExecutor
from queue import Empty, Queue
from threading import Event, Semaphore

from src.data.preprocessing.google_maps_photos import GoogleMapsPhotoResolver


class ParallelMapsPhotoResolver:
    def __init__(self, places, workers=3, *, factory=GoogleMapsPhotoResolver):
        self.factory = factory
        self.futures = {p['place_id']: Future() for p in places}
        self.jobs = Queue()
        for place in places:
            self.jobs.put(place)
        self.stop = Event()
        self.slots = Semaphore(workers * 4)
        self.workers = workers
        self.pool = None

    def _context(self):
        # Validate the browser dependency before the importer writes anything.
        probe = self.factory()
        try:
            probe._context()
        finally:
            probe.close()
        self.pool = ThreadPoolExecutor(max_workers=self.workers)
        self.tasks = [self.pool.submit(self._worker) for _ in range(self.workers)]

    def _worker(self):
        resolver = self.factory()
        processed = 0
        try:
            while not self.stop.is_set():
                if not self.slots.acquire(timeout=0.2):
                    continue
                try:
                    place = self.jobs.get_nowait()
                except Empty:
                    self.slots.release()
                    break
                future = self.futures[place['place_id']]
                try:
                    future.set_result(resolver.resolve(place))
                except Exception as error:
                    future.set_exception(error)
                processed += 1
                if processed % 40 == 0:
                    # Maps keeps browser caches across pages. Periodic recycling
                    # bounds memory during imports of hundreds of places.
                    resolver.close()
                    resolver = self.factory()
        finally:
            resolver.close()

    def resolve(self, place):
        try:
            return self.futures[place['place_id']].result()
        finally:
            self.slots.release()

    def close(self):
        self.stop.set()
        if self.pool is not None:
            self.pool.shutdown(wait=True, cancel_futures=True)
