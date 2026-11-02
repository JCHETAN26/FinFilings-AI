"""AuditManager contracts. Agents are scripted TEST DOUBLES; timings here are not benchmarks."""
import asyncio
from pathlib import Path
import sys
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parent))
import httpx
from pydantic import ValidationError
from audit_web import limits
from audit_web.errors import ProjectionError, classify
from audit_web.manager import AuditManager, Busy, Closed
from audit_web.projection import project_result
from audit_web_testkit import BlockingAgent, InstantAgent


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


async def settle(manager, job_id):
    while manager.get(job_id) is not None and manager.get(job_id).status not in ('completed', 'failed', 'cancelled'):
        await asyncio.sleep(0.005)
    return manager.get(job_id)


class Admission(unittest.IsolatedAsyncioTestCase):
    async def test_second_submit_is_busy_until_first_finishes_then_slot_frees(self):
        agent = BlockingAgent()
        manager = AuditManager(agent, project_result)
        first = manager.submit('q1')
        with self.assertRaises(Busy) as ctx:
            manager.submit('q2')
        self.assertEqual(ctx.exception.active_id, first.id)
        await agent.started.wait()
        self.assertEqual(manager.get(first.id).status, 'running')
        agent.release.set()
        done = await settle(manager, first.id)
        self.assertEqual(done.status, 'completed')
        second = manager.submit('q3')  # slot is free again
        self.assertNotEqual(first.id, second.id)
        await manager.shutdown()

    async def test_many_simultaneous_submits_admit_exactly_one(self):
        agent = BlockingAgent()
        manager = AuditManager(agent, project_result)

        async def attempt(i):
            await asyncio.sleep(0)
            try:
                return manager.submit(f'q{i}')
            except Busy:
                return None
        results = await asyncio.gather(*(attempt(i) for i in range(25)))
        self.assertEqual(sum(r is not None for r in results), 1)
        await manager.shutdown()
        self.assertEqual(agent.runs <= 1, True)

    async def test_pending_then_running_then_completed_are_real_transitions(self):
        agent = BlockingAgent()
        manager = AuditManager(agent, project_result)
        job = manager.submit('q')
        self.assertEqual((job.status, manager.elapsed(job)), ('pending', 0.0))  # nothing has run yet
        await agent.started.wait()
        self.assertEqual(job.status, 'running')
        agent.release.set()
        await settle(manager, job.id)
        self.assertEqual(job.status, 'completed')
        self.assertIsNotNone(job.result_bytes)
        self.assertIsNone(manager.active_id)


class Cancellation(unittest.IsolatedAsyncioTestCase):
    async def test_slot_is_held_until_cancel_cleanup_completes(self):
        gate = asyncio.Event()
        agent = BlockingAgent(cleanup_gate=gate)
        manager = AuditManager(agent, project_result)
        job = manager.submit('q')
        await agent.started.wait()
        manager.cancel(job.id)
        for _ in range(20):
            await asyncio.sleep(0.005)
        self.assertEqual((job.status, job.cancel_requested, agent.cleaned), ('running', True, False))  # not cancelled until cleanup ends
        with self.assertRaises(Busy):
            manager.submit('too soon')
        gate.set()
        await settle(manager, job.id)
        self.assertEqual((job.status, agent.cleaned, manager.active_id), ('cancelled', True, None))
        self.assertIsNone(job.result_bytes)
        manager.submit('now allowed')
        await manager.shutdown()

    async def test_cancel_before_agent_starts_never_runs_agent(self):
        agent = BlockingAgent()
        manager = AuditManager(agent, project_result)
        job = manager.submit('q')
        manager.cancel(job.id)  # supervisor has not taken its first step
        await settle(manager, job.id)
        self.assertEqual((job.status, agent.runs), ('cancelled', 0))

    async def test_cancel_finished_or_unknown_is_a_noop(self):
        manager = AuditManager(InstantAgent(), project_result)
        job = manager.submit('q')
        await settle(manager, job.id)
        self.assertEqual(manager.cancel(job.id).status, 'completed')
        self.assertIsNone(manager.cancel('00000000-0000-4000-8000-000000000000'))

    async def test_job_timeout_fails_and_waits_for_cleanup(self):
        agent = BlockingAgent()
        manager = AuditManager(agent, project_result)
        manager.job_timeout = 0.05
        job = manager.submit('q')
        await settle(manager, job.id)
        self.assertEqual((job.status, job.error['code'], agent.cleaned, job.result_bytes), ('failed', 'job_timeout', True, None))


class Shutdown(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_cancels_and_awaits_agent_then_rejects_new_work(self):
        agent = BlockingAgent()
        manager = AuditManager(agent, project_result)
        job = manager.submit('q')
        await agent.started.wait()
        await manager.shutdown()
        self.assertTrue(agent.cleaned)
        self.assertEqual((job.status, manager.active_id), ('cancelled', None))
        with self.assertRaises(Closed):
            manager.submit('after shutdown')

    async def test_shutdown_before_supervisor_first_step_releases_slot(self):
        manager = AuditManager(BlockingAgent(), project_result)
        job = manager.submit('q')
        await manager.shutdown()
        self.assertEqual((job.status, manager.active_id), ('cancelled', None))


class ShutdownBarrier(unittest.IsolatedAsyncioTestCase):
    async def test_warning_deadline_never_abandons_cleanup(self):
        gate = asyncio.Event()  # cleanup barrier: stands in for a Claude process group that is slow to die
        agent = BlockingAgent(cleanup_gate=gate)
        manager = AuditManager(agent, project_result, shutdown_warning=0.02)
        job = manager.submit('q')
        await agent.started.wait()
        resources_closed = []
        shutdown = asyncio.ensure_future(manager.shutdown())
        with self.assertLogs('audit_web', level='WARNING') as logs:
            await asyncio.sleep(0.15)  # several warning deadlines pass
        self.assertTrue(logs.output)
        self.assertFalse(shutdown.done())  # still waiting: the caller must not close backend/registry yet
        self.assertEqual((agent.cleaned, manager.active_id, job.status), (False, job.id, 'running'))
        with self.assertRaises(Closed):  # new audits are refused while draining
            manager.submit('during shutdown')
        gate.set()
        await shutdown
        resources_closed.append(True)
        self.assertEqual((agent.cleaned, manager.active_id, job.status), (True, None, 'cancelled'))
        self.assertEqual(resources_closed, [True])

    async def test_concurrent_shutdown_calls_both_wait_for_real_cleanup(self):
        gate = asyncio.Event()
        agent = BlockingAgent(cleanup_gate=gate)
        manager = AuditManager(agent, project_result, shutdown_warning=0.01)
        manager.submit('q')
        await agent.started.wait()
        first, second = asyncio.ensure_future(manager.shutdown()), asyncio.ensure_future(manager.shutdown())
        await asyncio.sleep(0.05)
        self.assertEqual((first.done(), second.done()), (False, False))
        gate.set()
        await asyncio.gather(first, second)
        self.assertTrue(agent.cleaned)


class History(unittest.IsolatedAsyncioTestCase):
    async def test_job_finished_at_monotonic_zero_is_still_pruned_by_ttl(self):
        clock = Clock()
        clock.now = 0.0
        manager = AuditManager(InstantAgent(), project_result, clock=clock)
        job = manager.submit('q')
        await settle(manager, job.id)
        self.assertEqual(job.finished, 0.0)  # a legitimate falsy timestamp
        clock.now = limits.HISTORY_TTL_SECONDS + 1
        self.assertIsNone(manager.get(job.id))

    async def test_only_eight_finished_jobs_and_oldest_expire(self):
        manager = AuditManager(InstantAgent(), project_result, clock=Clock())
        ids = []
        for i in range(11):
            job = manager.submit(f'q{i}')
            await settle(manager, job.id)
            ids.append(job.id)
        listed = [j.id for j in manager.list()]
        self.assertEqual(len(listed), 8)
        self.assertEqual(listed, ids[::-1][:8])  # newest first
        self.assertIsNone(manager.get(ids[0]))
        self.assertIsNotNone(manager.get(ids[-1]))

    async def test_ttl_expires_finished_jobs_but_not_the_active_one(self):
        clock = Clock()
        manager = AuditManager(InstantAgent(), project_result, clock=clock)
        job = manager.submit('q')
        await settle(manager, job.id)
        clock.now += limits.HISTORY_TTL_SECONDS - 1
        self.assertIsNotNone(manager.get(job.id))
        clock.now += 2
        self.assertIsNone(manager.get(job.id))
        agent = BlockingAgent()
        manager2 = AuditManager(agent, project_result, clock=clock)
        active = manager2.submit('q')
        await agent.started.wait()
        clock.now += 10 * limits.HISTORY_TTL_SECONDS
        self.assertIsNotNone(manager2.get(active.id))
        await manager2.shutdown()

    async def test_retained_result_memory_is_bounded(self):
        big = b'{"x":"' + b'a' * 4000 + b'"}'
        manager = AuditManager(InstantAgent(), lambda result: big, max_result_bytes=5000, max_history=8)
        for i in range(20):
            await settle(manager, manager.submit(f'q{i}').id)
        self.assertLessEqual(manager.retained_bytes(), 8 * 5000)
        self.assertEqual(len(manager.list()), 8)

    async def test_oversized_result_fails_explicitly_without_partial_result(self):
        manager = AuditManager(InstantAgent(), lambda result: b'x' * 5000, max_result_bytes=2048)
        job = manager.submit('q')
        await settle(manager, job.id)
        self.assertEqual((job.status, job.error['code'], job.result_bytes), ('failed', 'result_too_large', None))

    async def test_failed_jobs_have_error_and_no_result(self):
        manager = AuditManager(InstantAgent(ValueError('boom')), project_result)
        job = manager.submit('q')
        await settle(manager, job.id)
        self.assertEqual((job.status, job.result_bytes), ('failed', None))
        self.assertEqual(set(job.error), {'code', 'message', 'hint'})


class Errors(unittest.TestCase):
    SECRET = '/home/someone/.config/claude/token sk-ant-SECRET'

    def test_each_failure_class_maps_to_a_sanitized_actionable_error(self):
        cases = {
            'model_timeout': asyncio.TimeoutError(self.SECRET), 'model_unavailable': RuntimeError('Claude generation failed, exit=1 ' + self.SECRET),
            'retrieval_unavailable': httpx.ConnectError(self.SECRET), 'retrieval_timeout': httpx.ReadTimeout(self.SECRET),
            'invalid_model_output': ValidationError.from_exception_data('Critique', [dict(type='missing', loc=('score',), input={})]),
            'evidence_rejected': ValueError(self.SECRET), 'internal_error': KeyError(self.SECRET), 'result_rejected': ProjectionError(self.SECRET),
        }
        for expected, exc in cases.items():
            info = classify(exc)
            self.assertEqual(info['code'], expected, exc)
            self.assertTrue(info['message'] and info['hint'])
            blob = ' '.join(info.values())
            self.assertNotIn('SECRET', blob)
            self.assertNotIn('/home', blob)
            self.assertNotIn('Traceback', blob)

    def test_claude_executable_missing_is_actionable(self):
        self.assertEqual(classify(FileNotFoundError(2, 'No such file', 'claude'))['code'], 'model_unavailable')


class LiveManagerErrors(unittest.IsolatedAsyncioTestCase):
    async def test_agent_exceptions_never_leak_into_job_error(self):
        for exc in (asyncio.TimeoutError(), httpx.ConnectError('http://127.0.0.1:1 /secret/path'), RuntimeError('Claude generation failed, exit=9 /tmp/x'), ValueError('/etc/passwd')):
            manager = AuditManager(InstantAgent(exc), project_result)
            job = manager.submit('q')
            await settle(manager, job.id)
            self.assertEqual(job.status, 'failed')
            self.assertNotRegex(' '.join(job.error.values()), r'/etc|/tmp|secret|127\.0\.0\.1')
            self.assertIsNone(manager.active_id)


if __name__ == '__main__':
    unittest.main()
