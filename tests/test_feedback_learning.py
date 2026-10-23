import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from aiocqhttp import CQHttp
from aiocqhttp.exceptions import ActionFailed
from astrbot.core.message.components import Plain, Reply
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.agent.tool import ToolSet
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

from astrbot_plugin_qq_chat_control.learning import FeedbackLearning
from astrbot_plugin_qq_chat_control.limits import current_message
from astrbot_plugin_qq_chat_control.output import QuietOutput
from astrbot_plugin_qq_chat_control.profiles import MemberProfiles
from astrbot_plugin_qq_chat_control import profiles as profiles_module


class Event:
    def __init__(self, message_id, sender='alice', text='这首歌的副歌很适合打艺', parent='', group='10006', ts=1000):
        self.group, self.sender, self.extras = group, sender, {}
        self.parts = [Plain(text)] + ([Reply(id=parent)] if parent else [])
        self.message_obj = NS(raw_message={'message_id': message_id, 'time': ts, 'post_type': 'message'})
        self.unified_msg_origin = 'qq_private:GroupMessage:' + sender + '_' + group

    def get_platform_name(self): return 'aiocqhttp'
    def get_platform_id(self): return 'qq_private'
    def get_group_id(self): return self.group
    def get_sender_id(self): return self.sender
    def get_self_id(self): return 'bot'
    def get_messages(self): return self.parts
    def get_extra(self, key, default=None): return self.extras.get(key, default)
    def set_extra(self, key, value): self.extras[key] = value


class FeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'learning.db'
        self.rule = dict(persona_id='演示人设', feedback_observe=True, feedback_bot_ids=['other_bot'], provider_id='gemini')
        self.provider = NS(text_chat=AsyncMock(), bind_origin=lambda _: None)
        self.owner = NS(closing=False, rules={('qq_private', '10006'): self.rule},
            config={'allowed_group_ids': ['10006'], 'default_provider_id': 'gemini'},
            context=NS(get_config=lambda _: {'wake_prefix': ['/']}), tasks=set(),
            rule=lambda event: self.owner.rules.get((event.get_platform_id(), event.get_group_id()), {}),
            provider=lambda _: self.provider, limits=NS(successful=lambda result: bool(result.completion_text)))
        self.owner.profiles = MemberProfiles(self.owner, Path(self.tmp.name) / 'members.json')
        self.learning = FeedbackLearning(self.owner, self.path)
        native_observe = self.learning.observe

        def observe(event):
            with patch('astrbot_plugin_qq_chat_control.learning.time.time',
                       return_value=event.message_obj.raw_message['time']):
                native_observe(event)
        self.learning.observe = observe
        self.addCleanup(self.learning.close)

    def exposure(self, message_id='trigger', outgoing='sent', sender='alice', ts=1000):
        event = Event(message_id, sender=sender, ts=ts - 10)
        self.learning.observe(event)
        self.prepare(event)
        self.learning.generated(event)
        self.learning.sent(event, outgoing, [{'type': 'text', 'data': {'text': '这段进副歌确实舒服'}}], now=ts)
        self.learning.finish(event)
        return event

    async def test_low_affinity_cancels_inflight_send_before_platform_call(self):
        self.rule['member_profiles'] = True
        self.owner.profiles.records['qq_private:10006:alice'] = dict(affinity=19, affinity_recovery_at=1000)
        event = Event('pending')
        original = AsyncMock(return_value={'message_id': 'sent'})
        with patch.object(CQHttp, 'call_action', original), patch.object(profiles_module, 'time', return_value=1000):
            self.learning.install()
            token = current_message.set(NS(event=event))
            try:
                for action, params in [('send_group_msg', dict(group_id='10006', message=[])),
                                       ('set_msg_emoji_like', dict(message_id='pending', emoji_id='424', set=True))]:
                    with self.assertRaises(asyncio.CancelledError):
                        await CQHttp.call_action(object(), action, **params)
                self.assertTrue(event.get_extra('qq_auto_cancelled'))
                original.assert_not_awaited()
            finally:
                current_message.reset(token)
                self.learning.close()

    def prepare(self, event):
        token = current_message.set(NS(event=event))
        try:
            self.learning.prepare(event)
        finally:
            current_message.reset(token)

    def judge(self, batch, choices=None, intent='positive'):
        data = {'items': [dict(message_id=item['message_id'],
                              reply_id=(choices or {}).get(item['message_id'], item['fixed_reply']),
                              intent=intent, attribution_confidence=.95, intent_confidence=.95, reason='引用和语境明确') for item in batch['items']]}
        self.learning.apply_judgments(batch, data)

    def row(self, reply_id):
        return dict(self.learning.db.execute('SELECT * FROM replies WHERE id=?', (reply_id,)).fetchone())

    async def test_correction_cause_requires_exact_user_quote(self):
        event=self.exposure()
        self.learning.observe(Event('reaction',sender='bob',parent='sent',text='我说的是串口，不是网口',ts=1010))
        batch=self.learning.batch('qq_private','10006')
        item=self.feedback_item(event.get_extra('qq_feedback_reply'),intent='correction')
        item.update(cause='understanding',cause_confidence=.98,evidence_text='我说的是串口')
        self.learning.apply_judgments(batch,{'items':[item]})
        self.learning.settle(now=1601)
        self.assertEqual(self.learning.db.execute('SELECT cause FROM feedback').fetchone()[0],'understanding')
        self.assertEqual(json.loads(self.row(event.get_extra('qq_feedback_reply'))['summary'])['causes']['understanding'],1)

    async def test_fabricated_cause_evidence_stays_unknown_and_requests_review(self):
        event=self.exposure()
        self.learning.observe(Event('reaction',sender='bob',parent='sent',text='我说的是串口，不是网口',ts=1010))
        batch=self.learning.batch('qq_private','10006')
        item=self.feedback_item(event.get_extra('qq_feedback_reply'),intent='correction')
        item.update(cause='fact',cause_confidence=.99,evidence_text='你算错了')
        self.learning.validate_judgments(batch,{'items':[item]})
        self.assertEqual(item['cause'],'unknown')
        self.assertTrue(self.learning.uncertain(batch['items'][0],item))

    async def test_uncertain_cause_review_failure_preserves_certain_correction(self):
        event=self.exposure()
        self.learning.observe(Event('reaction',sender='bob',parent='sent',text='你说错了',ts=1010))
        item=self.feedback_item(event.get_extra('qq_feedback_reply'),intent='correction')
        self.learning.policy.judge=AsyncMock(side_effect=[json.dumps({'items':[item]}),RuntimeError('offline')])
        with self.assertLogs('astrbot',level='ERROR'):
            await self.learning.analyze(self.learning.batch('qq_private','10006'))
        self.learning.settle(now=1601)
        row=self.learning.db.execute('SELECT intent,cause FROM feedback').fetchone()
        self.assertEqual(tuple(row),('correction','unknown'))
        self.assertEqual(json.loads(self.row(event.get_extra('qq_feedback_reply'))['summary'])['correction_people'],1)

    def feedback_item(self, target, attribution=.95, confidence=.95, intent='positive', mid='reaction'):
        return dict(message_id=mid, reply_id=target, attribution_confidence=attribution,
                    intent_confidence=confidence, intent=intent, reason='引用具体原话')

    async def test_uncertain_feedback_uses_independent_codex_review(self):
        event = self.exposure()
        self.learning.observe(Event('reaction', sender='bob', parent='sent', ts=1010))
        target = event.get_extra('qq_feedback_reply')
        first = self.feedback_item(target, confidence=.4)
        second = self.feedback_item(target, intent='annoyance')
        self.learning.policy.judge = AsyncMock(side_effect=[json.dumps({'items': [first]}), json.dumps({'items': [second]})])
        await self.learning.analyze(self.learning.batch('qq_private', '10006'))
        calls = self.learning.policy.judge.await_args_list
        self.assertTrue(calls[1].kwargs['recheck'])
        self.assertNotIn('judgments', calls[1].args[2])
        row = self.learning.db.execute('SELECT * FROM feedback').fetchone()
        self.assertEqual(row['judge_provider'], 'local_codex/gpt-6.1-sol')
        self.assertEqual(row['intent'], 'annoyance')
        self.assertEqual(row['attribution_confidence'], 1)

    async def test_confident_unrelated_feedback_does_not_call_codex(self):
        self.exposure()
        self.learning.observe(Event('reaction', sender='bob', text='另外这件事不一样', ts=1010))
        self.learning.policy.judge = AsyncMock(return_value=json.dumps({'items': [self.feedback_item(None, intent='neutral')]}))
        await self.learning.analyze(self.learning.batch('qq_private', '10006'))
        self.learning.policy.judge.assert_awaited_once()
        self.assertEqual(self.learning.db.execute('SELECT COUNT(*) FROM feedback').fetchone()[0], 0)

    async def test_codex_unknown_semantic_feedback_is_not_zero_reward(self):
        event = self.exposure()
        for i in range(5):
            self.learning.observe(Event(str(i), sender=str(i % 2), text='这句话可能是另外一件事', ts=1010+i))
        data = {'items': [self.feedback_item(None, .4, .4, 'unknown', str(i)) for i in range(5)]}
        self.learning.policy.judge = AsyncMock(return_value=json.dumps(data))
        await self.learning.analyze(self.learning.batch('qq_private', '10006'))
        self.learning.settle(now=1601)
        row = self.row(event.get_extra('qq_feedback_reply'))
        self.assertEqual(row['outcome'], 'uncertain')
        self.assertIsNone(row['score'])

    async def test_invalid_primary_batch_is_replaced_before_any_write(self):
        event = self.exposure()
        self.learning.observe(Event('reaction', sender='bob', parent='sent', ts=1010))
        data = {'items': [self.feedback_item(event.get_extra('qq_feedback_reply'))]}
        self.learning.policy.judge = AsyncMock(side_effect=['{}', json.dumps(data)])
        with self.assertLogs('astrbot', level='ERROR'):
            await self.learning.analyze(self.learning.batch('qq_private', '10006'))
        self.assertEqual(self.learning.db.execute('SELECT intent FROM feedback').fetchone()[0], 'positive')

    async def test_low_attribution_is_not_rescued_by_high_intent(self):
        event = self.exposure()
        self.learning.observe(Event('reaction', sender='bob', text='这首歌确实很好听', ts=1010))
        data = {'items': [self.feedback_item(event.get_extra('qq_feedback_reply'), attribution=.5)]}
        self.learning.apply_judgments(self.learning.batch('qq_private', '10006'), data)
        self.assertEqual(self.learning.db.execute('SELECT COUNT(*) FROM feedback').fetchone()[0], 0)
        self.assertEqual(self.learning.db.execute("SELECT analyzed FROM messages WHERE message_id='reaction'").fetchone()[0], 2)

    async def test_failed_recheck_does_not_overwrite_confident_items(self):
        event = self.exposure()
        target = event.get_extra('qq_feedback_reply')
        for mid in ('certain', 'uncertain'):
            self.learning.observe(Event(mid, sender=mid, parent='sent', ts=1010))
        data = {'items': [self.feedback_item(target, mid='certain'),
                          self.feedback_item(target, confidence=.4, mid='uncertain')]}
        self.learning.policy.judge = AsyncMock(side_effect=[json.dumps(data), RuntimeError('offline')])
        with self.assertLogs('astrbot', level='ERROR'):
            await self.learning.analyze(self.learning.batch('qq_private', '10006'))
        result = dict(self.learning.db.execute('SELECT message_id,intent FROM feedback'))
        self.assertEqual(result, {'certain': 'positive', 'uncertain': 'unknown'})
        reviewed = self.learning.policy.judge.await_args_list[1].args[2]['messages']
        self.assertEqual([item['message_id'] for item in reviewed], ['uncertain'])

    async def test_scope_excludes_other_group_work_persona_self_bots_commands(self):
        for event in (Event('1', group='10003'), Event('2', sender='bot'),
                      Event('3', sender='other_bot'), Event('4', text='/画像')):
            self.learning.observe(event)
        self.assertFalse(self.path.exists())
        self.rule['persona_id'] = '工作群'
        self.learning.observe(Event('5'))
        self.assertFalse(self.path.exists())

    async def test_multiple_delivery_segments_one_exposure_direct_quote_any_segment(self):
        event = self.exposure()
        self.learning.sent(event, 'sent2', [{'type': 'text', 'data': {'text': '下一句'}}], now=1002)
        self.learning.sent(event, 'sent2', [{'type': 'text', 'data': {'text': '下一句'}}], now=1002)
        self.learning.observe(Event('reaction', sender='bob', parent='sent2', ts=1010))
        batch = self.learning.batch('qq_private', '10006')
        self.assertEqual(batch['items'][0]['fixed_reply'], event.get_extra('qq_feedback_reply'))
        self.judge(batch)
        self.learning.settle(now=1603)
        row = self.row(event.get_extra('qq_feedback_reply'))
        self.assertEqual(row['score'], .25)
        self.assertEqual(json.loads(row['summary'])['new_participants'], 1)
        self.assertEqual(self.learning.db.execute('SELECT COUNT(*) FROM replies').fetchone()[0], 1)
        self.assertEqual(self.learning.db.execute('SELECT COUNT(*) FROM outgoing').fetchone()[0], 2)

    async def test_chain_weight_and_same_person_cap_initiator_discount(self):
        event = self.exposure()
        for i, sender, parent in [('1', 'bob', 'sent'), ('2', 'bob', 'sent'),
                                   ('3', 'charlie', '1'), ('4', 'alice', 'sent')]:
            self.learning.observe(Event(i, sender=sender, parent=parent, ts=1010 + int(i)))
        batch = self.learning.batch('qq_private', '10006')
        self.assertEqual([i['depth'] for i in batch['items']], [0, 0, 1, 0])
        self.judge(batch)
        self.learning.settle(now=1601)
        row = self.row(event.get_extra('qq_feedback_reply'))
        self.assertAlmostEqual(row['score'], (1 + .7 + .25) / 4)
        self.assertEqual(json.loads(row['summary'])['new_participants'], 2)

    async def test_quote_is_not_positive_sentiment(self):
        event = self.exposure()
        self.learning.observe(Event('1', sender='bob', text='别刷了好烦啊', parent='sent', ts=1010))
        self.judge(self.learning.batch('qq_private', '10006'), intent='annoyance')
        self.learning.settle(now=1601)
        self.assertEqual(self.row(event.get_extra('qq_feedback_reply'))['score'], -.5)

    async def test_automatic_reply_has_no_discounted_question_asker(self):
        event = Event('auto')
        event.set_extra('qq_chat_automatic', True)
        self.learning.observe(event)
        self.prepare(event)
        self.assertEqual(self.row(event.get_extra('qq_feedback_reply'))['initiator'], '')

    async def test_seconds_precision_source_timestamp_does_not_drop_fast_quote(self):
        event = self.exposure(ts=1000.8)
        reaction = Event('fast', sender='bob', parent='sent', ts=1000)
        with patch('astrbot_plugin_qq_chat_control.learning.time.time', return_value=1000.9):
            FeedbackLearning.observe(self.learning, reaction)
        batch = self.learning.batch('qq_private', '10006')
        self.assertEqual(batch['items'][0]['fixed_reply'], event.get_extra('qq_feedback_reply'))
        row = self.learning.db.execute("SELECT ts,source_ts FROM messages WHERE message_id='fast'").fetchone()
        self.assertEqual((row['ts'], row['source_ts']), (1000.9, 1000))

    async def test_correction_is_preserved_not_rewarded(self):
        event = self.exposure()
        self.learning.observe(Event('1', sender='bob', parent='sent', ts=1010))
        self.judge(self.learning.batch('qq_private', '10006'), intent='correction')
        self.learning.settle(now=1601)
        row = self.row(event.get_extra('qq_feedback_reply'))
        self.assertIsNone(row['score'])
        self.assertEqual(json.loads(row['summary'])['correction_people'], 1)

    async def test_semantic_compares_parallel_replies_no_duplicate_credit(self):
        first = self.exposure()
        second = self.exposure('trigger2', 'sent2', ts=1005)
        self.learning.observe(Event('reaction', sender='bob', ts=1010))
        batch = self.learning.batch('qq_private', '10006')
        target = second.get_extra('qq_feedback_reply')
        self.assertEqual(len(batch['items'][0]['candidates']), 2)
        self.judge(batch, {'reaction': target})
        self.judge(batch, {'reaction': first.get_extra('qq_feedback_reply')})
        rows = self.learning.db.execute('SELECT * FROM feedback').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['reply_id'], target)
        self.assertEqual(rows[0]['weight'], .3)

    async def test_short_reply_is_judged_but_late_and_cross_group_are_excluded(self):
        event = self.exposure()
        for i, text, ts in [('1', '笑死', 1010), ('2', '这首歌的副歌确实好听', 1130)]:
            self.learning.observe(Event(i, sender='bob', text=text, ts=ts))
        self.learning.observe(Event('3', sender='bob', parent='sent', group='10003', ts=1010))
        batch = self.learning.batch('qq_private', '10006')
        self.assertEqual([i['message_id'] for i in batch['items']], ['1'])
        self.judge(batch, intent='neutral')
        self.learning.settle(now=1601)
        self.assertIsNone(self.row(event.get_extra('qq_feedback_reply'))['score'])

    async def test_late_direct_quote_counts_but_outside_ten_minutes_does_not(self):
        self.exposure()
        self.learning.observe(Event('late', sender='bob', parent='sent', ts=1500))
        self.learning.observe(Event('expired', sender='bob', parent='sent', ts=1610))
        batch = self.learning.batch('qq_private', '10006')
        self.assertEqual([i['message_id'] for i in batch['items']], ['late'])

    async def test_quiet_group_insufficient_and_active_group_weak_zero(self):
        first = self.exposure()
        self.learning.settle(now=1601)
        self.assertEqual(self.row(first.get_extra('qq_feedback_reply'))['outcome'], 'insufficient')
        second = self.exposure('trigger2', 'sent2', ts=2000)
        for i in range(5):
            self.learning.observe(Event(str(i), sender='bob' if i % 2 else 'charlie', text='无关', ts=2010 + i))
        self.judge(self.learning.batch('qq_private', '10006'), intent='neutral')
        self.learning.settle(now=2601)
        row = self.row(second.get_extra('qq_feedback_reply'))
        self.assertEqual(row['outcome'], 'active_no_response')
        self.assertEqual(row['score'], 0)

    async def test_reload_resume_once_and_group_clear(self):
        event = self.exposure()
        self.learning.observe(Event('reaction', sender='bob', parent='sent', ts=1010))
        self.learning.close()
        self.learning = FeedbackLearning(self.owner, self.path)
        self.addCleanup(self.learning.close)
        self.learning.open()
        self.judge(self.learning.batch('qq_private', '10006'))
        self.learning.settle(now=1601)
        before = self.row(event.get_extra('qq_feedback_reply'))
        self.learning.settle(now=1700)
        self.assertEqual(self.row(event.get_extra('qq_feedback_reply')), before)
        self.learning.clear_group('qq_private', '10006')
        for table in ('messages', 'outgoing', 'replies', 'feedback'):
            self.assertEqual(self.learning.db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 0)

    async def test_judge_failure_preserves_quote_without_false_score(self):
        event = self.exposure()
        self.learning.observe(Event('reaction', sender='bob', parent='sent', ts=1010))
        self.provider.text_chat.side_effect = RuntimeError('simulated offline')
        with self.assertLogs('astrbot', level='ERROR'):
            await self.learning.analyze(self.learning.batch('qq_private', '10006'))
        self.learning.settle(now=1601)
        row = self.row(event.get_extra('qq_feedback_reply'))
        self.assertIsNone(row['score'])
        self.assertEqual(row['outcome'], 'analysis_failed')

    async def test_partial_delivery_not_used_as_low_quality_sample(self):
        event = self.exposure()
        self.learning.db.execute('UPDATE replies SET send_errors=1')
        self.learning.settle(now=1601)
        self.assertEqual(self.row(event.get_extra('qq_feedback_reply'))['outcome'], 'partial_delivery')

    async def test_invalid_or_incomplete_judgment_rolls_back_whole_batch(self):
        self.exposure()
        for i in ['1', '2']:
            self.learning.observe(Event(i, sender='bob', parent='sent', ts=1010))
        batch = self.learning.batch('qq_private', '10006')
        bad = {'items': [dict(message_id='1', reply_id=1, intent='positive', attribution_confidence=.95, intent_confidence=.95),
                         dict(message_id='2', reply_id=1, intent='positive', attribution_confidence=.95, intent_confidence=8)]}
        with self.assertRaises(ValueError):
            self.learning.apply_judgments(batch, bad)
        self.assertEqual(self.learning.db.execute('SELECT COUNT(*) FROM feedback').fetchone()[0], 0)

    async def test_pending_batch_prevents_premature_settlement(self):
        event = self.exposure()
        self.learning.observe(Event('pending', sender='bob', parent='sent', ts=1010))
        self.learning.settle(now=1601)
        self.assertEqual(self.row(event.get_extra('qq_feedback_reply'))['status'], 'observing')

    async def test_background_profile_analysis_and_copied_events_are_not_exposures(self):
        event = Event('profile')
        event.set_extra('qq_chat_automatic', True)
        self.learning.prepare(event)
        self.assertFalse(self.path.exists())
        token = current_message.set(NS(event=Event('actual_pipeline')))
        try:
            self.learning.prepare(event)
            self.assertFalse(self.path.exists())
        finally:
            current_message.reset(token)

    async def test_add_arrival_timestamp_preserves_existing_observation(self):
        with sqlite3.connect(self.path) as db:
            db.execute('CREATE TABLE messages (platform TEXT,group_id TEXT,message_id TEXT,sender TEXT,ts REAL,'
                       'parent TEXT,text TEXT,kinds TEXT,analyzed INTEGER DEFAULT 0,'
                       'PRIMARY KEY(platform,group_id,message_id))')
            db.execute("INSERT INTO messages VALUES ('qq_private','10006','kept','alice',1000,'','原发言','[]',0)")
        self.learning.open()
        row = self.learning.db.execute("SELECT text,source_ts FROM messages WHERE message_id='kept'").fetchone()
        self.assertEqual(row['text'], '原发言')
        self.assertIsNone(row['source_ts'])

    async def test_real_pipeline_context_provider_binding_and_streaming_receipts(self):
        from astrbot.core.pipeline.scheduler import PipelineScheduler
        from astrbot_plugin_qq_chat_control.main import QQChatControl
        from astrbot.core.provider.entities import LLMResponse, ProviderRequest
        from astrbot_plugin_qq_chat_control import main as chat

        provider = NS(provider_config={'type': 'local_antigravity_cli', 'id': 'gemini'},
                      bind_origin=Mock(), chat_origins=lambda: set(),
                      text_chat=AsyncMock(return_value=LLMResponse(role='assistant', completion_text='副歌舒服')))

        async def stream(**kwargs):
            yield LLMResponse(role='assistant', completion_text='副歌舒服')
        provider.text_chat_stream = stream
        context = NS(get_all_providers=lambda: [provider], get_provider_by_id=lambda _: provider,
                     get_config=lambda *args: {'wake_prefix': ['/']}, register_web_api=Mock(),
                     get_llm_tool_manager=lambda: NS(get_full_tool_set=lambda: ToolSet([]),
                                                     iter_builtin_tools=lambda: []))
        config = dict(self.owner.config, quiet_group_errors=False, codex_provider_id='codex',
                      allowed_qq_ids=['alice'], group_rules=[dict(self.rule, platform_id='qq_private', group_id='10006')])
        event = Event('pipeline')
        api = NS(call_action=AsyncMock(side_effect=[{'message_id': 7}, {'message_id': 8}]))
        bot = object.__new__(CQHttp)
        bot._api = api
        native_bindings = chat.PersonaBindings
        native_limits = chat.LLMLimits
        with patch.object(chat, 'PersonaBindings', side_effect=lambda ctx, path: native_bindings(ctx, Path(self.tmp.name) / 'personas.json')), \
             patch.object(chat, 'LLMLimits', side_effect=lambda owner, path: native_limits(owner, Path(self.tmp.name) / 'limits.json')):
            plugin = QQChatControl(context, config)
        plugin.learning.path = self.path
        self.learning.close()
        plugin.profiles.path = Path(self.tmp.name) / 'members.json'
        plugin.learning.observe(event)

        async def execute(scheduler, received):
            await plugin.bind_request(received, ProviderRequest())
            async for _ in provider.text_chat_stream(prompt='打艺'):
                pass
            for text in ['副歌', '舒服']:
                await bot.send_group_msg(group_id=10006, message=[{'type': 'text', 'data': {'text': text}}])

        with patch.object(PipelineScheduler, 'execute', execute):
            plugin.learning.install()
            plugin.limits.install()
            try:
                await PipelineScheduler.execute(None, event)
                row = dict(plugin.learning.db.execute('SELECT * FROM replies').fetchone())
                self.assertEqual((row['finished'], row['generated'], row['status']), (1, 1, 'observing'))
                self.assertEqual(plugin.learning.db.execute('SELECT COUNT(*) FROM outgoing').fetchone()[0], 2)
                provider.bind_origin.assert_called_once_with(event.unified_msg_origin, progress_callback=None)
                self.assertIsNone(current_message.get())
            finally:
                await plugin.terminate()

    async def test_real_sdk_receipt_intercept_and_restore_no_extra_sends(self):
        event = Event('trigger')
        self.learning.observe(event)
        self.prepare(event)
        api = NS(call_action=AsyncMock(return_value={'message_id': 123}))
        bot = object.__new__(CQHttp)
        bot._api = api
        original = CQHttp.call_action
        self.learning.install()
        token = current_message.set(NS(event=event))
        try:
            result = await bot.send_group_msg(group_id=10006, message=[{'type': 'text', 'data': {'text': '你好'}}])
            self.assertEqual(result['message_id'], 123)
            api.call_action.assert_awaited_once()
            self.assertEqual(self.learning.db.execute('SELECT message_id FROM outgoing').fetchone()[0], '123')
        finally:
            current_message.reset(token)
            self.learning.close()
        self.assertIs(CQHttp.call_action, original)

    async def test_confirmed_timeout_keeps_actual_id_without_resending(self):
        event = Event('trigger')
        self.learning.observe(event)
        self.prepare(event)
        failure = ActionFailed({'retcode': 1200, 'message':
            'Timeout: NTEvent serviceAndMethod:NodeIKernelMsgService/sendMsg'})
        original = AiocqhttpMessageEvent.__dict__['send_message']
        mocked_send = AsyncMock(side_effect=failure)
        AiocqhttpMessageEvent.send_message = classmethod(mocked_send)
        quiet = QuietOutput()
        quiet.on_confirmed = self.learning.confirmed
        row = {'sender': {'user_id': 'bot'}, 'time': 1000, 'message_id': 88,
               'message': [{'type': 'text', 'data': {'text': '测试回复'}}]}
        bot = NS(get_group_msg_history=AsyncMock(return_value={'messages': [row]}))
        token = current_message.set(NS(event=event))
        try:
            with patch('astrbot_plugin_qq_chat_control.output.time.time', return_value=1000):
                await AiocqhttpMessageEvent.send_message(bot, MessageChain([Plain('测试回复')]),
                                                        {'self_id': 'bot'}, True, '10006')
            self.assertEqual(mocked_send.await_count, 1)
            self.assertEqual(self.learning.db.execute('SELECT message_id FROM outgoing').fetchone()[0], '88')
        finally:
            current_message.reset(token)
            quiet.close()
            AiocqhttpMessageEvent.send_message = original


if __name__ == '__main__':
    unittest.main(verbosity=2)
