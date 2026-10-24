import asyncio
import io
import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from PIL import Image
import test_group_adaptation as fixture
from astrbot_plugin_qq_chat_control.presence import square_image, same_avatar, validate_presence


class PresenceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.AdaptationTests.asyncSetUp

    def setup_presence(self):
        self.scope = ('qq_private', '10006')
        self.rules[self.scope]['self_card_auto'] = True
        self.p = self.learning.presence
        self.owner.context.persona_manager = NS(get_persona_v3_by_id=lambda _: {'prompt':'合成角色'})
        self.key = 'card:qq_private:10006:90001'
        self.p.judge = AsyncMock(return_value=dict(change=True, pause=False, name='今天也没下班', reason='接本群的梗', confidence=.95))
        self.calls = []
        self.card = '原来的名字'

        async def action(name, **params):
            self.calls.append((name, params))
            if name == 'get_group_member_info':
                return dict(card=self.card)
            if name == 'get_group_member_list':
                return [dict(user_id=1, card='今天不加班'), dict(user_id=90001, card=self.card)]
            if name == 'set_group_card':
                self.card = params['card']
            if name == 'get_login_info':
                return dict(user_id=90001)
        self.client = NS(call_action=action)
        self.learning.open().execute('INSERT INTO messages(platform,group_id,message_id,sender,ts,text,parent) VALUES (?,?,?,?,?,?,?)',
            (*self.scope, 'sample', '1', time.time(), '周末还在加班', ''))
        return self.p

    async def test_card_only_changes_self_and_persists_cooldown(self):
        p = self.setup_presence()
        await p.card(self.scope, self.client, '90001')
        changes = [v for k,v in self.calls if k=='set_group_card']
        self.assertEqual(changes, [dict(group_id=10006, user_id=90001, card='今天也没下班')])
        self.assertGreaterEqual(p.state(self.key)['next'], time.time()+5.9*3600)
        self.assertFalse(p.state(self.key)['paused'])
        await p.card(self.scope, self.client, '90001')
        self.assertEqual(len([k for k,v in self.calls if k=='set_group_card']), 1)

    async def test_manual_change_pauses_without_model_or_write(self):
        p = self.setup_presence()
        p.save(self.key, dict(current='机器人上次设置'))
        await p.card(self.scope, self.client, '90001')
        self.assertTrue(p.state(self.key)['paused'])
        p.judge.assert_not_awaited()
        self.assertNotIn('set_group_card', [k for k,v in self.calls])

    async def test_same_member_name_is_rejected(self):
        p = self.setup_presence()
        p.judge.return_value['name'] = '今天不加班'
        await p.card(self.scope, self.client, '90001')
        self.assertNotIn('set_group_card', [k for k,v in self.calls])

    async def test_disabled_before_execution_does_not_change(self):
        p = self.setup_presence()
        async def decide(*args, **kwargs):
            self.rules[self.scope]['self_card_auto'] = False
            return dict(change=True, name='不能发送', confidence=.99)
        p.judge.side_effect = decide
        await p.card(self.scope, self.client, '90001')
        self.assertNotIn('set_group_card', [k for k,v in self.calls])

    async def test_unknown_send_does_not_repeat_and_later_confirms(self):
        p = self.setup_presence()
        original = self.client.call_action
        async def action(name, **kwargs):
            result = await original(name, **kwargs)
            if name=='set_group_card':
                raise TimeoutError('synthetic timeout after acceptance')
            return result
        self.client.call_action = action
        with self.assertRaises(TimeoutError):
            await p.card(self.scope, self.client, '90001')
        self.assertTrue(p.state(self.key)['paused'])
        await p.card(self.scope, self.client, '90001')
        self.assertFalse(p.state(self.key)['paused'])
        self.assertEqual(len([k for k,v in self.calls if k=='set_group_card']), 1)

    async def test_disabled_features_do_not_query_platform(self):
        p = self.setup_presence()
        self.rules[self.scope]['self_card_auto'] = False
        self.learning.background.client = lambda _: self.fail('No platform request expected')
        await p.tick()

    async def test_avatar_requires_persona_and_retains_manual_override(self):
        p = self.setup_presence()
        self.owner.config.update(self_avatar_auto=True, self_avatar_platform=self.scope[0], self_avatar_persona='demo')
        def picture(color):
            out=io.BytesIO();Image.new('RGB',(512,512),color).save(out,format='PNG');return square_image(out.getvalue())
        p.folder.mkdir()
        path=p.folder/'candidate.jpg';path.write_bytes(picture('red'))
        p.db.execute('INSERT INTO presence_images(account,persona,site,post_id,source,path,reason,created) VALUES (?,?,?,?,?,?,?,?)',
            ('90001','demo','danbooru','1','https://example.invalid/art',str(path),'合成人设',time.time()))
        (p.folder/'90001-current.jpg').write_bytes(picture('white'))
        p.fetch=AsyncMock(return_value=picture('black'))
        p.collect=AsyncMock()
        await p.avatar(self.scope,self.client,'90001')
        self.assertTrue(p.state('avatar:90001')['paused'])
        self.assertNotIn('set_qq_avatar',[k for k,v in self.calls])

    async def test_clear_group_preserves_other_group_and_account_avatar(self):
        p=self.setup_presence()
        for key in [self.key,'card:qq_private:10003:90001','avatar:90001']:
            p.save(key,dict(current='demo'))
        p.clear(self.scope)
        self.assertFalse(p.state(self.key))
        self.assertTrue(p.state('card:qq_private:10003:90001'))
        self.assertTrue(p.state('avatar:90001'))

    async def test_avatar_sends_once_then_observes_cooldown(self, manual=False):
        p = self.setup_presence()
        self.owner.config.update(self_avatar_auto=not manual, self_avatar_platform=self.scope[0], self_avatar_persona='demo')
        def picture(color):
            out = io.BytesIO()
            Image.new('RGB', (512, 512), color).save(out, format='PNG')
            return square_image(out.getvalue())
        raw = picture('red')
        p.folder.mkdir()
        path = p.folder/'candidate.jpg'
        path.write_bytes(raw)
        p.db.execute('INSERT INTO presence_images(account,persona,site,post_id,source,path,reason,created) VALUES (?,?,?,?,?,?,?,?)',
            ('90001','demo','danbooru','1','https://example.invalid/art',str(path),'合成人设',time.time()))
        p.collect = AsyncMock()
        p.fetch = AsyncMock(side_effect=[picture('white'), raw])
        p.judge.return_value = dict(accept=True, confidence=.99, reason='角色匹配')
        await p.avatar(self.scope, self.client, '90001', **({'image_id':1} if manual else {}))
        await p.avatar(self.scope, self.client, '90001')
        writes = [v for k,v in self.calls if k=='set_qq_avatar']
        self.assertEqual(len(writes), 1)
        self.assertTrue(writes[0]['file'].startswith('base64://'))
        self.assertFalse(p.state('avatar:90001')['paused'])
        self.assertGreater(p.state('avatar:90001')['next'], time.time()+47*3600)

    async def test_manual_use_succeeds_with_auto_off(self):
        await self.test_avatar_sends_once_then_observes_cooldown(manual=True)

    async def test_web_pause_and_resume_keep_cooldown(self):
        from quart import Quart
        from astrbot_plugin_qq_chat_control.web import LearningWeb
        p = self.setup_presence()
        p.save(self.key, dict(current='旧名', next=12345))
        web, app = LearningWeb(self.owner), Quart(__name__)
        for action in ['pause', 'resume']:
            async with app.test_request_context('/', method='POST', json=dict(
                    platform=self.scope[0], group_id=self.scope[1], key=self.key, action=action)):
                await web.presence_control()
            self.assertEqual(p.state(self.key)['paused'], action=='pause')
            self.assertEqual(p.state(self.key)['next'], 12345)
        self.assertNotIn('current', p.state(self.key))

    def test_config_dependencies_and_intervals(self):
        validate_presence({}, [{}])
        with self.assertRaises(ValueError):validate_presence(dict(self_avatar_auto=True), [])
        with self.assertRaises(ValueError):validate_presence({}, [dict(self_card_min_hours=12,self_card_max_hours=6)])
        with self.assertRaises(ValueError):validate_presence(dict(self_avatar_min_hours=1), [])

    async def test_manual_collection_works_with_auto_off_and_stops_at_five(self):
        p=self.setup_presence()
        self.owner.config.update(self_avatar_auto=False,self_avatar_platform=self.scope[0],self_avatar_persona='demo')
        out=io.BytesIO();Image.new('RGB',(256,256),'red').save(out,format='PNG')
        p.fetch=AsyncMock(return_value=out.getvalue())
        p.search=AsyncMock(return_value=[dict(id=i,rating='g',file_url=f'https://cdn.donmai.us/{i}.png') for i in range(1,9)])
        p.judge=AsyncMock(side_effect=[dict(tag='demo_character')]+[dict(accept=True,reason='合成候选')]*5)
        await p.collect(self.scope,'90001','demo','合成人设',{},'avatar:90001',manual=True)
        self.assertEqual(p.db.execute('SELECT COUNT(*) FROM presence_images').fetchone()[0],5)
        self.assertEqual(p.fetch.await_count,5)
        self.assertNotIn('set_qq_avatar',[k for k,v in self.calls])
        self.assertEqual(p.state('avatar:90001')['search_page'],2)

    async def test_manual_operation_keeps_auto_switch_off(self):
        p=self.setup_presence()
        self.owner.config.update(self_avatar_auto=False,self_avatar_platform=self.scope[0],self_avatar_persona='demo')
        self.learning.background.client=lambda _: self.client
        p.collect=AsyncMock()
        p.start_manual(self.scope,'collect')
        await p.task
        p.collect.assert_awaited_once()
        self.assertFalse(self.owner.config['self_avatar_auto'])
        self.assertEqual(p.operation,'')

    async def test_manual_use_rejects_other_account_or_persona(self):
        p=self.setup_presence()
        self.owner.config.update(self_avatar_auto=False,self_avatar_platform=self.scope[0],self_avatar_persona='demo')
        with self.assertRaises(ValueError):
            await p.avatar(self.scope,self.client,'90001',image_id=999)
        self.assertNotIn('set_qq_avatar',[k for k,v in self.calls])

    def test_image_limits_and_roundtrip(self):
        out=io.BytesIO();Image.new('RGB',(256,512),'red').save(out,format='PNG')
        data=square_image(out.getvalue())
        self.assertEqual(Image.open(io.BytesIO(data)).size,(512,512))
        self.assertTrue(same_avatar(data,data))
        with self.assertRaises(ValueError):square_image(b'x'*(10*1024**2+1))
