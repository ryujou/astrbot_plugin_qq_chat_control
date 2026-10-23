import random


class ReplyOpportunities:
    def __init__(self, learning):
        self.learning = learning
        self.active = {}

    def recover(self, db):
        self.active.clear()
        with db:
            db.execute("UPDATE auto_reply_state SET status=CASE WHEN status='running' AND attempt=1 "
                       "THEN 'retry' ELSE 'idle' END WHERE status IN ('running','sending')")

    def state(self, scope):
        row = self.learning.open().execute('SELECT count,status,attempt,serial FROM auto_reply_state '
                                           'WHERE platform=? AND group_id=?', scope).fetchone()
        return dict(row) if row else dict(count=0, status='idle', attempt=0, serial=0)

    def save(self, scope, state):
        with self.learning.open():
            self.learning.db.execute('INSERT OR REPLACE INTO auto_reply_state VALUES (?,?,?,?,?,?)',
                (*scope, state['count'], state['status'], state['attempt'], state['serial']))

    def take(self, event, rule, blocked=False):
        scope = event.get_platform_id(), str(event.get_group_id())
        state = self.state(scope)
        threshold = int(rule.get('message_count', 10))
        state['count'] = min(threshold, state['count'] + 1)
        take = False
        if not blocked and state['status'] == 'retry':
            state.update(status='running', attempt=2, serial=state['serial'] + 1)
            take = True
        elif not blocked and state['status'] == 'idle' and state['count'] >= threshold:
            state['count'] = 0
            if random.random() < float(rule.get('reply_probability', 30)) / 100:
                state.update(status='running', attempt=1, serial=state['serial'] + 1)
                take = True
        self.save(scope, state)
        if take:
            self.active[scope] = state['serial']
            event.set_extra('qq_auto_opportunity', state['serial'])
        return take

    def sending(self, event):
        scope = event.get_platform_id(), str(event.get_group_id())
        token = event.get_extra('qq_auto_opportunity')
        if token is None or self.active.get(scope) != token:
            return
        state = self.state(scope)
        state['status'] = 'sending'
        self.save(scope, state)

    def finish(self, event, technical=False):
        scope = event.get_platform_id(), str(event.get_group_id())
        token = event.get_extra('qq_auto_opportunity')
        if token is None or self.active.get(scope) != token:
            return
        self.active.pop(scope)
        state = self.state(scope)
        retry = technical and state['status'] == 'running' and state['attempt'] == 1
        state['status'] = 'retry' if retry else 'idle'
        self.save(scope, state)

    def cancel(self, scope):
        self.active.pop(scope, None)
        state = self.state(scope)
        state['status'] = 'idle'
        self.save(scope, state)

    def clear(self, scope):
        self.active.pop(scope, None)
        with self.learning.open():
            self.learning.db.execute('DELETE FROM auto_reply_state WHERE platform=? AND group_id=?', scope)
