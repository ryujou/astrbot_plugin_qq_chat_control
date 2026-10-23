import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from astrbot_plugin_qq_chat_control.evaluation import comparison
from astrbot_plugin_qq_chat_control.learning import FeedbackLearning


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.scope = ('qq_private', 'a', 'persona')
        owner = NS(closing=False, rules={self.scope[:2]: {'persona_id': 'persona'}})
        self.learning = FeedbackLearning(owner, Path(self.tmp.name) / 'learning.db')
        self.db = self.learning.open()
        self.addCleanup(self.learning.close)
        self.now = 40 * 86400

    def sample(self, day, phase, participants=1, outcome='feedback', group='a', version='1.11.0', body='回复', mode='对照'):
        created = day * 86400 + (900 if phase == 'adaptive' else 0)
        summary = dict(new_participants=participants, annoyance_people=0, correction_people=0)
        score = None if outcome in {'uncertain', 'analysis_failed', 'partial_delivery'} else .2
        reply = self.db.execute('INSERT INTO replies(platform,group_id,persona,created,sent,completed,body,status,outcome,score,summary) '
            "VALUES ('qq_private',?,'persona',?,?,?,?,'settled',?,?,?)",
            (group, created, created+4, created+8, body, outcome, score, json.dumps(summary))).lastrowid
        action = dict(phase=phase, style='认真接话', format='text', mode=mode, policy_version=version)
        self.db.execute('INSERT INTO reply_actions(reply_id,platform,group_id,persona,created,action) VALUES (?,\'qq_private\',?,\'persona\',?,?)',
                        (reply, group, created, json.dumps(action)))
        self.db.commit()
        return reply

    def test_empty_and_unknown_samples_are_not_zero_quality(self):
        self.sample(39, 'adaptive', outcome='uncertain')
        report = comparison(self.learning, self.scope, self.now)
        adaptive = report['phases'][1]
        self.assertEqual((adaptive['total'], adaptive['samples'], adaptive['unknown']), (1,0,1))
        self.assertIsNone(adaptive['participants'])
        self.assertEqual(report['evidence']['status'], 'insufficient')

    def test_pairing_requires_same_version_and_control_mode(self):
        self.sample(37, 'baseline')
        self.sample(37, 'adaptive', version='different')
        self.sample(38, 'baseline')
        self.sample(38, 'adaptive', mode='学习')
        self.sample(39, 'baseline')
        self.sample(39, 'adaptive')
        report = comparison(self.learning, self.scope, self.now)
        self.assertEqual(report['evidence']['paired_periods'], 1)
        self.assertEqual(report['evidence']['paired_days'], 1)

    def test_day_cluster_trend_excludes_other_groups_and_invalid_samples(self):
        for day in range(33, 40):
            self.sample(day, 'baseline', participants=0)
            self.sample(day, 'adaptive', participants=2)
            self.sample(day, 'adaptive', participants=999, outcome='analysis_failed')
            self.sample(day, 'adaptive', participants=999, group='b')
        report = comparison(self.learning, self.scope, self.now)
        self.assertEqual(report['evidence']['status'], 'positive_trend')
        self.assertEqual(report['evidence']['interval'], [2.,2.])
        self.assertEqual(report['phases'][1]['failures'], 7)
        self.assertEqual(report['phases'][1]['samples'], 7)

    def test_pairing_uses_decision_time_after_request_wait(self):
        self.sample(39, 'baseline')
        reply = self.sample(39, 'adaptive')
        self.db.execute('UPDATE replies SET created=created-1800 WHERE id=?', (reply,))
        self.db.commit()
        report = comparison(self.learning, self.scope, self.now)
        self.assertEqual(report['evidence']['paired_periods'], 1)

    def test_repetition_latency_and_requested_are_separate(self):
        first = self.sample(39, 'adaptive', body='这句话真的挺有意思')
        second = self.sample(39, 'requested', body='这句话真的挺有意思！')
        created = self.db.execute('SELECT created FROM replies WHERE id=?',(first,)).fetchone()[0]
        self.db.execute('UPDATE replies SET created=?,sent=?,completed=? WHERE id=?',(created+30,created+34,created+38,second))
        self.db.commit()
        report = comparison(self.learning, self.scope, self.now)
        requested = report['phases'][2]
        self.assertEqual(requested['repetition'], 1)
        self.assertEqual(requested['first_seconds'], 4)
        self.assertEqual(requested['complete_seconds'], 8)
        self.assertEqual(report['evidence']['paired_periods'], 0)

    def test_token_coverage_and_shared_calls_are_reported_honestly(self):
        reply = self.sample(39, 'adaptive')
        for purpose, tokens, linked in [('reply', None, reply), ('feedback_recheck', 10, None)]:
            self.db.execute('INSERT INTO model_calls(platform,group_id,persona,created,purpose,provider,reply_id,seconds,success,input_tokens,cached_tokens,output_tokens) '
                "VALUES ('qq_private','a','persona',?,?, 'test',?,2,1,?,?,?)",
                (self.now-10,purpose,linked,tokens,tokens,tokens))
        report = comparison(self.learning, self.scope, self.now)
        costs = {row['purpose']: row for row in report['costs']}
        self.assertIsNone(costs['reply']['input_tokens'])
        self.assertEqual(costs['reply']['usage_known'], 0)
        self.assertEqual(costs['feedback_recheck']['usage_known'], 1)
        self.assertEqual(report['phase_costs'][0]['calls'], 1)

    def test_group_clear_includes_call_usage(self):
        self.db.execute("INSERT INTO model_calls(platform,group_id,persona) VALUES ('qq_private','a','persona')")
        self.db.execute("INSERT INTO model_calls(platform,group_id,persona) VALUES ('qq_private','b','persona')")
        self.db.commit()
        self.learning.clear_group('qq_private','a')
        self.assertEqual(self.db.execute('SELECT group_id FROM model_calls').fetchone()[0],'b')

    def test_versions_and_interrupted_calls_are_separate(self):
        self.sample(39, 'candidate', version='1.14.0')
        self.sample(39, 'candidate', version='1.16.0')
        for status in ('failed', 'interrupted', 'legacy'):
            self.db.execute("INSERT INTO model_calls(platform,group_id,persona,created,purpose,provider,success,status) VALUES ('qq_private','a','persona',?,'reaction_decide','test',0,?)", (self.now-1,status))
        report = comparison(self.learning, self.scope, self.now)
        self.assertEqual({v['version']:v['samples'] for v in report['versions']}, {'1.14.0':1,'1.16.0':1})
        cost = report['costs'][0]
        self.assertEqual((cost['failures'],cost['interrupted'],cost['legacy_failures']), (1,1,1))


if __name__ == '__main__':
    unittest.main()
