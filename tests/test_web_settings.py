import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from quart import Quart

from astrbot_plugin_qq_chat_control.web_settings import WebSettings


class Config(dict):
    def __init__(self, path, values):
        super().__init__(values)
        self.config_path = str(path)
        self.save_config(values)

    def save_config(self, values):
        self.clear()
        self.update(values)
        Path(self.config_path).write_text(json.dumps(values), encoding='utf-8')


class SettingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.config = Config(Path(temp.name) / 'config.json', dict(default_provider_id='chat', allowed_group_ids=['demo-a'],
            group_rules=[dict(platform_id='qq', group_id='demo-a', provider_id='chat', persona_id='friend', message_count=8,
                             feedback_observe=True, feedback_adapt_mode='学习', private_extra='keep'),
                         dict(platform_id='qq', group_id='demo-b', message_count=15)], private_global='keep'))
        self.reload = AsyncMock(return_value=(True, None))
        provider = NS(provider_config={'id':'chat'}, get_model=lambda:'demo-model')
        self.owner = NS(config=self.config, provider=lambda key: provider if key=='chat' else self.missing(key),
            context=NS(_star_manager=NS(reload=self.reload), get_all_providers=lambda:[provider], get_all_embedding_providers=lambda:[],
                persona_manager=NS(personas_v3=[{'name':'friend'}, {'name':'other'}],
                                   get_persona_v3_by_id=lambda key: {'name':key} if key in {'friend','other'} else None)))
        self.settings = WebSettings(self.owner)
        self.app = Quart(__name__)

    def missing(self, key):
        raise ValueError('模型不存在：' + key)

    async def get(self, area='group'):
        async with self.app.test_request_context('/?platform=qq&group_id=demo-a&area='+area):
            return await self.settings.get()

    async def save(self, changes, original, **extra):
        body = dict(area='group', platform='qq', group_id='demo-a', changes=changes, original=original)
        body.update(extra)
        async with self.app.test_request_context('/', method='POST', json=body):
            return await self.settings.save()

    async def test_fields_cover_schema_and_only_expose_model_names(self):
        for area in ('group', 'global'):
            data = await self.get(area)
            keys = {f['key'] for s in data['sections'] for f in s['fields']}
            self.assertEqual(keys, set(data['values']))
            self.assertNotIn('private_extra', data['values'])
            self.assertNotIn('private_global', data['values'])
        model = (await self.get())['sections'][0]['fields'][0]
        self.assertEqual(model['choices'][1], {'value':'chat','label':'chat · demo-model'})

    async def test_group_save_preserves_other_group_and_unknown_values(self):
        result = await self.save({'message_count':10}, {'message_count':8})
        self.assertTrue(result['saved'])
        current = self.settings.current()
        self.assertEqual([r['message_count'] for r in current['group_rules']], [10,15])
        self.assertEqual(current['group_rules'][0]['private_extra'], 'keep')
        self.assertEqual(current['private_global'], 'keep')
        self.reload.assert_awaited_once_with('astrbot_plugin_qq_chat_control')

    async def test_global_save_and_no_change(self):
        self.assertFalse((await self.save({'message_count':8}, {'message_count':8}))['saved'])
        self.reload.assert_not_awaited()
        await self.save({'member_limit_count':12}, {'member_limit_count':5}, area='global')
        self.assertEqual(self.settings.current()['member_limit_count'], 12)
        self.assertEqual(self.settings.current()['group_rules'][0]['message_count'], 8)

    async def test_stale_field_rejected_but_unrelated_current_edits_preserved(self):
        current = self.settings.current()
        current['group_rules'][0]['message_count'] = 9
        current['quiet_group_errors'] = False
        self.config.save_config(current)
        with self.assertRaisesRegex(ValueError, '其他页面修改'):
            await self.save({'message_count':10}, {'message_count':8})
        await self.save({'reaction_max_per_10min':1}, {'reaction_max_per_10min':2})
        self.assertEqual(self.settings.current()['group_rules'][0]['message_count'], 9)
        self.assertFalse(self.settings.current()['quiet_group_errors'])

    async def test_invalid_types_ranges_keys_and_dependencies_do_not_save(self):
        original = self.settings.current()
        cases = [({'message_count':0},{'message_count':8}), ({'message_count':True},{'message_count':8}),
                 ({'reaction_interval_seconds':60},{'reaction_interval_seconds':120}),
                 ({'reaction_max_per_10min':3},{'reaction_max_per_10min':2}),
                 ({'banter_intensity':101},{'banter_intensity':30}),
                 ({'feedback_observe':False},{'feedback_observe':True}),
                 ({'group_id':'other'},{'group_id':'demo-a'}),
                 ({'feedback_bot_ids':[123]},{'feedback_bot_ids':[]}),
                 ({'feedback_adapt_mode':'invalid'},{'feedback_adapt_mode':'学习'}),
                 ({'provider_id':'missing'},{'provider_id':'chat'})]
        for changes, baseline in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                await self.save(changes, baseline)
        self.assertEqual(self.settings.current(), original)
        self.reload.assert_not_awaited()

    async def test_persona_requires_explicit_confirmation(self):
        with self.assertRaisesRegex(ValueError, '清理本群聊天记录'):
            await self.save({'persona_id':'other'}, {'persona_id':'friend'})
        self.reload.assert_not_awaited()
        await self.save({'persona_id':'other'}, {'persona_id':'friend'}, confirm_persona_change=True)
        self.assertEqual(self.settings.current()['group_rules'][0]['persona_id'], 'other')

    async def test_reload_failure_reports_saved_state(self):
        self.reload.return_value = False, 'synthetic reload error'
        with self.assertRaisesRegex(ValueError, '配置已保存，但插件重载失败'):
            await self.save({'message_count':12}, {'message_count':8})
        self.assertEqual(self.settings.current()['group_rules'][0]['message_count'], 12)

    async def test_unknown_group_cannot_write(self):
        with self.assertRaisesRegex(ValueError, '群规则已不存在'):
            await self.save({'message_count':10}, {'message_count':8}, group_id='missing')
        self.reload.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
