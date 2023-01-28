import pytest

from sentry_sdk import start_transaction
from sentry_sdk.integrations.arq import ArqIntegration

from arq.connections import ArqRedis
from arq.jobs import Job
from arq.utils import timestamp_ms
from arq.worker import Retry, Worker

from fakeredis.aioredis import FakeRedis


@pytest.fixture(autouse=True)
def patch_fakeredis_info_command():
    from fakeredis._fakesocket import FakeSocket

    if not hasattr(FakeSocket, "info"):
        from fakeredis._commands import command
        from fakeredis._helpers import SimpleString

        @command((SimpleString,), name="info")
        def info(self, section):
            return section

        FakeSocket.info = info


@pytest.fixture
def init_arq(sentry_init):
    def inner(functions, allow_abort_jobs=False):
        sentry_init(
            integrations=[ArqIntegration()],
            traces_sample_rate=1.0,
            send_default_pii=True,
            debug=True,
        )

        server = FakeRedis()
        pool = ArqRedis(pool_or_conn=server.connection_pool)
        return pool, Worker(
            functions, redis_pool=pool, allow_abort_jobs=allow_abort_jobs
        )

    return inner


@pytest.mark.asyncio
async def test_job_result(init_arq):
    async def increase(ctx, num):
        return num + 1

    increase.__qualname__ = increase.__name__

    pool, worker = init_arq([increase])

    job = await pool.enqueue_job("increase", 3)

    assert isinstance(job, Job)

    await worker.run_job(job.job_id, timestamp_ms())
    result = await job.result()
    job_result = await job.result_info()

    assert result == 4
    assert job_result.result == 4


@pytest.mark.asyncio
async def test_job_retry(capture_events, init_arq):
    async def retry_job(ctx):
        if ctx["job_try"] < 2:
            raise Retry

    retry_job.__qualname__ = retry_job.__name__

    pool, worker = init_arq([retry_job])

    job = await pool.enqueue_job("retry_job")

    events = capture_events()

    await worker.run_job(job.job_id, timestamp_ms())

    event = events.pop(0)
    assert event["contexts"]["trace"]["status"] == "aborted"
    assert event["transaction"] == "retry_job"
    assert event["tags"]["arq_task_id"] == job.job_id
    assert event["extra"]["arq-job"]["retry"] == 1

    await worker.run_job(job.job_id, timestamp_ms())

    event = events.pop(0)
    assert event["contexts"]["trace"]["status"] == "ok"
    assert event["transaction"] == "retry_job"
    assert event["tags"]["arq_task_id"] == job.job_id
    assert event["extra"]["arq-job"]["retry"] == 2


@pytest.mark.asyncio
async def test_enqueue_job(capture_events, init_arq):
    async def dummy_job(_):
        pass

    pool, _ = init_arq([dummy_job])

    events = capture_events()

    with start_transaction() as transaction:
        await pool.enqueue_job("dummy_job")

    (event,) = events

    assert event["contexts"]["trace"]["trace_id"] == transaction.trace_id
    assert event["contexts"]["trace"]["span_id"] == transaction.span_id

    assert len(event["spans"])
    assert event["spans"][0]["op"] == "queue.submit.arq"
    assert event["spans"][0]["description"] == "dummy_job"
