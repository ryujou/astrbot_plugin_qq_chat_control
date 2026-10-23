import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import test_group_adaptation as fixture
from astrbot_plugin_qq_chat_control.background import Excluded
from astrbot_plugin_qq_chat_control.interaction import rank_candidates


class BackgroundTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.AdaptationTests.asyncSetUp
    event = fixture.AdaptationTests.event
    bound = fixture.AdaptationTests.bound

    def setup_background(self):
        self.scope = ('qq_private', '10006')
        self.rules[self.scope].update(group_background=True, candidate_replies=True, banter_intensity=60)
        self.bg = self.learning.background
        self.choose = self.policy.interaction
        return self.scope

    def candidates(self):
        return [dict(id=i, text=t, style=s, intensity=40, sticker_id=None) for i, t, s in
                [(1, '这段演出确实有意思', '接话'), (2, '我投隔壁偶像一票', '跨作品反串'), (3, '你们最喜欢哪场现场', '递话题')]]

    def review(self, uncertain=False):
        return dict(items=[dict(id=i, fit=4, engagement=4, natural=4, accuracy=4, novelty=4,
                               eligible=True, uncertain=uncertain, reason='适合本群当前话题') for i in (1, 2, 3)])

    async def test_size_type_pixel_exclusions_before_download(self):
        scope = self.setup_background()
        for item in [dict(name='clip.mp4', size=1), dict(name='image.jpg', size=11 * 1024**2),
                     dict(name='doc.pdf', size=21 * 1024**2), dict(name='photo.png', width=5000, height=5000),
                     dict(name='cover.jpg', video=True, size=1)]:
            self.assertTrue(self.bg.exclusion(scope, item))
        self.assertEqual(self.bg.exclusion(scope, dict(name='doc.pdf', size=20 * 1024**2)), '')
        self.bg.download = AsyncMock()
        self.bg.catalog(scope, 'file', [dict(id='a', name='clip.mp4', size=1)])
        self.assertEqual(self.bg.db.execute('SELECT status FROM group_sources').fetchone()[0], 'excluded')
        self.bg.download.assert_not_called()

    async def test_album_pagination_and_video_exclusion(self):
        scope = self.setup_background()
        self.bg.call = AsyncMock(side_effect=[
            dict(album_list=[dict(album_id='a', name='作品相册')], has_more=True, attach_info='next'),
            dict(media_list=[dict(type=1, video={}, client_key='v', file_size=1)], next_has_more=False),
            dict(album_list=[dict(album_id='b', name='第二页')], has_more=False),
            dict(media_list=[], next_has_more=False)])
        await self.bg.scan_albums(scope)
        self.assertEqual(self.bg.call.call_count, 4)
        self.assertEqual(self.bg.db.execute('SELECT status FROM group_sources').fetchone()[0], 'excluded')

    async def test_file_scan_bounded_by_reported_total(self):
        scope = self.setup_background()
        calls = []
        async def call(scope, action, **params):
            calls.append((action, params))
            if action == 'get_group_file_system_info':
                return dict(file_count=234)
            # The live API can keep returning full pages beyond the end.
            start = params['start_index']
            return dict(files=[dict(file_id=str(i), file_name=f'{i}.mp4', file_size=1)
                               for i in range(start, start + params['file_count'])], folders=[])
        self.bg.call = call
        await self.bg.scan_files(scope)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[-1][1]['file_count'], 234)
        self.assertEqual(self.bg.db.execute('SELECT COUNT(*) FROM group_sources').fetchone()[0], 234)

    async def test_incomplete_file_count_does_not_replace_existing(self):
        scope = self.setup_background()
        self.bg.catalog(scope, 'file', [dict(id='old', name='old.txt')])
        self.bg.call = AsyncMock(side_effect=[dict(file_count=2), dict(files=[], folders=[])])
        with self.assertRaises(ValueError):
            await self.bg.scan_files(scope)
        self.assertEqual(self.bg.db.execute('SELECT source_id FROM group_sources').fetchone()[0], 'old')

    async def test_file_page_counts_folders_as_entries(self):
        scope = self.setup_background()
        def file(i):
            return dict(file_id=str(i), file_name=f'{i}.txt', file_size=1)
        root = [dict(folder_id='a'), dict(folder_id='b')] + [file(i) for i in range(366)]
        async def call(scope, action, **params):
            if action == 'get_group_file_system_info':
                return dict(file_count=371)
            source = root if action == 'get_group_root_files' else ([file(i) for i in range(366,369)] if params['folder_id']=='a' else [file(i) for i in range(369,371)])
            page = source[params['start_index']:params['start_index']+params['file_count']]
            return dict(files=[x for x in page if 'file_id' in x], folders=[x for x in page if 'folder_id' in x])
        self.bg.call = call
        await self.bg.scan_files(scope)
        self.assertEqual(self.bg.db.execute('SELECT COUNT(*) FROM group_sources').fetchone()[0], 371)

    async def test_all_group_backgrounds_precede_document_processing(self):
        scope = self.setup_background()
        other = ('qq_private', '10003')
        self.rules[other]['group_background'] = True
        order = []
        for group in (scope, other):
            self.bg.catalog(group, 'file', [dict(id='x', name='x.txt')])
        async def refresh(group):
            order.append(('refresh', group))
        async def process(group, row):
            order.append(('process', group))
        self.bg.refresh = refresh
        self.bg.process = process
        self.bg.habits = AsyncMock()
        await self.bg.tick()
        self.assertEqual(order[:2], [('refresh', scope), ('refresh', other)])

    async def test_partial_scan_preserves_existing_catalog(self):
        scope = self.setup_background()
        self.bg.catalog(scope, 'album', [dict(id='old', name='a.jpg')])
        self.bg.call = AsyncMock(side_effect=[dict(album_list=[], has_more=True, attach_info='same'),
                                             dict(album_list=[], has_more=True, attach_info='same')])
        with self.assertRaises(ValueError):
            await self.bg.scan_albums(scope)
        self.assertEqual(self.bg.db.execute('SELECT COUNT(*) FROM group_sources').fetchone()[0], 1)

    async def test_catalog_updates_and_deletes_chunks_without_cross_group(self):
        scope = self.setup_background()
        other = ('qq_private', '10003')
        item = dict(id='same', name='note.txt', updated=1, size=5)
        for group in [scope, other]:
            self.bg.catalog(group, 'file', [item])
            self.bg.db.execute('INSERT INTO group_chunks VALUES (?,?,?,?,?,?,?)', (*group, 'same', 1, '旧内容', '[1,0]', 'test_embedding'))
        self.bg.catalog(scope, 'file', [dict(item, updated=2)])
        self.assertEqual(self.bg.db.execute('SELECT group_id FROM group_chunks').fetchone()[0], other[1])
        self.bg.catalog(scope, 'file', [])
        self.assertEqual(self.bg.db.execute('SELECT group_id FROM group_sources').fetchone()[0], other[1])

    async def test_notice_change_rebuild_and_failure_preserves(self):
        scope = self.setup_background()
        self.rules[scope].update(group_file_learning=False, group_album_learning=False)
        self.policy.judge = AsyncMock(return_value=json.dumps(dict(summary='音乐兴趣群，名称只是线索', sources=['group_name'])))
        self.bg.call = AsyncMock(side_effect=[dict(group_name='音乐群'), [dict(notice_id='n', message=dict(text='讨论现场'), publish_time=1)]])
        await self.bg.refresh(scope)
        self.assertEqual(self.bg.state(scope)['name'], '音乐群')
        self.bg.call = AsyncMock(side_effect=RuntimeError('offline'))
        await self.bg.refresh(scope)
        state = self.bg.state(scope)
        self.assertEqual(state['notices'][0]['text'], '讨论现场')
        self.assertIn('background', state['errors'])

    async def test_habits_require_two_people_and_distinct_fragments(self):
        scope = self.setup_background()
        e1 = self.event(sender='alice', text='喜欢音乐反串')
        e2 = self.event(sender='bob', text='我也接这个梗')
        self.learning.db.execute('UPDATE messages SET ts=ts-360 WHERE message_id=?', (e1.get_message_id() if hasattr(e1, 'get_message_id') else '1',))
        self.policy.judge = AsyncMock(return_value=json.dumps(dict(habits=[dict(text='可以跨作品互相接梗', message_ids=['1', '2'])])))
        await self.bg.habits(scope)
        self.assertEqual(len(self.bg.state(scope)['habits']), 1)

    async def test_weighted_review_and_intensity(self):
        self.setup_background()
        c = self.candidates()
        self.assertIsNone(rank_candidates(c, self.review(), 20, []))
        self.assertIsNotNone(rank_candidates(c, self.review(), 60, []))
        r = self.review()
        for v in r['items']:
            v.update(fit=1, engagement=1)
        self.assertIsNone(rank_candidates(c, r, 60, []))

    async def test_candidate_sends_exactly_selected_and_records_real_delivery(self):
        scope = self.setup_background()
        event = self.event(text='这场演出如何')
        event.set_extra('qq_chat_automatic', True)
        self.bg.context = AsyncMock(return_value={})
        self.stickers.ranked_pool = AsyncMock(return_value=[])
        self.choose.generate = AsyncMock(return_value=dict(items=self.candidates()))
        self.policy.judge = AsyncMock(return_value=json.dumps(self.review()))
        with self.bound(event):
            self.learning.prepare(event)
            self.assertTrue(await self.policy.prepare(event, NS(prompt='演出', system_prompt='群友', conversation=None)))
        event.send.assert_awaited_once()
        row = self.choose.db.execute('SELECT * FROM interaction_choices').fetchone()
        selected = next(c for c in json.loads(row['candidates']) if c['id'] == row['selected'])
        self.assertEqual(event.send.call_args.args[0].chain[0].text, selected['text'])
        reply = self.learning.db.execute('SELECT * FROM replies').fetchone()
        self.assertEqual(reply['generated'], 1)
        self.assertEqual(reply['finished'], 1)
        self.assertIsNone(self.learning.db.execute('SELECT propensity FROM reply_actions').fetchone()[0])

    async def test_invalid_candidates_are_rejected_individually(self):
        scope = self.setup_background()
        self.bg.context = AsyncMock(return_value={})
        self.stickers.ranked_pool = AsyncMock(return_value=[])
        candidates = self.candidates()
        candidates[0]['intensity'] = 100
        candidates[1]['sticker_id'] = 'invented'
        self.choose.generate = AsyncMock(return_value=dict(items=candidates))
        self.policy.judge = AsyncMock(return_value=json.dumps(dict(items=self.review()['items'][2:])))
        record = {}
        selected = await self.choose.choose(scope, NS(system_prompt='群友'), [], record)
        self.assertEqual(selected['id'], 3)
        self.assertTrue(record['candidates'][0]['rejected'])
        self.assertTrue(record['candidates'][1]['rejected'])
        candidates[2]['text'] = ''
        self.policy.judge.reset_mock()
        self.assertIsNone(await self.choose.choose(scope, NS(system_prompt='群友'), [], {}))
        self.policy.judge.assert_not_awaited()

    async def test_stale_or_uncertain_candidate_is_not_sent(self):
        scope = self.setup_background()
        self.bg.context = AsyncMock(return_value={})
        self.stickers.ranked_pool = AsyncMock(return_value=[])
        self.choose.generate = AsyncMock(return_value=dict(items=self.candidates()))
        self.policy.judge = AsyncMock(side_effect=[json.dumps(self.review(True)), json.dumps(self.review(True))])
        record = {}
        selected = await self.choose.choose(scope, NS(system_prompt='群友'), [], record)
        self.assertIsNone(selected)
        self.assertTrue(self.policy.judge.call_args.kwargs['recheck'])

    async def test_topic_change_cancels(self):
        scope = self.setup_background()
        self.bg.context = AsyncMock(return_value={})
        self.stickers.ranked_pool = AsyncMock(return_value=[])
        self.choose.generate = AsyncMock(return_value=dict(items=self.candidates()))
        self.policy.judge = AsyncMock(side_effect=[json.dumps(self.review()), json.dumps(dict(send=False, confidence=.99))])
        self.choose.messages = lambda scope: [dict(message_id='new', text='别说了，换话题')]
        record = {}
        selected = await self.choose.choose(scope, NS(system_prompt='群友'), [], record)
        self.assertIsNone(selected)
        self.assertIn('话题', record['reason'])

    async def test_elapsed_budget_cancels_before_generation(self):
        self.setup_background()
        event = self.event()
        event.set_extra('qq_chat_automatic', True)
        event.set_extra('qq_candidate_started', time.monotonic() - 61)
        async def slow(*args):
            import asyncio
            await asyncio.sleep(1)
        self.choose.choose = slow
        with self.bound(event):
            self.learning.prepare(event)
            await self.policy.prepare(event, NS(prompt='', system_prompt='', conversation=None))
        event.send.assert_not_called()
        self.assertIn('60秒', self.choose.db.execute('SELECT reason FROM interaction_choices').fetchone()[0])

    async def test_download_limit_cleans_temporary_file(self):
        scope = self.setup_background()
        self.bg.catalog(scope, 'file', [dict(id='x', name='x.txt', file_id='x', busid=1)])
        self.bg.call = AsyncMock(return_value=dict(url='http://unused'))
        paths = []
        async def too_large(url, path, limit):
            paths.append(path)
            path.write_bytes(b'partial')
            raise Excluded('下载超过文件大小上限')
        self.bg.download = too_large
        row = self.bg.db.execute('SELECT * FROM group_sources').fetchone()
        await self.bg.process(scope, row)
        self.assertFalse(paths[0].exists())
        self.assertEqual(self.bg.db.execute('SELECT status FROM group_sources').fetchone()[0], 'excluded')

    async def test_real_chunked_download_stops_at_limit(self):
        from aiohttp import web
        self.setup_background()
        async def stream(request):
            response = web.StreamResponse()
            await response.prepare(request)
            await response.write(b'x' * 4096)
            await response.write_eof()
            return response
        app = web.Application(); app.router.add_get('/', stream)
        runner = web.AppRunner(app); await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, '127.0.0.1', 0); await site.start()
        port = site._server.sockets[0].getsockname()[1]
        path = Path(self.tmp.name) / 'download.txt'
        with self.assertRaises(Excluded):
            await self.bg.download(f'http://127.0.0.1:{port}/', path, 1024)
        self.assertLessEqual(path.stat().st_size, 1024)

    async def test_source_read_cursor_survives_restart(self):
        scope = self.setup_background()
        self.bg.catalog(scope, 'file', [dict(id='x', name='x.txt', file_id='x', busid=1)])
        self.bg.call = AsyncMock(return_value=dict(url='http://unused'))
        async def download(url, path, limit):
            path.write_text('正文' * 6000, encoding='utf-8')
        self.bg.download = download
        await self.bg.process(scope, self.bg.db.execute('SELECT * FROM group_sources').fetchone())
        self.assertEqual(self.bg.db.execute('SELECT cursor FROM group_sources').fetchone()[0], 8)
        await self.bg.process(scope, self.bg.db.execute('SELECT * FROM group_sources').fetchone())
        self.assertEqual(self.bg.db.execute('SELECT status FROM group_sources').fetchone()[0], 'ready')
        self.assertEqual(self.bg.db.execute('SELECT COUNT(*) FROM group_chunks').fetchone()[0], 10)

    async def test_retrieval_does_not_read_disabled_or_other_group_sources(self):
        scope = self.setup_background()
        for group in [scope, ('qq_private', '10003')]:
            self.bg.catalog(group, 'file', [dict(id='x', name='x.txt')])
            self.bg.db.execute('UPDATE group_sources SET status="ready" WHERE group_id=?', (group[1],))
            self.bg.db.execute('INSERT INTO group_chunks VALUES (?,?,?,?,?,?,?)', (*group, 'x', 1, group[1], '[1,0]', 'test_embedding'))
        result = await self.bg.context(scope, '话题')
        self.assertEqual([s['text'] for s in result['sources']], ['10006'])
        self.rules[scope]['group_file_learning'] = False
        self.assertEqual((await self.bg.context(scope, '话题'))['sources'], [])
