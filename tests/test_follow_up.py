import asyncio
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.astr_agent_run_util import run_agent
from astrbot.core.message.components import At, Plain, Image
from astrbot.core.pipeline.process_stage.follow_up import register_active_runner, unregister_active_runner
from astrbot.core.provider.entities import LLMResponse, ProviderRequest

from astrbot_plugin_qq_chat_control import follow_up, main
from astrbot_plugin_qq_chat_control.work_progress import WorkProgress


class Event:
    unified_msg_origin = 'test:GroupMessage:20001_10003'

    def __init__(self, text='改成只给官方网站地址', sender='20001'):
        self.sender = sender
        self.parts = [At(qq='90001'), Plain(text)]
        self.extras = {}
        self.sent = []
        self.stopped = False
        self.trace = NS(record=Mock())
        self.is_at_or_wake_command = True

    def get_group_id(self): return '10003'
    def get_sender_id(self): return self.sender
    def get_self_id(self): return '90001'
    def get_messages(self): return self.parts
    def get_extra(self, key, default=None): return self.extras.get(key, default)
    def set_extra(self, key, value): self.extras[key] = value
    def get_platform_name(self): return 'aiocqhttp'
    def is_stopped(self): return self.stopped
    def stop_event(self): self.stopped = True
    def should_call_llm(self, disabled): self.disabled = disabled
    def plain_result(self, text): return NS(use_t2i=lambda enabled: text)
    async def send(self, result): self.sent.append(result)


class FollowUpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.provider = NS(steer=AsyncMock(return_value=True), provider_config={})
        self.owner = NS(chat_allowed=lambda event: True, limits=NS(is_limited=lambda event: False))
        self.original = Event('原任务')
        self.progress = WorkProgress(NS(tasks=set()), self.original, '详细', 30)
        self.progress.commentary = '还在执行原任务'
        self.original.set_extra('qq_work_progress', self.progress)
        self.runner = NS(done=lambda: False, provider=self.provider, request_stop=Mock(),
                         run_context=NS(context=NS(event=self.original), messages=[]))
        register_active_runner(self.original.unified_msg_origin, self.runner)
        self.addCleanup(unregister_active_runner, self.original.unified_msg_origin, self.runner)

    async def test_steer_immediate_and_keep_history(self):
        event = Event()
        self.assertTrue(await follow_up.steer_task(self.owner, event, self.provider))
        self.provider.steer.assert_awaited_once_with(event.unified_msg_origin, '改成只给官方网站地址', image_urls=[])
        self.assertEqual(self.runner.run_context.messages[-1].content[0].text, '改成只给官方网站地址')
        self.assertEqual(self.progress.commentary, '')
        self.assertIn('调整', self.progress.stage)
        self.assertIn('已送入', event.sent[0])

    async def test_stop_cancels_original_without_model_round_trip(self):
        event = Event('停止不查了')
        self.assertTrue(await follow_up.steer_task(self.owner, event, self.provider))
        self.runner.request_stop.assert_called_once()
        self.assertTrue(self.original.get_extra('agent_stop_requested'))
        self.assertTrue(self.progress.cancelled)
        self.provider.steer.assert_not_awaited()

    async def test_compound_instruction_is_steered_not_discarded(self):
        event = Event('停止搜索，改成总结已有结果')
        self.assertTrue(await follow_up.steer_task(self.owner, event, self.provider))
        self.provider.steer.assert_awaited_once()
        self.runner.request_stop.assert_not_called()

    async def test_other_sender_or_missing_mention_cannot_steer(self):
        other = Event(sender='20002')
        unmentioned = Event()
        unmentioned.parts = [Plain('停止不查了')]
        for event in [other, unmentioned]:
            self.assertFalse(await follow_up.steer_task(self.owner, event, self.provider))
        self.provider.steer.assert_not_awaited()
        self.runner.request_stop.assert_not_called()

    async def test_permission_and_automatic_boundaries(self):
        event = Event()
        for attr, value in [('chat_allowed', lambda event: False),
                            ('limits', NS(is_limited=lambda event: True))]:
            with patch.object(self.owner, attr, value):
                self.assertFalse(await follow_up.steer_task(self.owner, event, self.provider))
        event.set_extra('qq_chat_automatic', True)
        self.assertFalse(await follow_up.steer_task(self.owner, event, self.provider))
        self.provider.steer.assert_not_awaited()

    async def test_idle_finished_or_different_provider_uses_normal_flow(self):
        for attr, value in [('done', lambda: True), ('provider', NS())]:
            with patch.object(self.runner, attr, value):
                self.assertFalse(await follow_up.steer_task(self.owner, Event(), self.provider))
        self.provider.steer.return_value = False
        event = Event()
        self.assertFalse(await follow_up.steer_task(self.owner, event, self.provider))
        self.assertEqual(event.sent, [])
        self.assertEqual(self.runner.run_context.messages, [])
        self.assertFalse(await follow_up.steer_task(self.owner, event, NS()))

    async def test_rpc_failure_has_no_success_or_history_claim(self):
        self.provider.steer.side_effect = RuntimeError('connection lost')
        event = Event()
        with patch.object(follow_up.logger, 'exception') as log:
            self.assertTrue(await follow_up.steer_task(self.owner, event, self.provider))
        log.assert_called_once()
        self.assertEqual(self.runner.run_context.messages, [])
        self.assertIn('未确认', event.sent[0])

    async def test_follow_up_image_reaches_provider_and_history(self):
        event = Event('看这张图')
        event.parts.append(Image(file='https://example.com/image.png', url='https://example.com/image.png'))
        self.assertTrue(await follow_up.steer_task(self.owner, event, self.provider))
        self.assertEqual(self.provider.steer.call_args.kwargs['image_urls'], ['https://example.com/image.png'])
        self.assertEqual(self.runner.run_context.messages[-1].content[-1].image_url.url,
                         'https://example.com/image.png')

    async def test_prepare_consumes_follow_up_before_session_binding(self):
        owner = NS(**vars(self.owner), ensure_ready=AsyncMock(), provider_id=lambda event: 'test',
                   provider=lambda provider_id: self.provider, bindings=NS(bind=AsyncMock()))
        event = Event()
        await main.QQChatControl.prepare(owner, event)
        self.assertTrue(event.stopped)
        self.assertTrue(event.disabled)
        owner.bindings.bind.assert_not_awaited()

    async def test_native_runner_stop_cancels_active_stream(self):
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def stream(**kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
                yield LLMResponse(role='assistant', completion_text='unexpected', is_chunk=True)
            finally:
                cancelled.set()

        self.provider.text_chat_stream = stream
        runner = ToolLoopAgentRunner()
        await runner.reset(self.provider, ProviderRequest(prompt='原任务'),
                           ContextWrapper(context=NS(event=self.original)), NS(), BaseAgentRunHooks(), streaming=True)
        register_active_runner(self.original.unified_msg_origin, runner)
        self.addCleanup(unregister_active_runner, self.original.unified_msg_origin, runner)

        async def consume():
            return [chain async for chain in run_agent(runner, max_step=1, show_tool_use=False)]

        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(started.wait(), 2)
            self.assertTrue(await follow_up.steer_task(self.owner, Event('停止不查了'), self.provider))
            await asyncio.wait_for(task, 2)
            self.assertTrue(cancelled.is_set())
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()
