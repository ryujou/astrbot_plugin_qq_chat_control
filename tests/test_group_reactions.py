import asyncio
import json
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
import unittest

from aiocqhttp import CQHttp
from aiocqhttp.exceptions import ActionFailed
from quart import Quart

from astrbot_plugin_qq_chat_control.reactions import CATEGORIES, DECIDE
from astrbot_plugin_qq_chat_control.web import LearningWeb
from test_feedback_learning import Event
import test_group_adaptation as fixture


class ReactionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.AdaptationTests.asyncSetUp
    event = fixture.AdaptationTests.event
    bound = fixture.AdaptationTests.bound

    def setup_reactions(self):
        self.r = self.learning.reactions
        self.scope = ('qq_private', '10006')
        self.client = NS(call_action=AsyncMock(return_value={'group_id':'10006'}))
        self.learning.background.client = lambda _: self.client
        self.policy.judge = AsyncMock(return_value=json.dumps(dict(react=False, reason='语境不适合')))
        return self.event(text='我为了让风扇凉快给它也买了个风扇')

    def notice(self, mid='1', actor='alice', count=1, add=True, emoji='128053', group='10006'):
        event = Event('fake-notice-uuid', group=group, sender=actor)
        event.message_obj.message_id = 'fake-notice-uuid'
        event.message_obj.raw_message = dict(post_type='notice', notice_type='group_msg_emoji_like', group_id=group,
            message_id=mid, user_id=actor, is_add=add, likes=[dict(emoji_id=emoji, count=count)])
        self.r.observe_notice(event)
        return event

    def decision(self, event, emoji='128053', confidence=.95):
        self.policy.judge.return_value = json.dumps(dict(react=True, message_id=event.message_obj.raw_message['message_id'],
            emoji_id=emoji, categories=[CATEGORIES[1]], meaning='调侃荒诞操作', reason='给风扇再配风扇的反常操作',
            confidence=confidence, used_pattern_ids=[]))

    def actions(self):
        return self.r.action_snapshot(self.scope)['items']

    async def test_notice_uses_raw_id_and_does_not_become_chat(self):
        self.setup_reactions()
        event = self.notice()
        self.learning.observe(event)
        self.assertEqual(self.r.observations(self.scope)['items'][0]['message_id'], '1')
        self.assertEqual(self.learning.db.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 1)
        self.assertEqual(self.learning.db.execute('SELECT COUNT(*) FROM expression_pending').fetchone()[0], 0)
        self.assertFalse(self.r.active)
        self.assertFalse(self.learning.db.execute('SELECT 1 FROM outgoing').fetchone())

    async def test_silenced_target_excluded_and_rechecked_before_send(self):
        event = self.setup_reactions()
        self.rules[self.scope]['member_profiles'] = True
        record = dict(affinity=19, affinity_recovery_at=time.time())
        self.owner.profiles.records['qq_private:10006:alice'] = record
        await self.r.decide(self.scope, event)
        self.policy.judge.assert_not_awaited()
        self.client.call_action.assert_not_awaited()
        self.assertEqual(self.actions()[0]['status'], 'skipped')
        record['affinity'] = 20
        self.decision(event)
        async def lower_during_lookup(action, **params):
            record['affinity'] = 19
            return dict(group_id='10006')
        self.client.call_action.side_effect = lower_during_lookup
        await self.r.decide(self.scope, event)
        self.assertEqual(self.actions()[0]['status'], 'cancelled')
        self.assertEqual([c.args[0] for c in self.client.call_action.call_args_list], ['get_msg'])

    async def test_repeated_notice_withdrawal_unknown_and_bots(self):
        self.setup_reactions()
        self.notice(); self.notice()
        self.assertEqual(self.r.observations(self.scope)['emojis'][0]['messages'], 1)
        self.notice(actor='bob', count=2)
        self.assertEqual(self.r.observations(self.scope)['emojis'][0]['verified_people'], 2)
        self.notice(actor='alice', count=1, add=False)
        self.assertEqual(self.r.observations(self.scope)['items'][0]['actors'], ['bob'])
        self.notice(actor='', count=0, add=False)
        self.assertEqual(self.r.observations(self.scope)['emojis'], [])
        self.notice(actor='bot')
        self.assertEqual(self.r.observations(self.scope)['emojis'], [])
        self.notice(mid='unknown', actor='', count=3)
        self.assertEqual(self.r.observations(self.scope)['emojis'][0]['verified_people'], 0)

    async def test_missing_context_queried_once_tiny_id_is_not_qq(self):
        self.setup_reactions(); self.notice(mid='old', actor='')
        async def call(action, **params):
            return dict(group_id='10006', message=[]) if action=='get_msg' else dict(result=0, emojiLikesList=[dict(tinyId='opaque-1')])
        self.client.call_action.side_effect=call
        await self.r.tick(self.scope); await self.r.tick(self.scope)
        item=self.r.observations(self.scope)['items'][0]
        self.assertEqual(item['status'], 'missing')
        self.assertEqual(item['actors'], [])
        self.assertEqual(item['participants'], ['opaque-1'])
        self.assertEqual(self.client.call_action.await_count, 2)

    async def test_semantic_red_button_and_monkey_direct_send(self):
        event=self.setup_reactions()
        for emoji in ('424','128053'):
            event=self.event(text='大家以为只是客串，结果整个原班乐队突然登台了' if emoji=='424' else '为了避免迟到我决定不去上班')
            self.decision(event, emoji)
            await self.r.decide(self.scope, event)
            self.assertEqual(self.actions()[0]['status'], 'sent')
            self.client.call_action.assert_awaited_with('set_msg_emoji_like', message_id=str(self.number), emoji_id=emoji, set=True)
        self.assertFalse(self.learning.db.execute('SELECT 1 FROM auto_reply_state').fetchone())
        self.assertFalse(self.learning.db.execute('SELECT 1 FROM outgoing').fetchone())
        self.assertEqual(self.policy.judge.call_args.args[1], DECIDE)
        self.assertIn('不按关键词', DECIDE)
        self.assertLessEqual(len(self.policy.judge.call_args.args[2]['emojis']),12)

    async def test_none_low_confidence_invalid_pool_never_sends(self):
        event=self.setup_reactions()
        await self.r.decide(self.scope,event)
        self.decision(event,confidence=.84);await self.r.decide(self.scope,event)
        self.decision(event,emoji='not-in-pool');await self.r.decide(self.scope,event)
        self.client.call_action.assert_not_awaited()
        self.assertEqual({a['status'] for a in self.actions()}, {'skipped'})

    async def test_interval_capacity_and_reload(self):
        event=self.setup_reactions();self.decision(event)
        self.r.schedule(event)
        await self.r.active[self.scope];await asyncio.sleep(0)
        self.r.schedule(self.event());self.assertFalse(self.r.active)
        self.learning.db.execute('UPDATE reaction_actions SET created=created-121,sent=sent-121')
        event=self.event();self.decision(event);self.r.schedule(event)
        await self.r.active[self.scope];await asyncio.sleep(0)
        self.learning.close();self.learning.open()
        self.assertTrue(self.r.at_capacity(self.scope))
        self.assertTrue(self.r.already_reacted(self.scope,'1'))

    async def test_foreground_cancels_without_changing_text_counter(self):
        event=self.setup_reactions()
        self.learning.opportunities.save(self.scope,dict(count=7,status='idle',attempt=0,serial=0))
        started=asyncio.Event()
        async def wait(*a,**kw):
            started.set();await asyncio.Event().wait()
        self.policy.judge.side_effect=wait
        self.r.schedule(event);await started.wait()
        task=self.r.active[self.scope]
        self.learning.policy.interaction.start_foreground(event)
        await task
        self.assertEqual(self.actions()[0]['status'],'cancelled')
        self.client.call_action.assert_not_awaited()
        self.assertEqual(self.learning.opportunities.state(self.scope)['count'],7)

    async def test_stop_or_permission_change_before_send(self):
        event=self.setup_reactions();self.decision(event)
        async def get_msg(*a,**kw):
            self.r.cancel(self.scope,stop=True)
            return dict(group_id='10006')
        self.client.call_action.side_effect=get_msg
        await self.r.decide(self.scope,event)
        self.assertEqual(self.actions()[0]['status'],'cancelled')
        self.assertEqual(self.client.call_action.await_count,1)

    async def test_unknown_delivery_counts_and_does_not_retry(self):
        event=self.setup_reactions();self.decision(event)
        self.client.call_action.side_effect=[dict(group_id='10006'),TimeoutError('unknown')]
        await self.r.decide(self.scope,event)
        self.assertEqual(self.actions()[0]['status'],'unknown')
        self.assertTrue(self.r.already_reacted(self.scope,'1'))
        self.r.schedule(self.event());self.assertFalse(self.r.active)

    async def test_external_plugin_is_observed_not_throttled(self):
        event=self.setup_reactions()
        original=AsyncMock(return_value={})
        with patch.object(CQHttp,'call_action',original):
            self.learning.install()
            try:
                with self.bound(event):
                    await CQHttp().call_action('set_msg_emoji_like',message_id='1',emoji_id='128053',set=True)
            finally:
                CQHttp.call_action=original
        self.assertEqual(self.actions()[0]['source'],'external')
        self.assertFalse(self.r.at_capacity(self.scope))
        self.assertTrue(self.r.already_reacted(self.scope,'1'))
        self.notice(actor='')
        self.assertEqual(self.r.observations(self.scope)['emojis'],[])

    def evidence(self):
        rows=[]
        for i in range(5):
            event=self.event(sender='alice' if i%2 else 'bob')
            self.notice(str(self.number),actor='alice' if i%2 else 'bob')
        self.learning.db.execute('UPDATE reaction_observations SET source_ts=source_ts-id*301')
        return [self.r.decoded(r) for r in self.learning.db.execute('SELECT * FROM reaction_observations')]

    def patterns(self, rows):
        return dict(items=[dict(match_id=None,emoji_id='128053',categories=[CATEGORIES[1]],scene='荒诞操作',meaning='轻度调侃',boundary='认真求助不用',evidence_ids=[r['id'] for r in rows],confidence=.95,uncertain=False)])

    async def test_patterns_require_people_fragments_and_withdrawal_revokes(self):
        self.setup_reactions();rows=self.evidence()
        one=[dict(r,actors=['alice']) for r in rows]
        self.r.apply_patterns(self.scope,one,self.patterns(one))
        self.assertEqual(self.r.pattern_snapshot(self.scope)['items'][0]['status'],'pending')
        self.r.apply_patterns(self.scope,rows,self.patterns(rows))
        self.assertEqual(self.r.pattern_snapshot(self.scope)['items'][0]['status'],'active')
        for r in rows:self.notice(r['message_id'], actor='',count=0,add=False)
        self.assertEqual(self.r.pattern_snapshot(self.scope)['items'][0]['status'],'pending')

    async def test_group_persona_isolation(self):
        self.setup_reactions();self.notice()
        self.assertEqual(self.r.observations(('qq_private','10003'))['items'],[])
        self.rules[self.scope]['persona_id']='另一个人设'
        self.assertEqual(self.r.observations(self.scope)['items'],[])

    async def test_classification_failure_preserves_and_rechecks_uncertain(self):
        self.setup_reactions();rows=self.evidence()
        self.policy.judge.side_effect=RuntimeError('model unavailable')
        with self.assertLogs('astrbot',level='ERROR'):await self.r.classify(self.scope)
        self.assertEqual(self.r.observations(self.scope)['counts']['failed'],5)
        self.policy.judge.side_effect=None
        self.policy.judge.return_value=json.dumps(dict(items=[dict(id=r['id'],categories=[CATEGORIES[1]],meaning='调侃',reason='反常操作',boundary='求助不用',confidence=.6,uncertain=True) for r in rows]))
        await self.r.classify(self.scope)
        self.assertTrue(self.policy.judge.call_args.kwargs['recheck'])
        self.assertEqual(self.r.observations(self.scope)['counts']['uncertain'],5)

    async def test_backlog_cap_expiry_and_clear(self):
        self.setup_reactions()
        now=time.time()
        self.learning.db.executemany('INSERT INTO reaction_observations(platform,group_id,persona,message_id,emoji_id,created,updated,count) VALUES (?,?,?,?,?,?,?,1)',
            [(*self.r.scope(self.scope),str(i),'424',now-i,now) for i in range(505)])
        self.r.prune(self.scope)
        self.assertEqual(self.r.observations(self.scope)['counts']['pending'],500)
        self.assertEqual(self.r.state(self.scope)['meta']['dropped'],5)
        self.learning.clear_group(*self.scope)
        self.assertEqual(self.r.observations(self.scope)['total'],0)

    async def test_web_filters_statistics_and_scope(self):
        self.setup_reactions();self.notice()
        web=LearningWeb(self.owner);app=Quart(__name__)
        async with app.test_request_context('/?platform=qq_private&group_id=10006&days=30&emoji=424'):
            data=await web.reactions();self.assertEqual(data['total'],0);self.assertEqual(data['days'],30)
        async with app.test_request_context('/?platform=qq_private&group_id=10003'):
            self.assertEqual((await web.reactions())['items'],[])
        async with app.test_request_context('/?platform=qq_private&group_id=10006&days=2'):
            with self.assertRaises(ValueError):await web.reactions()

    async def test_no_reward_for_reactions_and_only_used_pattern_is_affected(self):
        event=self.setup_reactions();rows=self.evidence();self.r.apply_patterns(self.scope,rows,self.patterns(rows))
        state=self.r.state(self.scope);state['patterns'].append(dict(state['patterns'][0],id='unused'));self.r.save_state(self.scope,state)
        self.decision(event);await self.r.decide(self.scope,event)
        action=self.actions()[0]
        details=action['details'];details['used_pattern_ids']=['rp1']
        self.learning.db.execute('UPDATE reaction_actions SET sent=?,details=? WHERE id=?',(time.time()-125,json.dumps(details),action['id']))
        feedback=self.event(text='你这个猴子理解错了，这是认真求助')
        self.learning.db.execute('UPDATE messages SET ts=? WHERE message_id=?',(time.time()-10,str(self.number)))
        self.policy.judge.return_value=json.dumps(dict(items=[dict(message_id=str(self.number),action_id=action['id'],intent='correction',confidence=.95,evidence_text='这是认真求助',reason='纠正贴表情的语境',affected_pattern_ids=['rp1'])]))
        # Only the explicit reaction feedback lies in this action's observation window.
        self.learning.db.execute('DELETE FROM messages WHERE message_id<>?',(str(self.number),))
        await self.r.effects(self.scope)
        patterns={p['id']:p for p in self.r.state(self.scope)['patterns']}
        self.assertEqual(patterns['rp1']['status'],'suspended');self.assertEqual(patterns['unused']['status'],'active')
        self.assertIn(str(self.number),self.r.claimed_messages(self.scope))
        self.assertFalse(self.learning.db.execute('SELECT 1 FROM feedback').fetchone())

    async def test_oversized_image_not_downloaded(self):
        event=self.setup_reactions()
        self.r.media[(*self.scope,'1')]=(time.time(),dict(url='https://example.invalid/large.png',file_size=11*1024**2))
        self.learning.background.download=AsyncMock()
        self.assertEqual(await self.r.image(self.scope,[dict(message_id='1',image_descriptions=[])],self.tmp.name),([],None))
        self.learning.background.download.assert_not_awaited()

    async def test_timeout_before_send_and_explicit_platform_failure(self):
        event=self.setup_reactions();self.decision(event)
        native_timeout=asyncio.timeout
        self.policy.judge.side_effect=lambda *a,**kw: None
        async def slow(*a,**kw):await asyncio.sleep(.1)
        self.policy.judge.side_effect=slow
        with patch('astrbot_plugin_qq_chat_control.reactions.asyncio.timeout',side_effect=lambda seconds:native_timeout(.01)):
            await self.r.decide(self.scope,event)
        self.assertEqual(self.actions()[0]['status'],'failed')
        self.client.call_action.assert_not_awaited()
        self.policy.judge.side_effect=None;self.decision(event)
        self.client.call_action.side_effect=[dict(group_id='10006'),ActionFailed({'retcode':100,'wording':'not supported'})]
        await self.r.decide(self.scope,event)
        self.assertEqual(self.actions()[0]['status'],'failed')
        self.assertFalse(self.r.already_reacted(self.scope,'1'))

    async def test_cleared_ids_not_reused_and_reload_does_not_send(self):
        event=self.setup_reactions();self.decision(event);await self.r.decide(self.scope,event)
        old=self.actions()[0]['id'];self.learning.clear_group(*self.scope)
        event=self.event();self.decision(event);await self.r.decide(self.scope,event)
        self.assertGreater(self.actions()[0]['id'],old)
        self.learning.db.execute("UPDATE reaction_actions SET status='sending'");self.learning.db.commit()
        self.learning.close();self.client.call_action.reset_mock();self.learning.open()
        self.assertEqual(self.actions()[0]['status'],'unknown')
        self.client.call_action.assert_not_awaited()

    async def test_late_cached_image_description_reuses_existing_analysis(self):
        self.setup_reactions();rows=self.evidence()
        self.learning.db.execute("UPDATE reaction_observations SET text='',status='missing',resolved=1")
        self.stickers.descriptions=lambda *args:['图片里用风扇给另一台风扇降温']
        self.r.summarize=AsyncMock()
        self.policy.judge.return_value=json.dumps(dict(items=[dict(id=r['id'],categories=[CATEGORIES[1]],meaning='调侃',reason='反常操作',boundary='求助不用',confidence=.95,uncertain=False) for r in rows]))
        await self.r.tick(self.scope)
        self.assertEqual(self.r.observations(self.scope)['counts']['classified'],5)
        self.assertTrue(all(r['image_descriptions'] for r in self.policy.judge.call_args.args[2]['items']))
        self.client.call_action.assert_not_awaited()

    async def test_stop_reacting_preserves_text_retry(self):
        event=self.setup_reactions()
        event.get_message_str=lambda:'别贴表情了！'
        self.learning.opportunities.save(self.scope,dict(count=7,status='retry',attempt=1,serial=2))
        self.policy.interaction.observe(event)
        self.assertGreater(self.r.blocked[self.scope],time.time())
        self.assertEqual(self.learning.opportunities.state(self.scope),dict(count=7,status='retry',attempt=1,serial=2))
        self.r.schedule(event);self.assertFalse(self.r.active)
