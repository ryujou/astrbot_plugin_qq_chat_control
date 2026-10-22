import base64
import json
import mimetypes
import time
from pathlib import Path

from quart import jsonify, request

from .evaluation import comparison

from astrbot.api import logger
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import AiocqhttpAdapter


class LearningWeb:
    def __init__(self, owner):
        self.owner = owner

    def register(self):
        for name in ('bootstrap', 'overview', 'replies', 'reply', 'stickers', 'image', 'slang', 'members'):
            handler = getattr(self, name)

            async def view(handler=handler):
                try:
                    return jsonify(ok=True, data=await handler())
                except ValueError as error:
                    return jsonify(ok=False, message=str(error)), 400
                except FileNotFoundError:
                    return jsonify(ok=False, message='图片文件已不存在'), 404
                except Exception:
                    logger.exception('群聊学习看板读取失败')
                    return jsonify(ok=False, message='读取失败，请查看 AstrBot 日志'), 500

            self.owner.context.register_web_api('/astrbot_plugin_qq_chat_control/learning/' + name,
                                                view, ['GET'], '群聊学习看板：' + name)

    def scope(self):
        key = request.args.get('platform', ''), request.args.get('group_id', '')
        if key not in self.owner.rules:
            raise ValueError('请选择已配置的群')
        return (*key, self.owner.rules[key].get('persona_id', ''))

    def page(self):
        page = int(request.args.get('page', '1'))
        if page < 1:
            raise ValueError('页码必须大于0')
        return page, 20, (page - 1) * 20

    @property
    def db(self):
        return self.owner.learning.open()

    def members_for(self, scope):
        prefix = ':'.join(scope[:2]) + ':'
        return [dict(user_id=key[len(prefix):], name=value['name'], profile=value['profile'],
                     affinity=value['affinity'], last_update=value['last_update'], last_seen=value['last_seen'],
                     pending_samples=len(value['samples']))
                for key, value in self.owner.profiles.records.items() if key.startswith(prefix)]

    def terms_for(self, scope):
        items = []
        for row in self.db.execute('SELECT term,senses,updated FROM slang_terms WHERE platform=? AND group_id=? '
                                   'AND persona=? ORDER BY updated DESC', scope):
            senses = json.loads(row['senses'])
            for sense in senses:
                sense['confirmed'] = (sense['rounds'] >= 2 and len(sense['evidence']) >= 3
                    and len({e['sender'] for e in sense['evidence']}) >= 2 and sense['confidence'] >= .8)
            items.append(dict(term=row['term'], senses=senses, updated=row['updated']))
        return items

    async def bootstrap(self):
        live, accounts, connection_errors = {}, [], []
        for inst in self.owner.context.platform_manager.platform_insts:
            if not isinstance(inst, AiocqhttpAdapter):
                continue
            platform = inst.meta().id
            try:
                client = inst.get_client()
                login = await client.call_action('get_login_info')
                groups = await client.call_action('get_group_list')
                accounts.append(dict(platform=platform, user_id=str(login['user_id']), name=login.get('nickname', '')))
                live[platform] = {str(group['group_id']): group for group in groups}
            except Exception:
                logger.exception('学习看板查询 QQ 连接失败：%s', platform)
                connection_errors.append(platform)
        groups = []
        for (platform, group_id), rule in self.owner.rules.items():
            info = live.get(platform, {}).get(group_id, {})
            allowed = group_id in self.owner.config['allowed_group_ids']
            groups.append(dict(platform=platform, group_id=group_id, name=info.get('group_name') or '群 ' + group_id,
                member_count=info.get('member_count'), joined=group_id in live[platform] if platform in live else None,
                allowed=allowed, persona=rule.get('persona_id', ''), provider=rule.get('provider_id', ''),
                mode=rule.get('feedback_adapt_mode', '关闭'), feedback=bool(rule.get('feedback_observe')),
                slang=bool(rule.get('slang_learning')), stickers=bool(rule.get('sticker_learning')),
                profiles=bool(rule.get('member_profiles'))))
        return dict(groups=groups, accounts=accounts, connection_errors=connection_errors, updated=time.time())

    async def overview(self):
        scope = self.scope()
        replies = dict(self.db.execute('SELECT COUNT(*) total,SUM(status=\'settled\') settled,'
            'SUM(status=\'observing\') observing,AVG(score) mean_score FROM replies '
            'WHERE platform=? AND group_id=? AND persona=?', scope).fetchone())
        images = dict(self.db.execute('SELECT status,COUNT(*) FROM stickers WHERE platform=? AND group_id=? '
                                     'AND persona=? GROUP BY status', scope).fetchall())
        members, terms = self.members_for(scope), self.terms_for(scope)
        state = self.owner.learning.policy.state(scope[:2])
        actions = self.db.execute('SELECT COUNT(*),SUM(updated) FROM reply_actions WHERE platform=? '
                                 'AND group_id=? AND persona=?', scope).fetchone()
        evaluation = comparison(self.owner.learning, scope)
        topics = list(dict.fromkeys(clue for item in terms for sense in item['senses'] if sense['confirmed']
                                   for clue in sense['clues']))
        activity = [dict(row) for row in self.db.execute("SELECT date(created,'unixepoch','+8 hours') day,COUNT(*) replies,"
            'AVG(score) score FROM replies WHERE platform=? AND group_id=? AND persona=? AND created>=? '
            "GROUP BY date(created,'unixepoch','+8 hours') ORDER BY day", (*scope, time.time() - 7 * 86400))]
        styles = [dict(name=key, samples=arm['n']) for key, arm in state['arms'].items()]
        recent = [self.reply_row(row) for row in self.db.execute('SELECT r.*,a.action,a.updated trained FROM replies r '
            'LEFT JOIN reply_actions a ON a.reply_id=r.id WHERE r.platform=? AND r.group_id=? AND r.persona=? '
            'ORDER BY r.created DESC LIMIT 5', scope)]
        return dict(replies=replies, images=images, members=len(members),
            profiled_members=sum(bool(m['profile']) for m in members), terms=len(terms),
            confirmed_terms=sum(any(s['confirmed'] for s in item['senses']) for item in terms),
            actions=actions[0], processed=actions[1] or 0, trained=sum(arm['n'] for arm in state['arms'].values()),
            reviews=state['reviews'], last_review=state['last_review'], lessons=state['lessons'],
            topics=topics, styles=styles, comparison=evaluation['phases'][:2], evaluation=evaluation, activity=activity, recent=recent, updated=time.time())

    @staticmethod
    def reply_row(row):
        data = dict(row)
        for key in ('summary', 'context', 'action'):
            if key in data:
                data[key] = json.loads(data[key]) if data[key] else None
        return data

    async def replies(self):
        scope = self.scope()
        page, size, offset = self.page()
        query, values = 'r.platform=? AND r.group_id=? AND r.persona=?', list(scope)
        search, outcome = request.args.get('q', '').strip(), request.args.get('outcome', '')
        if search:
            query += ' AND r.body LIKE ?'
            values.append('%' + search + '%')
        if outcome:
            if outcome not in {'feedback', 'active_no_response', 'uncertain', 'insufficient', 'partial_delivery', 'analysis_failed', 'observing'}:
                raise ValueError('未知结算状态')
            query += ' AND ' + ('r.status=?' if outcome == 'observing' else 'r.outcome=?')
            values.append(outcome)
        total = self.db.execute('SELECT COUNT(*) FROM replies r WHERE ' + query, values).fetchone()[0]
        rows = self.db.execute('SELECT r.*,a.action,a.updated trained FROM replies r LEFT JOIN reply_actions a '
            'ON a.reply_id=r.id WHERE ' + query + ' ORDER BY r.created DESC LIMIT ? OFFSET ?', (*values, size, offset))
        return dict(items=[self.reply_row(row) for row in rows], total=total, page=page, page_size=size)

    async def reply(self):
        scope = self.scope()
        reply_id = int(request.args.get('id', '0'))
        row = self.db.execute('SELECT r.*,a.action,a.updated trained,a.propensity,a.candidates FROM replies r '
            'LEFT JOIN reply_actions a ON a.reply_id=r.id WHERE r.platform=? AND r.group_id=? AND r.persona=? '
            'AND r.id=?', (*scope, reply_id)).fetchone()
        if row is None:
            raise ValueError('本群没有这条回复')
        result = self.reply_row(row)
        result['candidates'] = json.loads(result['candidates']) if result['candidates'] else []
        result['outgoing'] = [r[0] for r in self.db.execute('SELECT message_id FROM outgoing WHERE platform=? '
            'AND group_id=? AND reply_id=?', (*scope[:2], reply_id))]
        result['feedback'] = [dict(r) for r in self.db.execute('SELECT f.*,m.sender,m.text,m.parent,m.ts '
            'FROM feedback f LEFT JOIN messages m ON m.platform=f.platform AND m.group_id=f.group_id AND m.message_id=f.message_id '
            'WHERE f.platform=? AND f.group_id=? AND f.reply_id=? ORDER BY m.ts', (*scope[:2], reply_id))]
        return result

    async def stickers(self):
        scope = self.scope()
        page, size, offset = self.page()
        query, values = 'platform=? AND group_id=? AND persona=?', list(scope)
        status, search = request.args.get('status', ''), request.args.get('q', '').strip()
        if status:
            if status not in {'ready', 'pending', 'rejected', 'failed'}:
                raise ValueError('未知图片状态')
            query += ' AND status=?'
            values.append(status)
        if search:
            query += ' AND (description LIKE ? OR visible_text LIKE ? OR scene LIKE ?)'
            values.extend(['%' + search + '%'] * 3)
        total = self.db.execute('SELECT COUNT(*) FROM stickers WHERE ' + query, values).fetchone()[0]
        rows = self.db.execute('SELECT id,status,created,seen,description,visible_text,tags,scene,uses,last_used,reward_sum,reward_n '
            'FROM stickers WHERE ' + query + ' ORDER BY created DESC LIMIT ? OFFSET ?', (*values, size, offset))
        items = [dict(row) for row in rows]
        for item in items:
            item['tags'] = json.loads(item['tags']) if item['tags'] else []
        return dict(items=items, total=total, page=page, page_size=size)

    async def image(self):
        scope = self.scope()
        row = self.db.execute("SELECT path FROM stickers WHERE platform=? AND group_id=? AND persona=? AND id=? AND status='ready'",
                              (*scope, int(request.args.get('id', '0')))).fetchone()
        if row is None:
            raise ValueError('本群没有这张可用表情包')
        root = (self.owner.learning.policy.stickers.root / scope[0] / scope[1]).resolve()
        path = Path(row['path']).resolve()
        if not path.is_relative_to(root):
            raise ValueError('图片不属于本群图库')
        mime = mimetypes.guess_type(path.name)[0]
        return dict(src='data:' + mime + ';base64,' + base64.b64encode(path.read_bytes()).decode('ascii'))

    async def slang(self):
        return dict(items=self.terms_for(self.scope()))

    async def members(self):
        items = self.members_for(self.scope())
        search = request.args.get('q', '').strip().lower()
        items = [m for m in items if not search or search in (m['user_id'] + m['name'] + m['profile']).lower()]
        items.sort(key=lambda m: m['last_seen'], reverse=True)
        page, size, offset = self.page()
        return dict(items=items[offset:offset + size], total=len(items), page=page, page_size=size)
