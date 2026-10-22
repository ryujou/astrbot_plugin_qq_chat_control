import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from quart import Quart
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.provider.entities import LLMResponse, ProviderRequest
from astrbot.core.provider.func_tool_manager import _PermissionGuardedTool
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot.core.agent.run_context import ContextWrapper

from astrbot_plugin_qq_chat_control.learning import FeedbackLearning
from astrbot_plugin_qq_chat_control.tools import bind_tools
from astrbot_plugin_qq_chat_control.web import LearningWeb


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.rules = {('qq_private', group): dict(persona_id='同人设', feedback_observe=True) for group in ['a', 'b']}
        self.owner = NS(rules=self.rules, config={'allowed_group_ids':['a','b']},
                        profiles=NS(records={}), context=NS(platform_manager=NS(platform_insts=[])))
        self.owner.learning = FeedbackLearning(self.owner, Path(self.tmp.name)/'learning.db')
        self.db = self.owner.learning.open()
        self.addCleanup(self.owner.learning.close)
        for group, body in [('a','甲群原话'),('b','乙群独立原话')]:
            self.db.execute('INSERT INTO replies(platform,group_id,persona,created,body,status,context,summary) '
                "VALUES ('qq_private',?,'同人设',1000,?,'settled','[]','{}')",(group,body))
        self.db.commit()
        self.web = LearningWeb(self.owner)
        self.app = Quart(__name__)

    async def get(self, method, query='group_id=a&platform=qq_private'):
        async with self.app.test_request_context('/?'+query):
            return await getattr(self.web,method)()

    async def test_scope_and_detail_reject_cross_group(self):
        result = await self.get('replies')
        self.assertEqual([r['body'] for r in result['items']], ['甲群原话'])
        with self.assertRaises(ValueError):
            await self.get('reply','group_id=a&platform=qq_private&id=2')
        with self.assertRaises(ValueError):
            await self.get('overview','group_id=unknown&platform=qq_private')

    async def test_null_score_and_empty_overview_remain_unknown(self):
        data = await self.get('overview')
        self.assertIsNone(data['replies']['mean_score'])
        self.assertEqual(data['comparison'][0]['samples'],0)
        self.assertIsNone(data['comparison'][0]['participants'])
        self.assertEqual(data['images'],{})
        self.assertEqual(data['trained'],0)

    async def test_member_prefix_and_candidate_slang(self):
        record = dict(name='成员',profile='只在甲群的画像',affinity=52,samples=[],last_update=3,last_seen=4)
        self.owner.profiles.records={'qq_private:a:1':record,'qq_private:b:2':dict(record,profile='乙群')}
        result = await self.get('members')
        self.assertEqual(result['total'],1)
        self.assertEqual(result['items'][0]['user_id'],'1')
        sense=dict(rounds=1,evidence=[dict(sender='1',text='原话',message_id='m')],confidence=.99,clues=[],meaning='含义',scene='场景')
        self.db.execute('INSERT INTO slang_terms VALUES (?,?,?,?,?,?)',('qq_private','a','同人设','黑话',json.dumps([sense]),5))
        self.db.commit()
        self.assertFalse((await self.get('slang'))['items'][0]['senses'][0]['confirmed'])

    async def test_sticker_bytes_and_other_group_and_path_excluded(self):
        root=self.owner.learning.policy.stickers.root/'qq_private'/'a'
        root.mkdir(parents=True)
        path=root/'1.gif'
        content=base64.b64decode('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7')
        path.write_bytes(content)
        self.db.execute("INSERT INTO stickers(id,platform,group_id,persona,status,path) VALUES (1,'qq_private','a','同人设','ready',?)",(str(path),))
        self.db.commit()
        result=await self.get('image','group_id=a&platform=qq_private&id=1')
        self.assertEqual(base64.b64decode(result['src'].split(',')[1]),content)
        self.assertTrue(result['src'].startswith('data:image/gif;'))
        with self.assertRaises(ValueError):await self.get('image','group_id=b&platform=qq_private&id=1')
        self.db.execute('UPDATE stickers SET path=?',(str(Path(self.tmp.name)/'outside.gif'),))
        self.db.commit()
        with self.assertRaises(ValueError):await self.get('image','group_id=a&platform=qq_private&id=1')


class ToolsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.core=FunctionTool(name='astrbot_exec_shell',description='执行命令',parameters={'type':'object','properties':{'command':{'type':'string'}},'required':['command']})
        self.admin=FunctionTool(name='llm_set_group_ban',description='禁言',parameters={'type':'object','properties':{'user_id':{'type':'integer'},'need_auth':{'type':'boolean'}}},handler_module_path='data.plugins.astrbot_plugin_qqadmin.main')
        self.disabled=FunctionTool(name='disabled',description='已关闭',parameters={},active=False)
        self.owner=NS(chat_allowed=lambda _:True,limits=NS(is_limited=lambda _:False),context=NS(get_llm_tool_manager=lambda:NS(
            get_full_tool_set=lambda:ToolSet([self.admin,self.disabled]),iter_builtin_tools=lambda:[self.core])))
        self.event=NS(get_extra=lambda _:False,get_group_id=lambda:'10009',get_platform_name=lambda:'aiocqhttp')

    async def test_all_tools_include_runtime_native_plugins_and_qq_artifacts(self):
        request=ProviderRequest(prompt='管理一下',func_tool=ToolSet([self.core]),system_prompt='人设')
        bind_tools(self.owner,self.event,request)
        names={tool.name for tool in request.func_tool.tools}
        self.assertEqual(names,{'astrbot_exec_shell','llm_set_group_ban','qq_send_artifact','qq_send_group_avatar','qq_list_group_files','qq_download_group_file'})
        self.assertNotIn('need_auth',request.func_tool.get_tool(self.admin.name).parameters['properties'])
        self.assertIn('need_auth',self.admin.parameters['properties'])
        self.assertIn('人设',request.system_prompt)

    async def test_limited_automatic_and_unallowed_get_no_tools(self):
        for mode in ['limited','automatic','unallowed']:
            with self.subTest(mode=mode):
                self.owner.limits.is_limited=lambda _:mode=='limited'
                self.owner.chat_allowed=lambda _:mode!='unallowed'
                self.event.get_extra=lambda _:mode=='automatic'
                request=ProviderRequest(func_tool=ToolSet([self.core]))
                bind_tools(self.owner,self.event,request)
                self.assertIsNone(request.func_tool)

    async def test_native_per_tool_permission_guard_is_preserved(self):
        original=FunctionTool(name='private_tool',description='受限工具',parameters={},handler=AsyncMock())
        manager=NS(_check_tool_permission=AsyncMock(return_value='当前用户无权限'))
        guarded=_PermissionGuardedTool(original,manager)
        self.owner.context.get_llm_tool_manager=lambda:NS(get_full_tool_set=lambda:ToolSet([guarded]),iter_builtin_tools=lambda:[])
        request=ProviderRequest(system_prompt='')
        bind_tools(self.owner,self.event,request)
        result=await request.func_tool.get_tool('private_tool').call(NS())
        self.assertEqual(result,'当前用户无权限')
        original.handler.assert_not_called()

    async def test_qq_file_tool_uses_native_executor_and_current_event(self):
        self.event.message_obj=NS(self_id='90001')
        self.event.bot=NS(call_action=AsyncMock(return_value={'files':[]}))
        self.event.get_result=lambda:None
        request=ProviderRequest()
        bind_tools(self.owner,self.event,request)
        tool=request.func_tool.get_tool('qq_list_group_files')
        results=[r async for r in FunctionToolExecutor.execute(tool,ContextWrapper(NS(event=self.event)))]
        self.assertIn('files',results[0].content[0].text)
        self.event.bot.call_action.assert_awaited_once_with(action='get_group_root_files',group_id=10009,self_id=90001)

    async def test_avatar_uses_current_group_and_waits_for_real_send(self):
        self.event.chain_result=lambda parts:NS(chain=parts)
        self.event.send=AsyncMock()
        self.event.get_result=lambda:None
        request=ProviderRequest()
        bind_tools(self.owner,self.event,request)
        tool=request.func_tool.get_tool('qq_send_group_avatar')
        results=[r async for r in FunctionToolExecutor.execute(tool,ContextWrapper(NS(event=self.event)))]
        self.assertIn('success',results[0].content[0].text)
        component=self.event.send.await_args.args[0].chain[0]
        self.assertEqual(component.file,'https://p.qlogo.cn/gh/10009/10009/640')
        self.event.send.side_effect=RuntimeError('image send failed')
        with self.assertRaisesRegex(RuntimeError,'image send failed'):
            await tool.handler(self.event)

    async def test_avatar_is_not_available_in_private_chat(self):
        self.event.get_group_id=lambda:''
        request=ProviderRequest()
        bind_tools(self.owner,self.event,request)
        self.assertNotIn('qq_send_group_avatar',{tool.name for tool in request.func_tool.tools})




    async def test_tool_call_without_text_is_not_counted_as_completed_reply(self):
        from astrbot_plugin_qq_chat_control.limits import LLMLimits
        limits=LLMLimits.__new__(LLMLimits)
        call=LLMResponse(role='assistant',tools_call_name=['qq_send_group_avatar'],tools_call_args=[{}])
        self.assertFalse(limits.successful(call))
        self.assertTrue(limits.successful(LLMResponse(role='assistant',completion_text='已发')))



if __name__ == '__main__':
    unittest.main()
