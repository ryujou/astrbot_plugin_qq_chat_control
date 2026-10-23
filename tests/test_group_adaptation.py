import asyncio
import base64
import contextvars
import io
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

import numpy as np
from PIL import Image as PILImage
from aiocqhttp import CQHttp
from astrbot.core.message.components import Image
from astrbot.core.message.message_event_result import MessageEventResult
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.provider.entities import LLMResponse

from astrbot_plugin_qq_chat_control.learning import FeedbackLearning
from astrbot_plugin_qq_chat_control.limits import LLMLimits, MessageUsage, ChatLimitExceeded, current_message
from astrbot_plugin_qq_chat_control.strategy import DIM
from test_feedback_learning import Event


class AdaptationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.rules = {('qq_private', group): dict(persona_id='同一个人设', feedback_observe=True,
                       slang_learning=False, feedback_adapt_mode='学习', sticker_learning=True,
                       group_background=False, candidate_replies=False,
                       feedback_judge_provider_id='judge') for group in ['10006', '10003']}
        self.embedding = NS(get_embedding=AsyncMock(return_value=[1., 0.]))
        self.judge = NS(text_chat=AsyncMock(return_value=LLMResponse(role='assistant', completion_text='{"items":[]}')),
                        bind_origin=Mock(), chat_only_context=contextvars.ContextVar('test_judge_tools', default=False))
        self.owner = NS(closing=False, rules=self.rules, tasks=set(),
            config=dict(allowed_group_ids=['10006', '10003'], allowed_qq_ids=['alice'],
                        default_provider_id='chat', member_limit_count=2, learning_embedding_provider_id='test_embedding'),
            context=NS(get_config=lambda _: {'wake_prefix': ['/']}, get_provider_by_id=lambda _: self.embedding,
                       conversation_manager=NS(update_conversation=AsyncMock())),
            rule=lambda event: self.rules.get((event.get_platform_id(), event.get_group_id()), {}),
            provider=lambda name: self.judge)
        self.owner.limits = LLMLimits(self.owner, Path(self.tmp.name) / 'limits.json')
        self.learning = FeedbackLearning(self.owner, Path(self.tmp.name) / 'learning.db')
        self.owner.learning = self.learning
        self.policy = self.learning.policy
        self.stickers = self.policy.stickers
        self.learning.open()
        self.addCleanup(self.learning.close)
        self.number = 0

    def event(self, group='10006', sender='alice', text='这下绷不住了'):
        self.number += 1
        event = Event(str(self.number), group=group, sender=sender, text=text)
        event.stop_event, event.should_call_llm = Mock(), Mock()
        event.is_at_or_wake_command = True
        event.plain_result = lambda text: MessageEventResult().message(text)

        async def send(chain):
            parts = [{'type': 'image' if isinstance(part, Image) else 'text', 'data': {'text': getattr(part, 'text', '')}}
                     for part in chain.chain]
            self.learning.sent(event, 'sent-' + str(self.number), parts)
        event.send = AsyncMock(side_effect=send)
        self.learning.observe(event)
        return event

    @contextmanager
    def bound(self, event):
        usage = MessageUsage(event)
        token = current_message.set(usage)
        try:
            yield usage
        finally:
            current_message.reset(token)

    def action(self, kind='text', sticker=None, phase='adaptive'):
        return dict(style='顺势接梗', format=kind, sticker_id=sticker,
                    arm='顺势接梗:' + kind if phase == 'adaptive' else 'baseline:text', phase=phase)

    async def choose(self, event, action=None):
        request = NS(prompt='这下绷不住了', conversation=NS(history='[]', cid='conversation'))
        action = action or self.action()
        original_judge = self.policy.judge
        async def judge(scope, system, payload, *args, **kwargs):
            if system.startswith('判断本轮QQ群回复场景'):
                return json.dumps(dict(scene='casual', confidence=.95, wants_sticker=False, items=[dict(id=action['sticker_id'], confidence=.95)] if action.get('sticker_id') else []))
            return await original_judge(scope, system, payload, *args, **kwargs)
        with self.bound(event):
            self.learning.prepare(event)
            with patch.object(self.policy, 'select', return_value=(action, [action], 1.)), \
                 patch.object(self.policy, 'judge', AsyncMock(side_effect=judge)):
                return await self.policy.prepare(event, request)

    def image(self, group='10006', status='ready'):
        directory = self.stickers.root / 'qq_private' / group
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / 'test.gif'
        a, b = PILImage.new('RGB', (12, 12), 'red'), PILImage.new('RGB', (12, 12), 'blue')
        a.save(path, save_all=True, append_images=[b], duration=100, loop=0)
        with self.learning.db:
            cursor = self.learning.db.execute('INSERT INTO stickers '
                '(platform,group_id,persona,source_file,source_message,url,context,created,status,path,description,visible_text,tags,scene,embedding,embedding_provider) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('qq_private', group, '同一个人设', 'opaque-qq-file', 'image-source', 'https://example.invalid/image', '[]',
                 1000, status, str(path), '猫猫捂脸', '绷不住了', '["无语"]', '接吐槽', '[1,0]', 'test_embedding'))
        return cursor.lastrowid, path

    async def settled(self, group='10006', score=.25, outcome='feedback', correction=0, phase='adaptive'):
        event = self.event(group=group)
        await self.choose(event, self.action(phase=phase))
        self.learning.generated(event)
        self.learning.sent(event, 'out-' + str(self.number), [{'type': 'text', 'data': {'text': '自然接话'}}])
        self.learning.finish(event)
        summary = dict(new_participants=1, annoyance_people=0, correction_people=correction)
        with self.learning.db:
            self.learning.db.execute("UPDATE replies SET status='settled',score=?,outcome=?,summary=? WHERE id=?",
                (score, outcome, json.dumps(summary), event.get_extra('qq_feedback_reply')))
        if outcome == 'feedback' or correction:
            message_id = 'feedback-' + str(self.number)
            self.learning.observe(Event(message_id, group=group, sender='bob', text='确实有意思' if not correction else '这句话理解错了'))
            with self.learning.db:
                self.learning.db.execute('INSERT INTO feedback(platform,group_id,message_id,reply_id,intent,weight) VALUES (?,?,?,?,?,?)',
                    ('qq_private', group, message_id, event.get_extra('qq_feedback_reply'), 'correction' if correction else 'positive', 1))
        return event.get_extra('qq_feedback_reply')

    async def test_pure_sticker_stops_native_llm_and_records_real_delivery(self):
        sticker, path = self.image()
        self.judge.text_chat.return_value = LLMResponse(role='assistant', completion_text=json.dumps({'items': [{'id': sticker, 'confidence': .95}]}))
        event = self.event()
        self.assertTrue(await self.choose(event, self.action('sticker', sticker)))
        event.stop_event.assert_called_once()
        event.should_call_llm.assert_called_with(True)
        self.assertIsInstance(event.send.await_args.args[0].chain[0], Image)
        self.learning.finish(event)
        row = self.learning.db.execute('SELECT * FROM replies WHERE id=?', (event.get_extra('qq_feedback_reply'),)).fetchone()
        self.assertEqual((row['generated'], row['finished'], row['status']), (1, 1, 'observing'))
        self.assertIn('猫猫捂脸', row['body'])
        self.owner.context.conversation_manager.update_conversation.assert_awaited_once()
        self.assertEqual(self.learning.db.execute('SELECT uses FROM stickers WHERE id=?', (sticker,)).fetchone()[0], 1)

    async def test_native_onebot_image_send_receipt_is_one_logical_reply(self):
        sticker, _ = self.image()
        event = self.event()
        api = NS(call_action=AsyncMock(return_value={'message_id': 123}))
        bot = object.__new__(CQHttp)
        bot._api = api

        async def native_send(chain):
            await AiocqhttpMessageEvent.send_message(bot=bot, message_chain=chain,
                event=event.message_obj.raw_message, is_group=True, session_id=event.get_group_id())
        event.send.side_effect = native_send
        self.learning.install()
        with patch.object(self.stickers, 'ranked_pool', AsyncMock(return_value=[dict(id=sticker, description='猫猫捂脸')])):
            self.assertTrue(await self.choose(event, self.action('sticker', sticker)))
        api.call_action.assert_awaited_once()
        self.assertEqual(self.learning.db.execute('SELECT message_id FROM outgoing').fetchone()[0], '123')
        self.assertEqual(self.learning.db.execute('SELECT COUNT(*) FROM replies').fetchone()[0], 1)
        parts = api.call_action.await_args.kwargs['message']
        self.assertEqual([p['type'] for p in parts], ['image'])

    async def test_work_persona_private_and_disabled_modes_do_not_adapt(self):
        scope = 'qq_private', '10006'
        for group, persona, mode in [('10006', '工作群', '学习'), ('', '同一个人设', '学习'),
                                      ('10006', '同一个人设', '关闭')]:
            self.rules[scope].update(persona_id=persona, feedback_adapt_mode=mode)
            event = self.event(group=group)
            self.assertFalse(await self.choose(event))
            self.assertIsNone(event.get_extra('qq_learning_action'))
        self.judge.text_chat.assert_not_awaited()
        self.embedding.get_embedding.assert_not_awaited()

    async def test_quota_is_checked_before_retrieval_or_image_send(self):
        event = self.event(sender='bob')
        record = self.owner.limits.reserve('qq_private:10006:bob')
        record.update(used=2, pending=0)
        with self.bound(event):
            self.learning.prepare(event)
            with self.assertRaises(ChatLimitExceeded):
                await self.policy.prepare(event, NS(prompt='test', conversation=None))
        self.embedding.get_embedding.assert_not_awaited()
        # The only emitted text is the existing quota reset notice.
        self.assertEqual(event.send.await_count, 1)

    async def test_learning_groups_share_images_across_personas_and_keep_reply_scope(self):
        sticker, _ = self.image()
        self.rules['qq_private', '10003']['persona_id'] = '另一个人设'
        self.judge.text_chat.return_value = LLMResponse(role='assistant',
            completion_text=json.dumps({'items': [{'id': sticker, 'confidence': .95}]}))
        found = await self.stickers.candidates('qq_private', '10003', '绷不住了')
        self.assertEqual([r['id'] for r in found], [sticker])
        payload = json.loads(self.judge.text_chat.await_args.kwargs['prompt'])
        self.assertNotIn('context', payload['candidates'][0])
        self.assertNotIn('group_id', payload['candidates'][0])
        other = self.event(group='10003')
        self.assertTrue(await self.choose(other, self.action('sticker', sticker)))
        other.send.assert_awaited_once()
        self.assertEqual(tuple(self.learning.db.execute('SELECT group_id,persona FROM replies').fetchone()),
                         ('10003', '另一个人设'))

    async def test_inactive_sources_and_destinations_do_not_share_images(self):
        sticker, _ = self.image()
        original = self.rules['qq_private', '10006'].copy()
        for change in [dict(sticker_learning=False), dict(feedback_observe=False),
                       dict(feedback_adapt_mode='关闭'), dict(persona_id='工作群'), dict(persona_id='更换后人设')]:
            with self.subTest(change=change):
                self.rules['qq_private', '10006'] = dict(original, **change)
                self.assertEqual(await self.stickers.candidates('qq_private', '10003', '绷不住了'), [])
                with self.assertRaises(ValueError):
                    self.stickers.get('qq_private', '10003', sticker)
        self.rules['qq_private', '10006'] = original
        self.owner.config['allowed_group_ids'] = ['10003']
        self.assertEqual(await self.stickers.candidates('qq_private', '10003', '绷不住了'), [])
        self.owner.config['allowed_group_ids'] = ['10003', '10006']
        self.rules['qq_private', '10003']['sticker_learning'] = False
        other = self.event(group='10003')
        with self.assertRaises(ValueError):
            await self.stickers.send(other, sticker)
        other.send.assert_not_awaited()
        self.judge.text_chat.assert_not_awaited()

    async def test_shared_caption_updates_image_reward_but_only_destination_policy(self):
        sticker, path = self.image()
        other = self.event(group='10003')
        await self.choose(other, self.action('caption', sticker))
        kwargs = self.policy.inject(other, {'prompt': '绷不住了', 'system_prompt': '人设'})
        self.assertEqual(kwargs['image_urls'], [str(path)])
        await self.policy.deliver(other)
        other.send.assert_awaited_once()
        reply_id = other.get_extra('qq_feedback_reply')
        with self.learning.db:
            self.learning.db.execute("UPDATE replies SET status='settled',score=.5,outcome='feedback',summary='{}' WHERE id=?",
                                     (reply_id,))
        self.policy.learn(('qq_private', '10003'))
        row = self.learning.db.execute('SELECT uses,reward_sum,reward_n FROM stickers WHERE id=?', (sticker,)).fetchone()
        self.assertEqual(tuple(row), (1, .25, 1))
        self.assertEqual(self.policy.state(('qq_private', '10006'))['arms'], {})
        self.assertEqual(self.policy.state(('qq_private', '10003'))['arms']['顺势接梗:caption']['n'], 1)

    async def test_incoming_image_uses_are_deduplicated_and_separated_by_group(self):
        sticker, _ = self.image()
        event = self.event()
        event.message_obj.raw_message['message'] = [dict(type='image', data=dict(file='opaque-qq-file', url='https://example.invalid/image'))]
        self.stickers.observe(event)
        self.stickers.observe(event)
        self.assertEqual(self.learning.db.execute('SELECT seen FROM stickers WHERE id=?', (sticker,)).fetchone()[0], 2)
        other = self.event(group='10003')
        other.message_obj.raw_message['message'] = event.message_obj.raw_message['message']
        self.stickers.observe(other)
        self.assertEqual(self.learning.db.execute('SELECT COUNT(*) FROM stickers').fetchone()[0], 2)
        self.assertEqual(self.learning.db.execute('SELECT COUNT(*) FROM sticker_occurrences').fetchone()[0], 2)

    async def test_known_image_reaction_is_judged_from_description_and_own_group_context(self):
        sticker, _ = self.image()
        source = self.event()
        await self.choose(source)
        self.learning.generated(source)
        self.learning.sent(source, 'bot-response', [dict(type='text', data=dict(text='这下绷不住了'))])
        self.learning.finish(source)
        reaction = self.event(sender='bob', text='')
        reaction.message_obj.raw_message['message'] = [dict(type='image', data=dict(file='opaque-qq-file', url='https://example.invalid/image'))]
        self.stickers.observe(reaction)
        batch = self.learning.batch('qq_private', '10006')
        target = next(item for item in batch['items'] if item['message_id'] == reaction.message_obj.raw_message['message_id'])
        self.assertEqual(target['image_descriptions'], ['猫猫捂脸'])
        self.assertIn('可见内容', target['text'])
        self.assertEqual(self.stickers.descriptions('qq_private', '10003', target['message_id']), [])

    async def test_repeated_real_uses_refresh_image_scene_using_original_local_gif(self):
        sticker, path = self.image()
        with self.learning.db:
            self.learning.db.execute('UPDATE stickers SET seen=6,described_seen=1 WHERE id=?', (sticker,))
        description = dict(is_sticker=True, confidence=.95, description='猫猫捂脸', visible_text='绷不住了', tags=['无语'], scene='本群用于吐槽离谱技术方案')
        self.judge.text_chat.return_value = LLMResponse(role='assistant', completion_text=json.dumps(description, ensure_ascii=False))
        with patch('astrbot_plugin_qq_chat_control.stickers.resolve_media_ref_to_base64_data', AsyncMock()) as network:
            await self.stickers.tick('qq_private', '10006')
        network.assert_not_awaited()
        row = self.learning.db.execute('SELECT scene,described_seen,status FROM stickers WHERE id=?', (sticker,)).fetchone()
        self.assertEqual(tuple(row), (description['scene'], 6, 'ready'))
        self.assertTrue(path.exists())

    async def test_semantic_retrieval_requires_judge_approval_of_real_ids(self):
        sticker, _ = self.image()
        self.judge.text_chat.return_value = LLMResponse(role='assistant', completion_text='{"items":[{"id":99999,"confidence":1}]}')
        self.assertEqual(await self.stickers.candidates('qq_private', '10006', '绷不住了'), [])
        self.judge.text_chat.return_value = LLMResponse(role='assistant', completion_text=json.dumps({'items': [{'id': sticker, 'confidence': .95}]}))
        found = await self.stickers.candidates('qq_private', '10006', '绷不住了')
        self.assertEqual([r['id'] for r in found], [sticker])
        self.assertFalse(self.judge.chat_only_context.get())

    async def test_semantic_similarity_ranks_candidates_but_judge_decides_suitability(self):
        sticker, _ = self.image()
        with self.learning.db:
            self.learning.db.execute('UPDATE stickers SET embedding=? WHERE id=?',
                (json.dumps([.44, float(np.sqrt(1 - .44 ** 2))]), sticker))
        self.judge.text_chat.return_value = LLMResponse(role='assistant',
            completion_text=json.dumps({'items': [{'id': sticker, 'confidence': .95}]}))
        found = await self.stickers.candidates('qq_private', '10006', '没看懂')
        self.assertEqual([r['id'] for r in found], [sticker])
        payload = json.loads(self.judge.text_chat.await_args.kwargs['prompt'])
        self.assertAlmostEqual(payload['candidates'][0]['similarity'], .44)
        self.judge.text_chat.return_value = LLMResponse(role='assistant', completion_text='{"items":[]}')
        self.assertEqual(await self.stickers.candidates('qq_private', '10006', '没看懂'), [])

    async def test_gif_description_uses_frames_and_preserves_original_animation(self):
        sticker, source = self.image(status='pending')
        original = source.read_bytes()
        media = NS(to_data_url=lambda: 'data:image/gif;base64,' + base64.b64encode(original).decode())
        description = dict(is_sticker=True, confidence=.95, description='猫猫捂脸', visible_text='绷不住了', tags=['无语'], scene='接吐槽')
        self.judge.text_chat.return_value = LLMResponse(role='assistant', completion_text=json.dumps(description, ensure_ascii=False))
        row = dict(self.learning.db.execute('SELECT * FROM stickers WHERE id=?', (sticker,)).fetchone())
        with patch('astrbot_plugin_qq_chat_control.stickers.resolve_media_ref_to_base64_data', AsyncMock(return_value=media)):
            await self.stickers.describe(row)
        ready = self.learning.db.execute('SELECT * FROM stickers WHERE id=?', (sticker,)).fetchone()
        self.assertEqual(ready['status'], 'ready')
        self.assertEqual(Path(ready['path']).read_bytes(), original)
        with PILImage.open(ready['path']) as image:
            self.assertEqual(image.n_frames, 2)
        self.assertEqual(len(self.judge.text_chat.await_args.kwargs['image_urls']), 2)
        self.assertEqual(ready['url'], '')

    async def test_ordinary_photo_is_excluded_without_semantic_indexing(self):
        sticker, source = self.image(status='pending')
        media = NS(to_data_url=lambda: 'data:image/gif;base64,' + base64.b64encode(source.read_bytes()).decode())
        self.judge.text_chat.return_value = LLMResponse(role='assistant', completion_text='{"is_sticker":false,"confidence":0.99}')
        row = dict(self.learning.db.execute('SELECT * FROM stickers WHERE id=?', (sticker,)).fetchone())
        with patch('astrbot_plugin_qq_chat_control.stickers.resolve_media_ref_to_base64_data', AsyncMock(return_value=media)):
            await self.stickers.describe(row)
        self.assertEqual(self.learning.db.execute('SELECT status FROM stickers WHERE id=?', (sticker,)).fetchone()[0], 'rejected')
        self.embedding.get_embedding.assert_not_awaited()

    async def test_caption_is_sent_once_after_nested_stream_completes(self):
        sticker, _ = self.image()
        event = self.event()
        self.judge.text_chat.return_value = LLMResponse(role='assistant', completion_text=json.dumps({'items': [{'id': sticker, 'confidence': .95}]}))
        await self.choose(event, self.action('caption', sticker))
        result = LLMResponse(role='assistant', completion_text='这下绷不住了')
        provider = NS(text_chat=AsyncMock(return_value=result))
        original = provider.text_chat

        async def stream(**kwargs):
            yield await provider.text_chat(**kwargs)
        provider.text_chat_stream = stream
        self.owner.profiles = NS(inject=lambda e, kw: kw, replied=Mock())
        self.owner.limits.wrap_provider(provider)
        with self.bound(event):
            responses = [r async for r in provider.text_chat_stream(prompt='test', system_prompt='基础人设')]
        self.assertEqual(responses[0].completion_text, '这下绷不住了')
        event.send.assert_awaited_once()
        prompt = original.await_args.kwargs
        self.assertEqual(prompt['system_prompt'].count('本群本轮表达策略与经验'), 1)
        self.assertEqual(len(prompt['image_urls']), 1)

    async def test_image_send_failure_is_not_retried_or_trained_as_dislike(self):
        sticker, _ = self.image()
        event = self.event()
        self.judge.text_chat.return_value = LLMResponse(role='assistant', completion_text=json.dumps({'items': [{'id': sticker, 'confidence': .95}]}))
        await self.choose(event, self.action('caption', sticker))
        event.send.side_effect = RuntimeError('onebot send failed')
        with self.assertLogs('astrbot', level='ERROR'):
            await self.policy.deliver(event)
        await self.policy.deliver(event)
        event.send.assert_awaited_once()
        self.assertGreater(self.learning.db.execute('SELECT send_errors FROM replies WHERE id=?', (event.get_extra('qq_feedback_reply'),)).fetchone()[0], 0)

    async def test_linucb_learns_once_and_reload_preserves_per_group_parameters(self):
        await self.settled()
        self.policy.learn(('qq_private', '10006'))
        first = self.policy.state(('qq_private', '10006'))
        self.policy.learn(('qq_private', '10006'))
        self.assertEqual(first, self.policy.state(('qq_private', '10006')))
        self.assertEqual(first['arms']['顺势接梗:text']['n'], 1)
        self.assertEqual(self.policy.state(('qq_private', '10003'))['arms'], {})
        self.learning.close()
        self.learning.open()
        self.assertEqual(first, self.policy.state(('qq_private', '10006')))

    async def test_corrections_prevent_positive_reinforcement_and_incomplete_is_ignored(self):
        await self.settled(score=.5, correction=1)
        await self.settled(score=None, outcome='partial_delivery')
        self.policy.learn(('qq_private', '10006'))
        arm = self.policy.state(('qq_private', '10006'))['arms']['顺势接梗:text']
        self.assertEqual(arm['n'], 1)
        self.assertLess(arm['b'][0], 0)
        await self.settled(score=-1., correction=1)
        self.policy.learn(('qq_private', '10006'))
        arm = self.policy.state(('qq_private', '10006'))['arms']['顺势接梗:text']
        self.assertAlmostEqual(arm['b'][0], -1.25)

    async def test_unexposed_failed_generation_is_not_kept_as_training_example(self):
        event = self.event()
        await self.choose(event)
        self.learning.finish(event)
        self.assertEqual(self.learning.db.execute('SELECT COUNT(*) FROM reply_actions').fetchone()[0], 0)

    async def test_image_pool_size_does_not_change_format_probability(self):
        totals = []
        for count in (1, 2):
            _, candidates, _ = self.policy.select(('qq_private', '10006'), [1.] * DIM,
                [dict(id=i) for i in range(count)], False, 1800)
            totals.append({kind: sum(a['probability'] for a in candidates if a['format'] == kind)
                           for kind in ('text', 'sticker', 'caption')})
            self.assertAlmostEqual(sum(a['probability'] for a in candidates), 1)
            for a in candidates:
                self.assertAlmostEqual(a['probability'], a['arm_probability'] * a['image_probability'])
        for kind in totals[0]:
            self.assertAlmostEqual(totals[0][kind], totals[1][kind])

    async def test_serious_sticker_request_keeps_text_candidate(self):
        action, candidates, _ = self.policy.select(('qq_private', '10006'), [1.] * DIM,
                                                   [dict(id=1)], True, 1800, wants_sticker=True)
        self.assertEqual(len(candidates), 1)
        self.assertEqual((action['style'], action['format']), ('认真接话', 'text'))

    async def test_semantic_scene_controls_candidates_and_skips_image_lookup(self):
        event = self.event(text='最近真的撑不下去了，给张表情包吧')
        with self.bound(event):
            self.learning.prepare(event)
            with patch.object(self.policy, 'judge', AsyncMock(return_value='{"scene":"serious","confidence":0.99,"wants_sticker":true}')), \
                 patch.object(self.stickers, 'candidates', AsyncMock()) as lookup:
                await self.policy.prepare(event, NS(conversation=None))
        lookup.assert_not_awaited()
        action = event.get_extra('qq_learning_action')
        self.assertEqual((action['style'], action['format']), ('认真接话', 'text'))

    async def test_scene_failure_is_recorded_and_not_trained(self):
        event = self.event()
        with self.bound(event):
            self.learning.prepare(event)
            with patch.object(self.policy, 'judge', AsyncMock(side_effect=RuntimeError('offline'))), \
                 self.assertLogs('astrbot', level='ERROR'):
                await self.policy.prepare(event, NS(conversation=None))
        self.assertEqual(event.get_extra('qq_learning_action')['phase'], 'scene_failed')
        self.learning.db.execute("UPDATE replies SET status='settled',score=1,outcome='feedback',summary='{}'")
        self.policy.learn(('qq_private', '10006'))
        self.assertEqual(self.policy.state(('qq_private', '10006'))['arms'], {})

    async def test_scene_and_image_selection_share_one_model_call(self):
        sticker, _ = self.image()
        event = self.event()
        self.judge.text_chat.return_value.completion_text = json.dumps(dict(
            scene='casual', confidence=.98, wants_sticker=False, should_reply=True,
            items=[dict(id=sticker, confidence=.96), dict(id=999999, confidence=.99)]))
        with self.bound(event):
            self.learning.prepare(event)
            with patch.object(self.policy, 'select', return_value=(self.action(), [self.action()], 1.)) as select:
                await self.policy.prepare(event, NS(conversation=None))
        self.judge.text_chat.assert_awaited_once()
        self.assertEqual([s['id'] for s in select.call_args.args[2]], [sticker])
        self.assertIn('candidates', json.loads(self.judge.text_chat.call_args.kwargs['prompt']))

    async def test_auto_can_stay_silent_but_explicit_request_is_not_suppressed(self):
        for automatic in (True, False):
            event = self.event()
            event.set_extra('qq_chat_automatic', automatic)
            with self.bound(event):
                self.learning.prepare(event)
                rid = event.get_extra('qq_feedback_reply')
                with patch.object(self.policy, 'judge', AsyncMock(return_value=json.dumps(dict(
                    scene='casual', confidence=.98, wants_sticker=False, should_reply=False, items=[])))):
                    suppressed = await self.policy.prepare(event, NS(conversation=None))
            self.assertEqual(suppressed, automatic)
            exists = self.learning.db.execute('SELECT id FROM replies WHERE id=?', (rid,)).fetchone()
            self.assertEqual(exists is None, automatic)
            event.send.assert_not_awaited()

    async def test_image_description_yields_to_active_reply(self):
        self.image(status='pending')
        running = Mock(done=lambda: False)
        self.owner.limits.tasks.add(running)
        try:
            with patch.object(self.stickers, 'describe', AsyncMock()) as describe:
                await self.stickers.tick('qq_private', '10006')
                describe.assert_not_awaited()
        finally:
            self.owner.limits.tasks.discard(running)

    async def test_codex_recheck_requires_max_and_uses_chat_only_context(self):
        self.judge.provider_config = {'思考强度': 'max'}
        self.judge.get_model = lambda: 'gpt-6.1-sol'
        async def response(**kwargs):
            self.assertTrue(self.judge.chat_only_context.get())
            self.assertIsNone(current_message.get())
            self.assertNotIn('func_tool', kwargs)
            return LLMResponse(role='assistant', completion_text='{}')
        self.judge.text_chat.side_effect = response
        await self.policy.judge(('qq_private', '10006'), 'test', {}, recheck=True)
        self.assertFalse(self.judge.chat_only_context.get())
        self.judge.provider_config['思考强度'] = 'low'
        with self.assertRaises(ValueError):
            await self.policy.judge(('qq_private', '10006'), 'test', {}, recheck=True)

    async def test_unknown_cases_do_not_trigger_experience_review(self):
        for _ in range(8):
            await self.settled(score=None, outcome='uncertain')
        with patch.object(self.policy, 'judge', AsyncMock()) as judge:
            await self.policy.review(('qq_private', '10006'))
        judge.assert_not_awaited()

    async def test_control_period_keeps_original_prompt_and_has_probability_one(self):
        scope = 'qq_private', '10006'
        self.rules[scope]['feedback_adapt_mode'] = '对照'
        action, candidates, probability = self.policy.select(scope, [1.] * DIM, [], False, 1800)
        self.assertEqual(action['phase'], 'baseline')
        self.assertEqual(probability, 1)
        self.assertEqual(len(candidates), 1)
        event = self.event()
        event.set_extra('qq_learning_action', action)
        kwargs = {'system_prompt': '原始人设', 'prompt': 'test'}
        self.assertIs(self.policy.inject(event, kwargs), kwargs)

    async def test_exploration_logs_exact_conditional_selection_probability(self):
        with patch('astrbot_plugin_qq_chat_control.strategy.random.random', return_value=0), \
             patch('astrbot_plugin_qq_chat_control.strategy.random.randrange', return_value=2):
            action, candidates, probability = self.policy.select(('qq_private', '10006'), [1.] * DIM, [], False, 1800)
        self.assertEqual(action, candidates[2])
        self.assertAlmostEqual(probability, 1 / len(candidates))

    async def test_explicit_sticker_request_can_select_image_during_control_period(self):
        scope = 'qq_private', '10006'
        self.rules[scope]['feedback_adapt_mode'] = '对照'
        action, candidates, probability = self.policy.select(scope, [1.] * DIM, [{'id': 1}], False, 1800, wants_sticker=True)
        self.assertEqual(action['phase'], 'requested')
        self.assertNotEqual(action['format'], 'text')
        self.assertTrue(all(a['format'] != 'text' for a in candidates))

    async def test_used_lesson_is_suspended_on_negative_evidence(self):
        scope = ('qq_private', '10006')
        state = self.policy.state(scope)
        state['lessons'] = [dict(scene='技术', clues=['代码'], advice='先吐槽再回答', reply_ids=[1,2])]
        self.policy.save(scope, state)
        reply_id = await self.settled(correction=1)
        action = self.policy.action_for(reply_id)
        action['lesson_advice'] = ['先吐槽再回答']
        self.learning.db.execute('UPDATE reply_actions SET action=? WHERE reply_id=?', (json.dumps(action), reply_id))
        self.policy.learn(scope)
        lesson = self.policy.state(scope)['lessons'][0]
        self.assertEqual(lesson['status'], 'suspended')
        self.assertEqual(lesson['counter_reply_ids'], [reply_id])
        event = self.event(text='代码报错')
        event.set_extra('qq_learning_action', self.action())
        result = self.policy.inject(event, {'prompt':'代码报错'})
        self.assertNotIn('先吐槽再回答', result['system_prompt'])

    async def test_counterexample_triggers_review_before_eight_new_cases(self):
        scope = ('qq_private', '10006')
        first = await self.settled()
        second = await self.settled(correction=1)
        state = self.policy.state(scope)
        state.update(review_cursor=first, lessons=[dict(scene='技术', clues=['代码'], advice='先吐槽再回答',
                     reply_ids=[first,second],status='suspended',counter_reply_ids=[second])])
        self.policy.save(scope,state)
        self.judge.text_chat.return_value = LLMResponse(role='assistant',completion_text=json.dumps(self.review_data([first,second])))
        await self.policy.review(scope)
        self.assertEqual(self.policy.state(scope)['reviews'],1)
        self.assertEqual(self.policy.state(scope)['lessons'][0]['status'],'active')

    async def test_review_rejects_cases_without_real_reactions(self):
        ids=[await self.settled() for _ in range(8)]
        self.learning.db.execute('DELETE FROM feedback')
        self.judge.text_chat.return_value=LLMResponse(role='assistant',completion_text=json.dumps(self.review_data(ids)))
        with self.assertLogs('astrbot',level='ERROR'):
            await self.policy.review(('qq_private','10006'))
        self.assertEqual(self.policy.state(('qq_private','10006'))['reviews'],0)

    async def test_suspended_lesson_cannot_return_without_counterexample(self):
        scope = ('qq_private', '10006')
        ids = [await self.settled() for _ in range(8)]
        state = self.policy.state(scope)
        state['lessons'] = [dict(scene='技术', clues=['代码'], advice='先吐槽再回答',
            reply_ids=ids[:2], status='suspended', counter_reply_ids=[ids[-1]])]
        self.policy.save(scope, state)
        self.judge.text_chat.return_value = LLMResponse(role='assistant', completion_text=json.dumps(self.review_data(ids)))
        with self.assertLogs('astrbot', level='ERROR'):
            await self.policy.review(scope)
        self.assertEqual(self.policy.state(scope)['lessons'][0]['status'], 'suspended')
        self.assertEqual(self.policy.state(scope)['reviews'], 0)

    async def test_reply_usage_does_not_cross_persona_change(self):
        event = self.event()
        await self.choose(event)
        self.rules['qq_private', '10006']['persona_id'] = '另一个人设'
        self.learning.record_reply_call(event, self.judge, 0, None, False)
        self.assertEqual(self.learning.db.execute('SELECT COUNT(*) FROM model_calls').fetchone()[0], 0)

    async def test_reply_stream_records_one_call_and_unknown_tokens(self):
        event=self.event()
        await self.choose(event)
        self.owner.profiles=NS(inject=lambda e,kw:kw,replied=Mock())
        async def stream(**kwargs):
            yield LLMResponse(role='assistant',completion_text='完成')
        provider=NS(text_chat=AsyncMock(),text_chat_stream=stream,provider_config={'id':'test'})
        self.owner.limits.wrap_provider(provider)
        with self.bound(event):
            result=[item async for item in provider.text_chat_stream(prompt='test')]
        row=self.learning.db.execute("SELECT * FROM model_calls WHERE purpose='reply'").fetchone()
        self.assertEqual(row['success'],1)
        self.assertIsNone(row['input_tokens'])
        self.assertEqual(self.learning.db.execute("SELECT COUNT(*) FROM model_calls WHERE purpose='reply'").fetchone()[0],1)

    def review_data(self, ids):
        return dict(lessons=[dict(scene='技术话题', clues=['代码'], advice='先接本轮具体问题，别硬套旧梗', reply_ids=ids[:2])],
                    style_bias=[dict(style='认真接话', delta=.05, reply_ids=ids[:2])])

    async def test_judge_updates_grounded_experience_without_changing_persona(self):
        ids = [await self.settled() for _ in range(8)]
        self.judge.text_chat.return_value = LLMResponse(role='assistant', completion_text=json.dumps(self.review_data(ids), ensure_ascii=False))
        await self.policy.review(('qq_private', '10006'))
        state = self.policy.state(('qq_private', '10006'))
        self.assertEqual(state['reviews'], 1)
        self.assertEqual(state['review_cursor'], max(ids))
        self.assertEqual(self.rules['qq_private', '10006']['persona_id'], '同一个人设')
        self.assertEqual(self.policy.state(('qq_private', '10003'))['lessons'], [])
        self.judge.text_chat.reset_mock()
        await self.policy.review(('qq_private', '10006'))
        self.judge.text_chat.assert_not_awaited()

    async def test_judge_cannot_cite_another_group_case_even_with_same_persona(self):
        ids = [await self.settled() for _ in range(8)]
        foreign = await self.settled(group='10003')
        self.judge.text_chat.return_value = LLMResponse(role='assistant', completion_text=json.dumps(self.review_data([ids[0], foreign]), ensure_ascii=False))
        with self.assertLogs('astrbot', level='ERROR'):
            await self.policy.review(('qq_private', '10006'))
        self.assertEqual(self.policy.state(('qq_private', '10006'))['reviews'], 0)

    async def test_review_preserves_training_that_finishes_while_judge_is_running(self):
        ids = [await self.settled() for _ in range(8)]

        async def infer(**kwargs):
            self.assertIsNone(current_message.get())
            self.assertTrue(self.judge.chat_only_context.get())
            self.policy.learn(('qq_private', '10006'))
            return LLMResponse(role='assistant', completion_text=json.dumps(self.review_data(ids), ensure_ascii=False))
        self.judge.text_chat.side_effect = infer
        await self.policy.review(('qq_private', '10006'))
        self.assertEqual(self.policy.state(('qq_private', '10006'))['arms']['顺势接梗:text']['n'], 8)

    async def test_clear_group_removes_only_its_gallery_policy_and_actions(self):
        await self.settled()
        await self.settled(group='10003')
        self.policy.learn(('qq_private', '10006'))
        first, path_a = self.image()
        second, path_b = self.image(group='10003')
        with self.learning.db:
            self.learning.db.execute("UPDATE reply_actions SET action=json_set(action,'$.sticker_id',?) WHERE group_id='10003'", (first,))
        self.learning.clear_group('qq_private', '10006')
        self.assertIsNone(self.learning.db.execute("SELECT json_extract(action,'$.sticker_id') FROM reply_actions WHERE group_id='10003'").fetchone()[0])
        self.assertFalse(path_a.exists())
        self.assertTrue(path_b.exists())
        self.assertEqual(self.learning.db.execute('SELECT COUNT(*) FROM reply_actions WHERE group_id=\'10003\'').fetchone()[0], 1)
        self.assertEqual(self.learning.db.execute('SELECT COUNT(*) FROM stickers').fetchone()[0], 1)
        self.assertEqual(self.policy.state(('qq_private', '10006'))['arms'], {})


if __name__ == '__main__':
    unittest.main()
