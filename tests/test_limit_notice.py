import asyncio
import json
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import test_group_adaptation as fixture
from astrbot_plugin_qq_chat_control.limits import LLMLimits

class LimitNoticeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.AdaptationTests.asyncSetUp
    event = fixture.AdaptationTests.event

    def prepare(self, group='10006', sender='bob'):
        self.owner.context.persona_manager = NS(get_persona_v3_by_id=lambda _: {'prompt':'合成群人设'})
        event = self.event(group, sender)
        key = ':'.join(('qq_private',group,sender))
        record = self.owner.limits.reserve(key)
        record.update(used=2,pending=0)
        return event, record

    async def test_concurrent_notice_generated_once_and_survives_reload(self):
        event, record = self.prepare()
        self.policy.judge = AsyncMock(return_value='{"text":"今天先收工，别催了。"}')
        first, second = await asyncio.gather(self.owner.limits.limit_notice(event),self.owner.limits.limit_notice(event))
        self.assertEqual(first,second)
        self.assertEqual(self.policy.judge.await_count,1)
        self.assertEqual(record['used'],2)
        restored=LLMLimits(self.owner,self.owner.limits.path)
        self.assertEqual(await restored.limit_notice(event),first)
        self.assertEqual(self.policy.judge.await_count,1)

    async def test_group_member_and_cycle_isolation(self):
        self.policy.judge=AsyncMock(side_effect=['{"text":"甲群歇会儿。"}','{"text":"乙群先收工。"}','{"text":"另一位先聊到这。"}','{"text":"新一轮下班了。"}'])
        a,r=self.prepare();b,_=self.prepare('10003');c,_=self.prepare(sender='carol')
        answers=[await self.owner.limits.limit_notice(e) for e in [a,b,c]]
        self.assertEqual(len(set(answers)),3)
        r['start']=0
        fresh=self.owner.limits.reserve('qq_private:10006:bob')
        fresh.update(used=2,pending=0)
        self.assertEqual(await self.owner.limits.limit_notice(a),'新一轮下班了。')

    async def test_only_confirmed_expressions_are_used(self):
        event,_=self.prepare()
        self.learning.expressions.state=lambda *a: {'expressions':[{'status':'active','style':'简短唱反调'},{'status':'pending','style':'未验证表达'}]}
        self.policy.judge=AsyncMock(return_value='{"text":"到点收工。"}')
        await self.owner.limits.limit_notice(event)
        payload=self.policy.judge.call_args.args[2]
        self.assertEqual([e['style'] for e in payload['expressions']],['简短唱反调'])

    async def test_failure_or_mechanical_notice_cached_without_retry(self):
        event,_=self.prepare()
        self.policy.judge=AsyncMock(return_value='{"text":"请等5小时额度重置"}')
        first=await self.owner.limits.limit_notice(event)
        self.assertNotIn('小时',first)
        self.assertEqual(await self.owner.limits.limit_notice(event),first)
        self.assertEqual(self.policy.judge.await_count,1)
