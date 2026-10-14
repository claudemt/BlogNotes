"""One browser operation at a time; bounded in-memory task history."""
from __future__ import annotations
import logging
import threading
import uuid
from collections import deque
from datetime import datetime

logger = logging.getLogger(__name__)

class Job:
    def __init__(self, name: str):
        self.id = uuid.uuid4().hex[:12]
        self.name = name
        self.state = 'running'
        self.message = '准备中'
        self.created_at = datetime.now().isoformat(timespec='seconds')
        self.log: deque[str] = deque(maxlen=100)

    def report(self, message: str):
        self.message = message
        self.log.append(f"[{datetime.now():%H:%M:%S}] {message}")

    def as_dict(self):
        return dict(id=self.id, name=self.name, state=self.state, message=self.message,
                    created_at=self.created_at, log=list(self.log))

class JobManager:
    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self.guard = threading.Lock()
        self.busy = False

    def submit(self, name: str, fn):
        with self.guard:
            if self.busy:
                raise RuntimeError('浏览器正在执行另一项任务，请完成后再试')
            self.busy = True
            job = Job(name)
            self.jobs[job.id] = job
            for key in list(self.jobs)[:-60]:
                if self.jobs[key].state != 'running':
                    self.jobs.pop(key, None)

        def runner():
            try:
                fn(job)
                job.state = 'done'
                job.report('操作结束')
            except Exception as exc:
                logger.exception('任务失败：%s', name)
                job.state = 'error'
                job.report(str(exc))
            finally:
                with self.guard:
                    self.busy = False
        threading.Thread(target=runner, daemon=True, name='travelnotes-' + job.id).start()
        return job

    def get(self, id: str):
        return self.jobs.get(id)

    def find_running(self, name: str | None = None):
        """The running job whose name matches, if any.

        Used to *attach* a second click to the task already in flight instead of
        rejecting it — a repeated 登录 click must not start another browser.
        """
        with self.guard:
            for job in self.jobs.values():
                if job.state == 'running' and (name is None or job.name == name):
                    return job
        return None
