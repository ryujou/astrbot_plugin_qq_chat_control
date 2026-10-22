import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from astrbot_plugin_qq_chat_control import main as control, profiles as profiles_module
from astrbot.core.provider.entities import LLMResponse
from astrbot.core.star.filter.command import CommandFilter, GreedyStr


def assessment(delta=2, quote='我喜欢游戏', confidence=.99, target='bot', profile='喜欢游戏，偏好简短聊天'):
    return json.dumps(dict(profile=profile, affinity_delta=delta, target=target, confidence=confidence,
                          evidence=[dict(index=0, quote=quote)]), ensure_ascii=False)


class Event:
    def __init__(self, text='我喜欢玩游戏', sender='member', group='group', platform='qq_private', post_type='message'):
        self.sender, self.group, self.platform, self.text = sender, group, platform, text
        self.parts = [control.Plain(text)]
        self.message_obj = SimpleNamespace(raw_message={'post_type': post_type})
        self.unified_msg_origin = f'{platform}:GroupMessage:{sender}_{group}'
        self._extras = {}
        self.is_at_or_wake_command = True
        self.sent = []

    def get_platform_name(self): return 'aiocqhttp'
    def get_platform_id(self): return self.platform
    def get_group_id(self): return self.group
    def get_sender_id(self): return self.sender
    def get_sender_name(self): return '阿明'
    def get_self_id(self): return 'bot'
    def get_messages(self): return self.parts
    def get_extra(self, key, default=None): return self._extras.get(key, default)
    def set_extra(self, key, value): self._extras[key] = value
    def request_llm(self, **kwargs): return SimpleNamespace(func_tool=None, system_prompt='', **kwargs)
    def plain_result(self, text): return SimpleNamespace(use_t2i=lambda enabled: text)
    def should_call_llm(self, disabled): self.disabled = disabled
    def stop_event(self): self.stopped = True
    async def send(self, text): self.sent.append(text)


class MemberProfileTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.controller = object.__new__(control.QQChatControl)
        self.controller.config = {'allowed_group_ids': ['group', 'other', 'work'], 'allowed_qq_ids': ['white'],
            'default_provider_id': 'group-model'}
        self.controller.tasks = set()
        self.controller.rules = {(platform, group): {'member_profiles': True, 'provider_id': 'group-model'}
            for platform, group in [('qq_private', 'group'), ('qq_private', 'other'), ('second', 'group')]}
        self.controller.rules[('qq_private', 'work')] = {'member_profiles': False}
        self.controller.context = SimpleNamespace(get_config=lambda origin: {'wake_prefix': ['/']})
        self.controller.profiles = control.MemberProfiles(self.controller, Path(self.directory.name) / 'members.json')
        self.controller.limits = control.LLMLimits(self.controller, Path(self.directory.name) / 'limits.json')
        self.recheck = AsyncMock(return_value=assessment(delta=0))
        self.controller.learning = SimpleNamespace(prepare=Mock(), generated=Mock(), record_reply_call=Mock(),
            slang=SimpleNamespace(inject=lambda event, kwargs: kwargs),
            policy=SimpleNamespace(prepare=AsyncMock(return_value=False), judge=self.recheck,
                                   inject=lambda event, kwargs: kwargs, deliver=AsyncMock()))
        self.chat = AsyncMock(return_value=LLMResponse(role='assistant',
            completion_text=assessment()))
        self.provider = SimpleNamespace(provider_config={'type': 'local_antigravity_cli'},
            bind_origin=Mock(), text_chat=self.chat, text_chat_stream=None)
        self.controller.provider = Mock(return_value=self.provider)

    async def asyncTearDown(self):
        tasks = list(self.controller.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.directory.cleanup()

    def observe(self, text='喜欢游戏', **kwargs):
        event = Event(text=text, **kwargs)
        self.controller.profiles.observe(event)
        return event

    def batch(self, replies=True, **kwargs):
        for number in range(20):
            event = self.observe(f'我喜欢游戏，今天聊到第{number}个话题', **kwargs)
            if replies:
                self.controller.profiles.replied(event, LLMResponse(role='assistant', completion_text='聊得挺开心'))
        return event

    async def update(self, now=10000):
        with patch.object(profiles_module, 'time', return_value=now):
            await self.controller.profiles.update_due()

    async def test_collection_excludes_self_notices_commands_and_empty(self):
        for event in [Event(sender='bot'), Event(group=''), Event(group='forbidden'), Event(group='work'),
                      Event(post_type='notice'), Event(text='/画像 更正 内容'), Event(text=''), Event(text='哈')]:
            self.controller.profiles.observe(event)
        self.assertEqual(self.controller.profiles.records, {})
        event = self.observe()
        self.observe()
        record = self.controller.profiles.records[self.controller.profiles.key(event)]
        self.assertEqual(len(record['samples']), 1)
        self.assertEqual(record['affinity'], 50)

    async def test_group_member_platform_isolation_and_restart(self):
        for kwargs in ({}, {'sender': 'second'}, {'group': 'other'}, {'platform': 'second'}):
            self.observe(**kwargs)
        self.assertEqual(len(self.controller.profiles.records), 4)
        restored = control.MemberProfiles(self.controller, self.controller.profiles.path)
        self.assertEqual(restored.records, self.controller.profiles.records)
        self.assertEqual(restored.events, {})

    async def test_twenty_messages_and_hourly_rate_limit(self):
        event = self.batch(replies=False)
        self.controller.profiles.records[self.controller.profiles.key(event)]['samples'].pop()
        await self.update()
        self.chat.assert_not_awaited()
        self.observe('最后一个新话题')
        await self.update()
        record = self.controller.profiles.records[self.controller.profiles.key(event)]
        self.assertEqual(record['affinity'], 50)
        self.assertEqual(record['profile'], '喜欢游戏，偏好简短聊天')
        self.batch(replies=False)
        await self.update(11000)
        self.assertEqual(self.chat.await_count, 1)
        await self.update(13600)
        self.assertEqual(self.chat.await_count, 2)

    async def test_passive_messages_update_profile_not_affinity(self):
        event = self.batch(replies=False)
        await self.update()
        record = self.controller.profiles.records[self.controller.profiles.key(event)]
        self.assertTrue(record['profile'])
        self.assertEqual(record['affinity'], 50)

    async def test_delta_and_score_are_clamped(self):
        event = self.batch()
        self.chat.return_value.completion_text = assessment(delta=999)
        record = self.controller.profiles.records[self.controller.profiles.key(event)]
        record['affinity'] = 99
        await self.update()
        self.assertEqual(record['affinity'], 100)
        self.batch()
        self.chat.return_value.completion_text = assessment(delta=-999)
        record['affinity'] = 1
        await self.update(13600)
        self.assertEqual(record['affinity'], 0)

    async def test_direct_insult_without_bot_reply_updates_with_one_message(self):
        event = Event(text='你这个机器人是个傻逼')
        event.parts.insert(0, control.At(qq='bot'))
        self.controller.profiles.observe(event)
        self.chat.return_value.completion_text = assessment(delta=-10, quote=event.text)
        await self.update()
        record = self.controller.profiles.records[self.controller.profiles.key(event)]
        self.assertEqual(record['affinity'], 40)
        self.assertEqual(record['samples'], [])
        self.recheck.assert_not_awaited()
        await self.update(10060)
        self.assertEqual(self.chat.await_count, 1)

    async def test_direct_interactions_use_minute_interval_and_batch_delta(self):
        for number, now in enumerate([10000, 10030, 10060]):
            event = Event(text='你这个蠢货' + str(number))
            event.parts.insert(0, control.At(qq='bot'))
            self.controller.profiles.observe(event)
            self.chat.return_value.completion_text = assessment(delta=-15, quote='你这个蠢货')
            await self.update(now)
        record = self.controller.profiles.records[self.controller.profiles.key(event)]
        self.assertEqual(self.chat.await_count, 2)
        self.assertEqual(record['affinity'], 20)

    async def test_reply_target_and_context_are_preserved(self):
        event = Event(text='你把结果算错了')
        event.parts.insert(0, control.Reply(id='demo', sender_id='bot', chain=[control.Plain('答案是9')]))
        self.controller.profiles.observe(event)
        sample = self.controller.profiles.records[self.controller.profiles.key(event)]['samples'][0]
        self.assertTrue(sample['direct'])
        self.assertEqual(sample['quoted'], ['答案是9'])
        self.chat.return_value.completion_text = assessment(delta=0, quote=event.text)
        await self.update()
        self.assertEqual(self.controller.profiles.records[self.controller.profiles.key(event)]['affinity'], 50)

    async def test_uncertain_or_fabricated_evidence_requires_one_recheck(self):
        for first in [assessment(-10, confidence=.6), assessment(-10, quote='捏造的原话')]:
            self.controller.profiles.records.clear()
            event = self.batch()
            self.chat.return_value.completion_text = first
            self.recheck.return_value = assessment(delta=0, confidence=.99)
            await self.update()
            self.assertEqual(self.controller.profiles.records[self.controller.profiles.key(event)]['affinity'], 50)
        self.assertEqual(self.recheck.await_count, 2)
        self.assertTrue(self.recheck.call_args.kwargs['recheck'])

    async def test_failed_or_still_unknown_recheck_keeps_score(self):
        for failed in [False, True]:
            self.controller.profiles.records.clear()
            event = self.batch()
            self.chat.return_value.completion_text = assessment(delta=-10, confidence=.6)
            self.recheck.side_effect = RuntimeError('unavailable') if failed else None
            self.recheck.return_value = assessment(delta=-15, confidence=.7)
            with patch.object(profiles_module.logger, 'exception'):
                await self.update()
            self.assertEqual(self.controller.profiles.records[self.controller.profiles.key(event)]['affinity'], 50)

    async def test_recent_direct_message_survives_full_sample_buffer(self):
        for number in range(40):
            self.observe('普通话题' + str(number))
        event = Event(text='谢谢你认真帮我排查')
        event.parts.insert(0, control.At(qq='bot'))
        self.controller.profiles.observe(event)
        record = self.controller.profiles.records[self.controller.profiles.key(event)]
        self.assertEqual(len(record['samples']), 40)
        self.assertTrue(record['samples'][-1]['direct'])

    async def test_direct_interaction_precedes_passive_profile_backlog(self):
        for number in range(4):
            self.batch(replies=False, sender='passive' + str(number))
        event = Event(text='谢谢你的帮助', sender='active')
        event.parts.insert(0, control.At(qq='bot'))
        self.controller.profiles.observe(event)
        self.chat.return_value.completion_text = assessment(delta=5, quote=event.text)
        await self.update()
        self.assertEqual(self.controller.profiles.records[self.controller.profiles.key(event)]['affinity'], 55)
        self.assertEqual(self.chat.await_count, 4)

    async def test_view_correct_delete_only_self_and_no_llm(self):
        event = self.observe()
        other = self.observe(sender='other')
        self.assertIn('50/100', self.controller.profiles.command(event, ''))
        self.controller.profiles.command(event, '更正 叫我阿明，喜欢游戏')
        self.assertIn('叫我阿明', self.controller.profiles.command(event, ''))
        self.assertNotIn('叫我阿明', self.controller.profiles.command(other, ''))
        self.assertIn('用法', self.controller.profiles.command(event, '修改好感 100'))
        await self.controller.member_affinity(event)
        await self.controller.member_profile(event, '')
        self.assertTrue(event.disabled)
        self.assertEqual(self.controller.limits.records, {})
        self.controller.profiles.command(event, '删除')
        self.assertNotIn(self.controller.profiles.key(event), self.controller.profiles.records)
        self.assertIn(self.controller.profiles.key(other), self.controller.profiles.records)
        self.chat.assert_not_awaited()

    async def test_native_command_parser_accepts_empty_and_multiword(self):
        command = object.__new__(CommandFilter)
        self.assertEqual(command.validate_and_convert_params([], {'action': GreedyStr}), {'action': ''})
        self.assertEqual(command.validate_and_convert_params(['更正', '叫我阿明', '喜欢游戏'], {'action': GreedyStr}),
            {'action': '更正 叫我阿明 喜欢游戏'})

    async def test_bounded_samples_and_message_lengths(self):
        event = None
        for number in range(60):
            event = self.observe(str(number) + '很长' * 400)
        record = self.controller.profiles.records[self.controller.profiles.key(event)]
        self.assertEqual(len(record['samples']), 40)
        self.assertTrue(all(len(sample['text']) <= 300 for sample in record['samples']))

    async def test_analysis_uses_group_model_readonly_and_no_quota(self):
        event = self.batch()
        self.provider.provider_config['type'] = 'local_codex_app_server'
        self.provider.bind_request = Mock()
        token = control.current_message.set(SimpleNamespace(event=event))
        try:
            async def inspect(**kwargs):
                self.assertIsNone(control.current_message.get())
                return LLMResponse(role='assistant', completion_text=assessment(delta=0))
            self.chat.side_effect = inspect
            await self.update()
            self.assertIsNotNone(control.current_message.get())
        finally:
            control.current_message.reset(token)
        binding = self.provider.bind_request.call_args.kwargs
        self.assertTrue(binding['automatic'])
        self.assertEqual(binding['tool_specs'], [])
        self.assertIsNone(binding['execute_tool'])
        self.controller.provider.assert_called_with('group-model')
        self.assertEqual(self.controller.limits.records, {})
        self.assertEqual(event.sent, [])
        self.assertFalse(event.get_extra('qq_chat_automatic', False))

    async def test_invalid_failed_and_empty_analysis_preserve_previous(self):
        for response in ('not json', '[]', '{"profile":55,"affinity_delta":true}', ''):
            event = self.batch()
            record = self.controller.profiles.records[self.controller.profiles.key(event)]
            record['last_update'] = 0
            record['profile'] = '原画像'
            self.chat.return_value.completion_text = response
            with patch.object(profiles_module.logger, 'exception') as logged:
                await self.update()
                logged.assert_called_once()
            self.assertEqual((record['profile'], record['affinity']), ('原画像', 50))
            self.assertTrue(record['samples'])

    async def test_new_messages_during_analysis_remain_pending(self):
        event = self.batch()
        async def generate(**kwargs):
            self.observe('这条是分析期间的新消息')
            return LLMResponse(role='assistant', completion_text='{"profile":"爱游戏", "affinity_delta":0}')
        self.chat.side_effect = generate
        await self.update()
        record = self.controller.profiles.records[self.controller.profiles.key(event)]
        self.assertEqual(record['samples'], [{'text': '这条是分析期间的新消息', 'direct': False}])

    async def test_analysis_runs_once_without_blocking_maintenance(self):
        self.batch()
        entered = asyncio.Event()
        async def wait(**kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.chat.side_effect = wait
        self.controller.profiles.schedule_update()
        await entered.wait()
        task = self.controller.profiles.task
        self.controller.profiles.schedule_update()
        self.assertIs(self.controller.profiles.task, task)
        self.assertIn(task, self.controller.tasks)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(task.cancelled())

    async def test_delete_or_correct_during_analysis_is_not_overwritten(self):
        for action in ('删除', '更正 本人最新更正'):
            event = self.batch()
            async def generate(**kwargs):
                self.controller.profiles.command(event, action)
                return LLMResponse(role='assistant', completion_text='{"profile":"过时数据", "affinity_delta":2}')
            self.chat.side_effect = generate
            await self.update(20000)
            self.assertNotIn('过时数据', self.controller.profiles.command(event, ''))

    async def test_context_is_scoped_and_idle_has_no_personal_score(self):
        event = self.observe()
        other = self.observe(group='other')
        self.controller.profiles.command(event, '更正 喜欢游戏')
        self.controller.profiles.command(other, '更正 只在另一个群说的偏好')
        context = self.controller.profiles.context(event)
        self.assertIn('喜欢游戏', context)
        self.assertNotIn('另一个群', context)
        group_context = self.controller.profiles.context(event, group=True)
        self.assertIn('没有指定互动对象', group_context)
        self.assertNotIn('好感', group_context)

    async def test_normal_direct_automatic_and_nested_stream_inject_once(self):
        event = self.observe()
        self.controller.profiles.command(event, '更正 喜欢游戏')
        event = self.observe('这是一条新的互动')
        async def chat(**kwargs):
            self.assertEqual(kwargs['system_prompt'].count('当前发言人的群内资料'), 1)
            return LLMResponse(role='assistant', completion_text='正常回复')
        async def stream(**kwargs):
            yield LLMResponse(role='assistant', completion_text='片段', is_chunk=True)
            yield await self.provider.text_chat(**kwargs)
        self.provider.text_chat, self.provider.text_chat_stream = chat, stream
        self.controller.limits.wrap_provider(self.provider)
        for automatic in (False, True):
            event.set_extra('qq_chat_automatic', automatic)
            usage = SimpleNamespace(event=event, ticket=None, counted=False, active=0, notified=False)
            token = control.current_message.set(usage)
            try:
                await self.provider.text_chat(prompt='问题', system_prompt='原人设')
                async for response in self.provider.text_chat_stream(prompt='问题', system_prompt='原人设'):
                    pass
            finally:
                control.current_message.reset(token)
            self.assertEqual(usage.active, 0)
        self.assertEqual(self.controller.limits.records['qq_private:group:member']['used'], 1)
        self.assertEqual(event.get_extra('qq_member_profile_sample')[1]['reply'], '正常回复')


if __name__ == '__main__':
    unittest.main()
