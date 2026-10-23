import asyncio
import contextvars
import json
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

from astrbot.core.provider.entities import LLMResponse
from astrbot_plugin_qq_chat_control.main import QQChatControl
from astrbot_plugin_qq_chat_control.strategy import GroupPolicy


class PublicConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_standard_provider_does_not_require_codex_integration(self):
        plugin = QQChatControl.__new__(QQChatControl)
        plugin.config = {'default_provider_id': 'ordinary', 'codex_provider_id': ''}
        plugin.rules = {}
        plugin.ready_lock = asyncio.Lock()
        plugin.ready = False
        plugin.validation_error = None
        ordinary = NS(provider_config={'id': 'ordinary'})
        plugin.context = NS(get_provider_by_id=lambda key: ordinary if key == 'ordinary' else None)
        plugin.bindings = NS(reconcile=AsyncMock())
        await plugin.ensure_ready()
        self.assertTrue(plugin.ready)
        plugin.bindings.reconcile.assert_awaited_once_with({}, ordinary)

    async def test_configured_recheck_provider_keeps_max_requirement(self):
        provider = NS(provider_config={'reasoning_effort': 'max'}, get_model=lambda: 'gpt-6.1-sol',
            chat_only_context=contextvars.ContextVar('public_recheck', default=False),
            text_chat=AsyncMock(return_value=LLMResponse(role='assistant', completion_text='{}')))
        owner = NS(rules={('qq','10001'): {'persona_id':'demo'}},
            config={'feedback_recheck_provider_id':'my-codex'}, provider=Mock(return_value=provider),
            limits=NS(successful=lambda response: bool(response.completion_text)))
        learning = NS(owner=owner, path=Path('unused.db'), record_call=Mock(), expressions=NS(generations={}))
        policy = GroupPolicy(learning)
        await policy.judge(('qq','10001'), 'test', {}, recheck=True)
        owner.provider.assert_called_once_with('my-codex')
        self.assertEqual(learning.record_call.call_args.args[2], 'my-codex')
        self.assertFalse(provider.chat_only_context.get())
        provider.provider_config['reasoning_effort'] = 'low'
        with self.assertRaises(ValueError):
            await policy.judge(('qq','10001'), 'test', {}, recheck=True)

    async def test_automatic_generic_reply_has_no_tools(self):
        plugin = QQChatControl.__new__(QQChatControl)
        plugin.chat_allowed = lambda event: True
        plugin.provider_id = lambda event: 'ordinary'
        plugin.provider = lambda key: NS(provider_config={'type':'openai_chat_completion'})
        plugin.learning = NS(prepare=Mock(), policy=NS(prepare=AsyncMock(return_value=False)))
        plugin.limits = NS(is_limited=lambda event: False)
        event = NS(get_platform_name=lambda:'aiocqhttp', get_extra=lambda key:key=='qq_chat_automatic')
        request = NS(func_tool=object())
        await plugin.bind_request(event, request)
        self.assertIsNone(request.func_tool)

    async def test_generic_reply_preserves_tools_only_for_unlimited_direct_chat(self):
        plugin = QQChatControl.__new__(QQChatControl)
        plugin.chat_allowed = lambda event: True
        plugin.provider_id = lambda event: 'ordinary'
        plugin.provider = lambda key: NS(provider_config={'type':'openai_chat_completion'})
        plugin.learning = NS(prepare=Mock(), policy=NS(prepare=AsyncMock(return_value=False)))
        event = NS(get_platform_name=lambda:'aiocqhttp', get_extra=lambda key:False)
        for limited in (True, False):
            with self.subTest(limited=limited):
                plugin.limits = NS(is_limited=lambda event: limited)
                tools = object()
                request = NS(func_tool=tools)
                await plugin.bind_request(event, request)
                self.assertIs(request.func_tool, None if limited else tools)

    def test_defaults_have_no_identifiers_or_enabled_learning(self):
        schema = json.loads((Path(__file__).resolve().parents[1]/'_conf_schema.json').read_text(encoding='utf-8'))
        for key in ['management_group_id','default_provider_id','codex_provider_id','learning_embedding_provider_id']:
            self.assertEqual(schema[key]['default'], '')
        self.assertEqual(schema['allowed_group_ids']['default'], [])
        self.assertEqual(schema['allowed_qq_ids']['default'], [])
        self.assertEqual(schema['group_rules']['default'], [])


if __name__ == '__main__':
    unittest.main()
