import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from astrbot_plugin_qq_chat_control.learning import FeedbackLearning
from astrbot_plugin_qq_chat_control.limits import current_message
from astrbot_plugin_qq_chat_control.slang import term_matches
from test_feedback_learning import Event


class SlangTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.provider = NS(text_chat=AsyncMock(), bind_origin=lambda _: None)
        self.rule = dict(persona_id='演示人设', feedback_observe=False, slang_learning=True)
        self.rules = {('qq_private', '10006'): self.rule,
                      ('qq_private', '10003'): dict(self.rule, persona_id='demo_persona')}
        self.owner = NS(closing=False, rules=self.rules,
            config=dict(allowed_group_ids=['10006', '10003'], default_provider_id='gemini'),
            context=NS(get_config=lambda _: {'wake_prefix': ['/']}),
            rule=lambda event: self.rules.get((event.get_platform_id(), event.get_group_id()), {}),
            provider=lambda _: self.provider, limits=NS(successful=lambda response: bool(response.completion_text)))
        self.path = Path(self.tmp.name) / 'learning.db'
        self.learning = FeedbackLearning(self.owner, self.path)
        self.slang = self.learning.slang
        self.addCleanup(self.learning.close)

    def seed(self, start=1000, group='10006', same_person=False):
        messages = ['这个机器人又死了，刚才还能回复', '又死了，bot离线了吗',
                    '今天我想听首歌', '等会去吃饭', '有朋友在外面玩', '快到下班时间了', '刚刚点了个外卖', '晚点再过来看']
        for i, text in enumerate(messages):
            event = Event(str(start + i), text=text, sender='alice' if same_person or i % 2 == 0 else 'bob',
                          group=group, ts=start + i)
            with patch('astrbot_plugin_qq_chat_control.learning.time.time', return_value=start + i):
                self.learning.observe(event)
        return self.slang.batch('qq_private', group, now=start + 10)

    def data(self, batch, sense_id=None, meaning='机器人掉线或停止回应', ids=None):
        return {'terms': [dict(term='死了', senses=[dict(sense_id=sense_id, meaning=meaning,
            scene='讨论bot是否在线、能否回复时的说法，不能解释为真人死亡', clues=['机器人', '掉线', 'bot'],
            confidence=.92, evidence_ids=ids or [batch['messages'][0]['message_id'], batch['messages'][1]['message_id']])])]}

    def apply(self, batch, data=None):
        with patch('astrbot_plugin_qq_chat_control.slang.time.time', return_value=batch['cutoff'] + 1):
            self.slang.apply(batch, self.data(batch) if data is None else data)

    def confirmed(self, group='10006'):
        first = self.seed(group=group)
        self.apply(first)
        second = self.seed(start=1310, group=group)
        self.apply(second, self.data(second, sense_id=1))
        return second

    def inject(self, event, kwargs):
        token = current_message.set(NS(event=event))
        try:
            return self.slang.inject(event, kwargs)
        finally:
            current_message.reset(token)

    async def test_learns_even_when_feedback_observation_off(self):
        batch = self.seed()
        self.assertEqual(len(batch['messages']), 8)
        self.assertEqual(self.learning.db.execute('SELECT COUNT(*) FROM replies').fetchone()[0], 0)

    async def test_multiple_rounds_and_people_required_before_use(self):
        first = self.seed()
        self.apply(first)
        self.assertEqual(self.slang.relevant('qq_private', '10006', '机器人又死了'), [])
        second = self.seed(start=1310)
        self.assertEqual(second['known'][0]['senses'][0]['sense_id'], 1)
        self.apply(second, self.data(second, sense_id=1))
        learned = self.slang.relevant('qq_private', '10006', '机器人又死了')
        self.assertIn('掉线', learned[0]['senses'][0]['meaning'])
        self.assertNotIn('sender', json.dumps(learned))

    async def test_same_person_repetition_cannot_establish_group_meaning(self):
        first = self.seed(same_person=True)
        self.apply(first)
        second = self.seed(start=1310, same_person=True)
        self.apply(second, self.data(second, sense_id=1))
        self.assertEqual(self.slang.relevant('qq_private', '10006', '机器人死了'), [])

    async def test_made_up_evidence_and_literal_substring_do_not_create_terms(self):
        batch = self.seed()
        self.apply(batch, self.data(batch, ids=['invented', 'another']))
        self.assertEqual(self.slang.entries('qq_private', '10006'), [])
        self.assertFalse(term_matches('PHP', 'This is PHPMailer'))
        self.assertTrue(term_matches('PHP', '还敢诋毁php吗'))

    async def test_one_word_keeps_separate_contextual_meanings(self):
        self.confirmed()
        third = self.seed(start=1620)
        self.apply(third, self.data(third, meaning='另一个尚未证实的新用法'))
        row = self.slang.entries('qq_private', '10006')[0]
        senses = json.loads(row['senses'])
        self.assertEqual(len(senses), 2)
        self.assertEqual(senses[0]['meaning'], '机器人掉线或停止回应')
        self.assertNotIn('另一个尚未证实', json.dumps(self.slang.relevant('qq_private', '10006', '死了'), ensure_ascii=False))

    async def test_replay_or_duplicate_updates_do_not_increment_rounds(self):
        batch = self.seed()
        data = self.data(batch)
        data['terms'][0]['senses'].append(dict(data['terms'][0]['senses'][0], sense_id=1))
        self.apply(batch, data)
        self.apply(batch, self.data(batch, sense_id=1))
        sense = json.loads(self.slang.entries('qq_private', '10006')[0]['senses'])[0]
        self.assertEqual(sense['rounds'], 1)
        self.assertEqual(len(sense['evidence']), 2)

    async def test_group_and_persona_isolation_and_targeted_clear(self):
        self.confirmed()
        self.assertEqual(self.slang.relevant('qq_private', '10003', '机器人死了'), [])
        self.confirmed(group='10003')
        self.rule['persona_id'] = '另一人设'
        self.assertEqual(self.slang.relevant('qq_private', '10006', '机器人死了'), [])
        self.rule['persona_id'] = '演示人设'
        self.learning.clear_group('qq_private', '10006')
        self.assertEqual(self.slang.entries('qq_private', '10006'), [])
        self.assertTrue(self.slang.relevant('qq_private', '10003', '机器人死了'))

    async def test_same_persona_keeps_group_definitions_and_injection_separate(self):
        self.rules['qq_private', '10003']['persona_id'] = self.rule['persona_id']
        self.confirmed()
        for start, sense_id in [(1000, None), (1310, 1)]:
            batch = self.seed(start=start, group='10003')
            data = self.data(batch, sense_id=sense_id, meaning='游戏角色被击败')
            data['terms'][0]['senses'][0].update(scene='讨论游戏角色状态', clues=['游戏', '角色'])
            self.apply(batch, data)
        for group, own, other in [('10006', '机器人掉线或停止回应', '游戏角色被击败'),
                                  ('10003', '游戏角色被击败', '机器人掉线或停止回应')]:
            batch = self.seed(start=1620, group=group)
            known = json.dumps(batch['known'], ensure_ascii=False)
            self.assertIn(own, known)
            self.assertNotIn(other, known)
            result = self.inject(Event('current', group=group), {'prompt': '死了', 'system_prompt': '基础人设'})
            self.assertIn(own, result['system_prompt'])
            self.assertNotIn(other, result['system_prompt'])

    async def test_use_only_relevant_entries_and_preserve_base_prompt(self):
        self.confirmed()
        event = Event('now', text='机器人怎么死了')
        kwargs = {'prompt': '机器人怎么死了', 'system_prompt': '基础演示人设人设'}
        result = self.inject(event, kwargs)
        self.assertEqual(kwargs['system_prompt'], '基础演示人设人设')
        self.assertTrue(result['system_prompt'].startswith('基础演示人设人设'))
        self.assertIn('掉线或停止回应', result['system_prompt'])
        self.assertEqual(self.slang.relevant('qq_private', '10006', '今天午饭吃什么'), [])

    async def test_disabled_work_group_and_background_calls_get_no_injection(self):
        event = Event('now')
        kwargs = {'system_prompt': 'base'}
        self.assertIs(self.slang.inject(event, kwargs), kwargs)
        self.rule['persona_id'] = '工作群'
        self.assertIs(self.inject(event, kwargs), kwargs)
        self.rule['persona_id'] = '演示人设'
        self.rule['slang_learning'] = False
        self.assertIs(self.inject(event, kwargs), kwargs)

    async def test_no_model_call_without_enough_new_messages_or_within_interval(self):
        await self.slang.update_due('qq_private', '10006')
        self.provider.text_chat.assert_not_awaited()
        first = self.seed()
        self.apply(first)
        self.assertIsNone(self.slang.batch('qq_private', '10006', now=1020))

    async def test_reload_keeps_contextual_dictionary(self):
        self.confirmed()
        self.learning.close()
        self.learning = FeedbackLearning(self.owner, self.path)
        self.slang = self.learning.slang
        self.addCleanup(self.learning.close)
        self.assertTrue(self.slang.relevant('qq_private', '10006', '机器人又死了'))

    async def test_invalid_term_preserves_valid_terms(self):
        batch = self.seed()
        data = self.data(batch)
        data['terms'].append(dict(term='另一个词', senses='invalid'))
        self.apply(batch, data)
        self.assertEqual([r['term'] for r in self.slang.entries('qq_private', '10006')], ['死了'])

    async def test_model_failure_is_logged_and_does_not_invent_meaning(self):
        self.seed(start=1000)
        self.provider.text_chat.side_effect = RuntimeError('simulated failure')
        with patch('astrbot_plugin_qq_chat_control.slang.time.time', return_value=1010):
            with self.assertLogs('astrbot', level='ERROR'):
                await self.slang.update_due('qq_private', '10006')
        self.assertEqual(self.slang.entries('qq_private', '10006'), [])
        self.assertIsNone(self.slang.batch('qq_private', '10006', now=1311))

    async def test_candidate_context_guide_without_unverified_definition(self):
        first = self.seed()
        self.apply(first)
        result = self.inject(Event('now'), {'prompt': '死了', 'system_prompt': ''})
        self.assertNotIn('掉线或停止回应', result['system_prompt'])

    async def test_native_stream_nested_chat_receives_context_once(self):
        from astrbot.core.provider.entities import LLMResponse
        from astrbot_plugin_qq_chat_control.limits import LLMLimits, MessageUsage

        self.confirmed()
        self.owner.learning = self.learning
        self.owner.profiles = NS(inject=lambda event, kwargs: kwargs, replied=Mock())
        self.owner.tasks = set()
        self.provider.text_chat = AsyncMock(return_value=LLMResponse(role='assistant', completion_text='又掉了'))
        native_chat = self.provider.text_chat

        async def stream(**kwargs):
            yield await self.provider.text_chat(**kwargs)

        self.provider.text_chat_stream = stream
        limits = LLMLimits(self.owner, Path(self.tmp.name) / 'limits.json')
        limits.wrap_provider(self.provider)
        event = Event('current', text='机器人死了')
        event.set_extra('qq_chat_automatic', True)
        token = current_message.set(MessageUsage(event))
        try:
            async for response in self.provider.text_chat_stream(prompt='机器人死了', system_prompt='基础人设'):
                self.assertEqual(response.completion_text, '又掉了')
        finally:
            current_message.reset(token)
        instructions = native_chat.await_args.kwargs['system_prompt']
        self.assertEqual(instructions.count('群内用语理解与接梗'), 1)
        self.assertIn('机器人掉线或停止回应', instructions)

    async def test_missing_json_delimiter_from_model_keeps_grounded_candidates(self):
        batch = self.seed()
        text = json.dumps(self.data(batch), ensure_ascii=False)
        text = text.replace(', "senses"', ' "senses"', 1)
        self.provider.text_chat.return_value = NS(completion_text=text)
        with patch('astrbot_plugin_qq_chat_control.slang.time.time', return_value=1010):
            await self.slang.update_due('qq_private', '10006')
        self.assertEqual(len(self.slang.entries('qq_private', '10006')), 1)
        self.assertEqual(self.slang.relevant('qq_private', '10006', '机器人死了'), [])

    async def test_repaired_model_output_cannot_invent_evidence(self):
        self.seed()
        self.provider.text_chat.return_value = NS(completion_text='{"terms":[{"term":"死了" "senses":'
            '[{"sense_id":null,"meaning":"编造含义","scene":"编造场景","clues":[],"confidence":0.9,'
            '"evidence_ids":["invented","invented2"]}]}]}')
        with patch('astrbot_plugin_qq_chat_control.slang.time.time', return_value=1010):
            await self.slang.update_due('qq_private', '10006')
        self.assertEqual(self.slang.entries('qq_private', '10006'), [])

    async def test_sparse_uses_accumulate_as_candidates_across_batches(self):
        first = self.seed()
        self.apply(first, self.data(first, ids=[first['messages'][0]['message_id']]))
        self.assertEqual(self.slang.relevant('qq_private', '10006', '机器人死了'), [])
        second = self.seed(start=1310)
        self.apply(second, self.data(second, sense_id=1))
        self.assertTrue(self.slang.relevant('qq_private', '10006', '机器人死了'))

    async def test_quote_original_outside_preceding_context_is_included(self):
        self.seed()
        for i in range(10):
            event = Event('gap' + str(i), text='过渡话题', ts=1100 + i)
            with patch('astrbot_plugin_qq_chat_control.learning.time.time', return_value=1100 + i):
                self.learning.observe(event)
        first = self.slang.batch('qq_private', '10006', now=1110)
        self.apply(first, {'terms': []})
        second = self.seed(start=1420)
        with self.learning.db:
            self.learning.db.execute("UPDATE messages SET parent='1000' WHERE message_id='1420'")
        batch = self.slang.batch('qq_private', '10006', now=1430)
        self.assertIn('1000', [row['message_id'] for row in batch['nearby']])
        self.assertNotIn('1000', [row['message_id'] for row in batch['messages']])


if __name__ == '__main__':
    unittest.main(verbosity=2)
