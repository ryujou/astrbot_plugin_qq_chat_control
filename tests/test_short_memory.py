import asyncio
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from astrbot.core.utils.active_event_registry import active_event_registry
from astrbot_plugin_qq_chat_control.memory import ShortMemory


class ShortMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_archives_only_in_configured_groups(self):
        rows = [NS(user_id=origin, cid=cid) for origin, cid in [
            ('qq:GroupMessage:alice_demo-a', 'old-member'),
            ('qq:GroupMessage:demo-a', 'old-shared'),
            ('qq:GroupMessage:alice_demo-a', 'current'),
            ('qq:GroupMessage:bob_demo-a', 'busy'),
            ('qq:GroupMessage:alice_other', 'unmanaged'),
            ('qq:GroupMessage:alice_demo-ab', 'similar'),
            ('qq:FriendMessage:alice', 'private'),
            ('other:GroupMessage:alice_demo-a', 'other-platform')]]
        manager = NS(get_conversations=AsyncMock(return_value=rows),
            get_curr_conversation_id=AsyncMock(return_value='current'), delete_conversation=AsyncMock())
        with tempfile.TemporaryDirectory() as directory:
            memory = ShortMemory(NS(conversation_manager=manager), Path(directory) / 'sessions.json')
            memory.sessions = {r.cid: {} for r in rows}
            with patch.dict(active_event_registry._events, {'qq:GroupMessage:bob_demo-a': object()}):
                self.assertEqual(await memory.prune_archived({('qq', 'demo-a'): {}}), 2)
            self.assertEqual([c.args for c in manager.delete_conversation.await_args_list], [
                ('qq:GroupMessage:alice_demo-a', 'old-member'), ('qq:GroupMessage:demo-a', 'old-shared')])
            retained = json.loads(memory.path.read_text(encoding='utf-8'))
            self.assertEqual(set(retained), {r.cid for r in rows} - {'old-member', 'old-shared'})

    async def test_no_rules_do_not_read_or_delete_conversations(self):
        manager = NS(get_conversations=AsyncMock(), delete_conversation=AsyncMock())
        with tempfile.TemporaryDirectory() as directory:
            memory = ShortMemory(NS(conversation_manager=manager), Path(directory) / 'sessions.json')
            self.assertEqual(await memory.prune_archived({}), 0)
        manager.get_conversations.assert_not_awaited()
        manager.delete_conversation.assert_not_awaited()

    async def test_prune_yields_to_database_writer_and_keeps_scope_and_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'data'
            path.mkdir()
            db = sqlite3.connect(path / 'data_v4.db')
            try:
                db.execute('CREATE TABLE platform_message_history '
                           '(id INTEGER, platform_id TEXT, user_id TEXT, created_at TEXT)')
                db.executemany('INSERT INTO platform_message_history VALUES (?,?,?,?)', [
                    (1, 'qq', 'demo-a', '2023-11-14 21:00:00'),
                    (2, 'qq', 'demo-a', '2023-11-14 22:10:00'),
                    (3, 'qq', 'demo-a', '2023-11-14 22:11:00'),
                    (4, 'qq', 'other', '2023-11-14 21:00:00'),
                    (5, 'other', 'demo-a', '2023-11-14 21:00:00')])
                db.commit()
                db.execute('BEGIN IMMEDIATE')
                memory = ShortMemory(NS(get_registered_star=lambda _: None), Path(directory) / 'sessions.json')
                event = NS(unified_msg_origin='qq:GroupMessage:alice_demo-a',
                    get_platform_id=lambda: 'qq', get_group_id=lambda: 'demo-a')

                async def release_writer():
                    await asyncio.sleep(.05)
                    db.commit()

                release = asyncio.create_task(release_writer())
                old_cwd = os.getcwd()
                started = time.monotonic()
                try:
                    os.chdir(directory)
                    await memory.prune_group(event, {'group_context_minutes': 30, 'group_context_messages': 1}, now=1700000000)
                finally:
                    os.chdir(old_cwd)
                    await release
                self.assertLess(time.monotonic() - started, 1)
                self.assertEqual(db.execute('SELECT id FROM platform_message_history ORDER BY id').fetchall(), [(3,), (4,), (5,)])
            finally:
                db.close()

    async def test_database_failure_is_reported(self):
        memory = ShortMemory(NS(get_registered_star=lambda _: None), Path('unused-sessions.json'))
        event = NS(unified_msg_origin='qq:GroupMessage:alice_demo-a',
                   get_platform_id=lambda: 'qq', get_group_id=lambda: 'demo-a')
        with patch.object(memory, 'prune_records', side_effect=sqlite3.OperationalError('synthetic write failure')):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'synthetic write failure'):
                await memory.prune_group(event, {'group_context_minutes': 30, 'group_context_messages': 30})


if __name__ == '__main__':
    unittest.main()
