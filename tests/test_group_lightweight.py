import asyncio
import json
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from astrbot_plugin_qq_chat_control.learning import FeedbackLearning
from astrbot_plugin_qq_chat_control.interaction import rank_candidates
import test_group_background as background_fixture
import test_group_adaptation as fixture


class LightweightTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.AdaptationTests.asyncSetUp
    event = fixture.AdaptationTests.event
    bound = fixture.AdaptationTests.bound
    image = fixture.AdaptationTests.image
    setup_background = background_fixture.BackgroundTests.setup_background
    candidates = background_fixture.BackgroundTests.candidates
    review = background_fixture.BackgroundTests.review

    def seed(self, count=10, single_person=False):
        scope = self.setup_background()
        for i in range(count):
            self.event(sender='alice' if single_person or i % 2 == 0 else 'bob', text='音乐话题里接一个跨作品玩笑')
        self.learning.db.execute('UPDATE expression_pending SET ts=ts-360 WHERE message_id=?', ('1',))
        return scope

    def extraction(self, uncertain=False, match_id=None):
        return dict(items=[dict(match_id=match_id, scene='音乐演出话题', style='用熟悉作品作轻度唱反调',
            boundary='认真求助和要求停止时不用', confidence=.9, uncertain=uncertain, message_ids=['1', '10'])])

    def mock_candidates(self):
        self.bg.context = AsyncMock(return_value={})
        self.stickers.ranked_pool = AsyncMock(return_value=[])
        self.choose.generate = AsyncMock(return_value=dict(items=self.candidates()))
        self.policy.judge = AsyncMock(return_value=json.dumps(self.review()))

    async def send_candidate(self, event):
        event.set_extra('qq_chat_automatic', True)
        with self.bound(event):
            self.learning.prepare(event)
            await self.policy.prepare(event, NS(prompt='音乐', system_prompt='群友', conversation=None))

    async def test_pending_survives_context_cleanup_and_restart(self):
        scope = self.seed()
        with self.learning.db:
            self.learning.db.execute('DELETE FROM messages')
        self.learning.close()
        self.learning.open()
        self.policy.judge = AsyncMock(return_value=json.dumps(self.extraction()))
        await self.bg.habits(scope)
        state = self.learning.expressions.snapshot(scope)
        self.assertEqual(state['meta']['pending'], 0)
        self.assertEqual(state['meta']['processed'], 10)
        self.assertEqual(state['items'][0]['status'], 'active')

    async def test_single_person_repetition_stays_pending(self):
        scope = self.seed(single_person=True)
        self.policy.judge = AsyncMock(return_value=json.dumps(self.extraction()))
        await self.bg.habits(scope)
        self.assertEqual(self.learning.expressions.snapshot(scope)['items'][0]['status'], 'pending')
        self.assertEqual((await self.bg.context(scope, '音乐'))['expressions'], [])

    async def test_same_fragment_is_not_stable(self):
        scope = self.seed()
        self.learning.db.execute('UPDATE expression_pending SET ts=?', (int(time.time() // 300) * 300 + 1,))
        self.policy.judge = AsyncMock(return_value=json.dumps(self.extraction()))
        await self.bg.habits(scope)
        self.assertEqual(self.learning.expressions.snapshot(scope)['items'][0]['status'], 'pending')

    async def test_failure_keeps_batch_and_waits_next_cycle(self):
        scope = self.seed()
        self.policy.judge = AsyncMock(side_effect=RuntimeError('model unavailable'))
        await self.bg.habits(scope)
        await self.bg.habits(scope)
        status = self.learning.expressions.snapshot(scope)
        self.assertEqual(status['meta']['pending'], 10)
        self.assertEqual(status['meta']['failed'], 1)
        self.policy.judge.assert_awaited_once()

    async def test_uncertain_recheck_remains_unconfirmed(self):
        scope = self.seed()
        self.policy.judge = AsyncMock(return_value=json.dumps(self.extraction(uncertain=True)))
        await self.bg.habits(scope)
        self.assertEqual(self.policy.judge.await_count, 2)
        self.assertTrue(self.policy.judge.call_args.kwargs['recheck'])
        self.assertEqual(self.learning.expressions.snapshot(scope)['items'][0]['status'], 'pending')

    async def test_capacity_expiry_and_duplicate_observation(self):
        scope = self.setup_background()
        now = time.time()
        rows = [(*scope, self.rules[scope]['persona_id'], str(i), 'alice', now+i/10000, '', '短句') for i in range(505)]
        self.learning.db.executemany('INSERT INTO expression_pending VALUES (?,?,?,?,?,?,?,?)', rows)
        self.learning.expressions.prune(scope)
        self.assertEqual(self.learning.expressions.snapshot(scope)['meta']['pending'], 500)
        self.assertEqual(self.learning.expressions.snapshot(scope)['meta']['dropped'], 5)
        self.learning.db.execute('UPDATE expression_pending SET ts=? WHERE message_id=?', (now-86401, '10'))
        self.learning.expressions.prune(scope)
        self.assertEqual(self.learning.expressions.snapshot(scope)['meta']['dropped'], 6)

    async def test_group_persona_disabled_and_clear_boundaries(self):
        scope = self.seed()
        self.rules[scope]['group_background'] = False
        self.event()
        self.assertEqual(self.learning.expressions.snapshot(scope)['meta']['pending'], 10)
        self.rules[scope]['group_background'] = True
        self.rules[scope]['persona_id'] = '另一人设'
        self.assertEqual(self.learning.expressions.snapshot(scope)['meta']['pending'], 0)
        self.assertEqual(self.learning.expressions.snapshot(('qq_private', '10003'))['items'], [])
        self.learning.clear_group(*scope)
        self.assertEqual(self.learning.db.execute('SELECT COUNT(*) FROM expression_pending').fetchone()[0], 0)

    async def test_command_and_bot_are_not_material(self):
        scope = self.setup_background()
        self.rules[scope]['feedback_bot_ids'] = ['robot2']
        for sender, text in [('bot', '自言自语'), ('robot2', '别的机器人'), ('alice', '/jrlp')]:
            self.event(sender=sender, text=text)
        self.assertEqual(self.learning.expressions.snapshot(scope)['meta']['pending'], 0)

    async def test_frontend_work_defers_extraction(self):
        scope = self.seed()
        task = asyncio.create_task(asyncio.sleep(10))
        self.owner.limits.tasks.add(task)
        try:
            self.policy.judge = AsyncMock()
            await self.bg.habits(scope)
            self.policy.judge.assert_not_awaited()
            self.assertEqual(self.learning.expressions.snapshot(scope)['meta']['pending'], 10)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.owner.limits.tasks.discard(task)

    async def test_retrieval_uses_one_query_and_total_budget(self):
        scope = self.seed()
        self.policy.judge = AsyncMock(return_value=json.dumps(self.extraction()))
        await self.bg.habits(scope)
        self.bg.catalog(scope, 'file', [dict(id='doc', name='music.txt', updated=1, size=100)])
        self.learning.db.execute("UPDATE group_sources SET status='ready'")
        for i in range(5):
            self.learning.db.execute('INSERT INTO group_chunks VALUES (?,?,?,?,?,?,?)', (*scope, 'doc', i, '音'*1200, '[1,0]', 'test_embedding'))
        self.embedding.get_embedding.reset_mock()
        context = await self.bg.context(scope, '音乐演出')
        self.embedding.get_embedding.assert_awaited_once()
        self.assertEqual(len(context['expressions']), 1)
        self.assertLessEqual(len(json.dumps(context['expressions'], ensure_ascii=False)) + sum(len(x['text']) for x in context['sources']), 6000)

    async def test_legacy_habits_are_not_confirmed_patterns(self):
        scope = self.setup_background()
        self.bg.save(scope, dict(habits=[dict(text='旧猜测', evidence=[])]))
        context = await self.bg.context(scope, '音乐')
        self.assertEqual(context['habits'], [])
        self.assertEqual(self.bg.state(scope)['habits'][0]['text'], '旧猜测')

    async def test_seven_messages_reload_eighth_takes_once(self):
        scope = self.setup_background()
        rule = dict(message_count=8, reply_probability=100)
        op = self.learning.opportunities
        for _ in range(7):
            self.assertFalse(op.take(self.event(), rule))
        self.learning.close()
        self.learning.open()
        self.assertTrue(op.take(self.event(), rule))
        self.assertFalse(op.take(self.event(), rule))
        self.assertEqual(op.state(scope)['count'], 1)

    async def test_retry_once_without_lottery_and_next_count_capped(self):
        scope = self.setup_background()
        op, rule = self.learning.opportunities, dict(message_count=8, reply_probability=100)
        for _ in range(8):
            event = self.event()
            taken = op.take(event, rule)
        self.assertTrue(taken)
        for _ in range(20):
            self.assertFalse(op.take(self.event(), rule))
        self.assertEqual(op.state(scope)['count'], 8)
        op.finish(event, technical=True)
        retry = self.event()
        with patch('astrbot_plugin_qq_chat_control.opportunities.random.random', side_effect=AssertionError('retry must not draw')):
            self.assertTrue(op.take(retry, rule))
        op.finish(retry, technical=True)
        self.assertEqual(op.state(scope)['status'], 'idle')
        self.assertEqual(op.state(scope)['attempt'], 2)

    async def test_restart_presend_retries_but_sending_does_not(self):
        scope = self.setup_background()
        rule = dict(message_count=1, reply_probability=100)
        event = self.event()
        self.learning.opportunities.take(event, rule)
        self.learning.close()
        self.learning.open()
        self.assertEqual(self.learning.opportunities.state(scope)['status'], 'retry')
        event = self.event()
        self.learning.opportunities.take(event, rule)
        self.learning.opportunities.sending(event)
        self.learning.close()
        self.learning.open()
        self.assertEqual(self.learning.opportunities.state(scope)['status'], 'idle')

    async def test_technical_candidate_failure_retains_one_opportunity(self):
        scope = self.setup_background()
        self.mock_candidates()
        self.choose.generate.side_effect = ValueError('invalid model json')
        for attempt in (1, 2):
            event = self.event()
            self.assertTrue(self.learning.opportunities.take(event, dict(message_count=8 if attempt==2 else 1, reply_probability=100)))
            await self.send_candidate(event)
            event.send.assert_not_awaited()
            self.assertEqual(self.learning.opportunities.state(scope)['status'], 'retry' if attempt==1 else 'idle')

    async def test_semantic_skip_consumes_opportunity(self):
        scope = self.setup_background()
        self.mock_candidates()
        self.choose.generate.return_value = dict(items=[])
        event = self.event()
        self.learning.opportunities.take(event, dict(message_count=1, reply_probability=100))
        await self.send_candidate(event)
        self.assertEqual(self.learning.opportunities.state(scope)['status'], 'idle')
        event.send.assert_not_awaited()

    async def test_malformed_candidate_and_review_do_not_kill_valid_one(self):
        scope = self.setup_background()
        self.mock_candidates()
        self.choose.generate.return_value = dict(items=[None, self.candidates()[1], dict(self.candidates()[2], intensity=101)])
        self.policy.judge.return_value = json.dumps(dict(items=[self.review()['items'][1]]))
        selected = await self.choose.choose(scope, NS(system_prompt='群友'), [], {})
        self.assertEqual(selected['id'], 2)
        selected = rank_candidates(self.candidates(), dict(items=[None, self.review()['items'][1]]), 60, [])
        self.assertEqual(selected['id'], 2)

    async def test_busy_same_topic_uses_bounded_snapshot(self):
        scope = self.setup_background()
        self.mock_candidates()
        initial = [dict(message_id='a', text='聊音乐')]
        at_review = initial + [dict(message_id='b', text='我也喜欢')]
        during_review = at_review + [dict(message_id='c', text='那首歌也不错')]
        from unittest.mock import Mock
        self.choose.messages = Mock(side_effect=[at_review, during_review, during_review+[dict(message_id='d',text='同意')]])
        self.policy.judge.side_effect = [json.dumps(self.review()), json.dumps(dict(send=True, confidence=.99))]
        record = {}
        selected = await self.choose.choose(scope, NS(system_prompt='群友'), initial, record)
        self.assertIsNotNone(selected)
        self.assertEqual(self.choose.messages.call_count, 2)
        self.assertEqual(record['validated_through'][-1], 'c')
        self.assertEqual(self.policy.judge.call_args_list[0].args[2]['messages'], at_review)

    async def test_topic_change_during_review_cancels(self):
        scope = self.setup_background()
        self.mock_candidates()
        from unittest.mock import Mock
        self.choose.messages = Mock(side_effect=[[], [dict(message_id='new', text='换话题，来讨论作业')]])
        self.policy.judge.side_effect = [json.dumps(self.review()), json.dumps(dict(send=False, confidence=.99))]
        record = {}
        self.assertIsNone(await self.choose.choose(scope, NS(system_prompt='群友'), [], record))
        self.assertIn('话题', record['reason'])

    async def test_stop_or_direct_task_preempts_generation(self):
        scope = self.setup_background()
        self.mock_candidates()
        entered = asyncio.Event()
        async def generating(*args):
            entered.set()
            await asyncio.sleep(10)
        self.choose.generate.side_effect = generating
        event = self.event()
        self.learning.opportunities.take(event, dict(message_count=1, reply_probability=100))
        task = asyncio.create_task(self.send_candidate(event))
        await asyncio.wait_for(entered.wait(), 3)
        direct = self.event(text='帮我解决这个问题')
        self.choose.start_foreground(direct)
        await task
        event.send.assert_not_awaited()
        self.assertEqual(self.learning.opportunities.state(scope)['status'], 'idle')
        self.assertTrue(self.choose.foreground_busy(scope))
        self.choose.finish_foreground(direct)
        self.assertFalse(self.choose.foreground_busy(scope))

    async def test_partial_delivery_never_retries(self):
        scope = self.setup_background()
        self.mock_candidates()
        event = self.event()
        async def send(chain):
            self.learning.sent(event, 'delivered', [dict(type='text', data=dict(text=chain.chain[0].text))])
            raise RuntimeError('delivery completion unknown')
        event.send.side_effect = send
        self.learning.opportunities.take(event, dict(message_count=1, reply_probability=100))
        await self.send_candidate(event)
        self.assertEqual(self.learning.opportunities.state(scope)['status'], 'idle')
        row = self.learning.db.execute('SELECT * FROM replies').fetchone()
        self.assertIsNotNone(row['sent'])
        self.assertFalse(row['generated'])

    async def test_confirmed_used_ids_are_subset_and_not_all_references(self):
        scope = self.setup_background()
        self.mock_candidates()
        self.bg.context.return_value = dict(expressions=[dict(id='e1', scene='音乐', style='唱反调', boundary='适度'), dict(id='e2', scene='音乐', style='提问', boundary='适度')])
        for candidate in self.choose.generate.return_value['items']:
            candidate['expression_ids'] = ['e1']
        review = self.review()
        for item in review['items']:
            item['used_expression_ids'] = ['e1', 'e2', 'invented']
        self.policy.judge.return_value = json.dumps(review)
        selected = await self.choose.choose(scope, NS(system_prompt='群友'), [], {})
        self.assertEqual(selected['used_expression_ids'], ['e1'])

    async def test_candidate_image_feedback_updates_once_without_bandit(self):
        scope = self.setup_background()
        self.mock_candidates()
        sticker, _ = self.image()
        self.stickers.ranked_pool.return_value = [dict(id=sticker, description='捂脸猫')]
        for candidate in self.choose.generate.return_value['items']:
            candidate['sticker_id'] = sticker
        event = self.event()
        await self.send_candidate(event)
        with self.learning.db:
            self.learning.db.execute("UPDATE replies SET status='settled',outcome='feedback',score=.5,summary='{}'")
        self.policy.learn(scope)
        self.policy.learn(scope)
        row = self.learning.db.execute('SELECT reward_n,reward_sum FROM stickers WHERE id=?', (sticker,)).fetchone()
        self.assertEqual(tuple(row), (1, .25))
        self.assertEqual(self.policy.state(scope)['arms'], {})

    async def test_negative_only_suspends_used_and_attributed_patterns(self):
        scope = self.setup_background()
        self.mock_candidates()
        self.bg.context.return_value = dict(expressions=[dict(id='e1', scene='音乐', style='唱反调', boundary='适度')])
        for candidate in self.choose.generate.return_value['items']:
            candidate['expression_ids'] = ['e1']
        review = self.review()
        for item in review['items']:
            item['used_expression_ids'] = ['e1']
        self.policy.judge.return_value = json.dumps(review)
        state = self.policy.state(scope)
        state['expressions'] = [dict(id=i, status='active', scene='音乐', style='表达', boundary='适度', evidence=[])
                                for i in ('e1', 'e2')]
        self.policy.save(scope, state)
        event = self.event()
        await self.send_candidate(event)
        rid = event.get_extra('qq_feedback_reply')
        from test_feedback_learning import Event
        reaction = Event('reaction', sender='bob', text='别老是唱反调了', parent='sent-'+str(self.number))
        self.learning.observe(reaction)
        batch = self.learning.batch(*scope)
        judgment = dict(message_id='reaction', reply_id=rid, intent='annoyance', attribution_confidence=.99,
            intent_confidence=.99, reason='明确反感唱反调', cause='expression', cause_confidence=.99,
            evidence_text='别老是唱反调了', affected_expression_ids=['e1', 'e2'], affected_lesson_ids=[])
        batch['items'] = [item for item in batch['items'] if item['message_id']=='reaction']
        self.assertEqual(len(batch['items']), 1)
        self.learning.apply_judgments(batch, dict(items=[judgment]))
        self.learning.db.execute("UPDATE messages SET analyzed=1 WHERE message_id!='reaction'")
        self.learning.settle(now=time.time()+601)
        self.policy.learn(scope)
        entries = {e['id']:e for e in self.policy.state(scope)['expressions']}
        self.assertEqual(entries['e1']['status'], 'suspended')
        self.assertEqual(entries['e2']['status'], 'active')
        self.assertEqual(entries['e1']['counter_reply_ids'], [rid])

    async def test_clear_during_extraction_does_not_restore_private_material(self):
        scope = self.seed()
        async def extracted(*args, **kwargs):
            self.learning.clear_group(*scope)
            return json.dumps(self.extraction())
        self.policy.judge = AsyncMock(side_effect=extracted)
        await self.bg.habits(scope)
        self.assertEqual(self.learning.expressions.snapshot(scope)['items'], [])
        self.assertEqual(self.learning.expressions.snapshot(scope)['meta']['pending'], 0)

    async def test_explicit_stop_consumes_pending_retry(self):
        scope = self.setup_background()
        op = self.learning.opportunities
        event = self.event()
        op.take(event, dict(message_count=1, reply_probability=100))
        op.finish(event, technical=True)
        stopped = self.event(text='别说了')
        stopped.get_message_str = lambda: '别说了'
        self.choose.observe(stopped)
        self.assertTrue(stopped.get_extra('qq_suppress_auto'))
        self.assertEqual(op.state(scope)['status'], 'idle')

    async def test_clear_during_candidate_does_not_restore_records(self):
        scope = self.setup_background()
        self.mock_candidates()
        entered = asyncio.Event()
        async def generating(*args):
            entered.set()
            await asyncio.sleep(10)
        self.choose.generate.side_effect = generating
        event = self.event()
        self.learning.opportunities.take(event, dict(message_count=1, reply_probability=100))
        task = asyncio.create_task(self.send_candidate(event))
        await asyncio.wait_for(entered.wait(), 3)
        self.learning.clear_group(*scope)
        await task
        event.send.assert_not_awaited()
        for table in ('interaction_choices', 'reply_actions', 'replies', 'auto_reply_state'):
            self.assertEqual(self.learning.db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)

    async def test_expression_merge_and_capacity_prefer_supported_entries(self):
        scope = self.seed()
        state = self.policy.state(scope)
        state['expressions'] = [dict(id='e'+str(i), scene='场景'+str(i), style='表达', boundary='边界',
            evidence=[], status='active' if i else 'pending', last_supported=i) for i in range(50)]
        state['expression_meta'] = dict(next_id=49)
        self.policy.save(scope, state)
        self.policy.judge = AsyncMock(return_value=json.dumps(self.extraction()))
        await self.bg.habits(scope)
        entries = self.learning.expressions.snapshot(scope)['items']
        self.assertEqual(len(entries), 50)
        self.assertNotIn('e0', [e['id'] for e in entries])
        self.assertEqual(next(e for e in entries if e['id']=='e50')['status'], 'active')
        for i in range(10):
            self.event(sender='alice' if i % 2 else 'bob', text='继续音乐玩笑')
        state = self.policy.state(scope)
        state['expression_meta']['attempted'] = 0
        self.policy.save(scope, state)
        merged = self.extraction(match_id='e50')
        merged['items'][0]['message_ids'] = ['11', '20']
        self.policy.judge.return_value = json.dumps(merged)
        await self.bg.habits(scope)
        entries = self.learning.expressions.snapshot(scope)['items']
        self.assertEqual(len(entries), 50)
        self.assertEqual(len(next(e for e in entries if e['id']=='e50')['evidence']), 4)


if __name__ == '__main__':
    unittest.main()
