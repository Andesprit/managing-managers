"""Exercise the brief queue in real linked worktrees, directly and through Atelier's ACP runner.

No real agents, usage endpoints or GitHub: fixtures write stage results, make commits and stand in for gh.
"""
import copy
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CONDUITS = ROOT / '.atelier/conduits'
GIT_ENV = {**os.environ, 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_SYSTEM': '/dev/null',
           'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t',
           'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t',
           'CLAUDE_CODE_DISABLE_BACKGROUND_TASKS': '1'}
BRIEF = '''# Add {name}

## Goal
The project gains {name}.

## In scope
- Create {name}

## Out of scope
- Anything else

## Allowed paths
- {name}

## Definition of done
- {name} exists
- A reader understands {name}
'''
CHECK = {'command': 'true', 'exit_code': 0, 'summary': 'ok'}


def scorecard(name):
    return [{'id': 'exists', 'outcome': f'{name} exists', 'kind': 'measured',
             'command': f'test -f {name} && echo 1 || echo 0', 'direction': 'up', 'target': 1},
            {'id': 'clear', 'outcome': f'A reader understands {name}', 'kind': 'judged', 'how': 'Read it'}]


def plan(name):
    return {'summary': f'One task adds {name}', 'scorecard': scorecard(name),
            'tasks': [{'id': 'add', 'title': f'Add {name}', 'scope': [name], 'outcomes': ['exists', 'clear']}]}


READY = {'status': 'READY', 'summary': 'Added the file', 'checks': [CHECK]}
ACCEPT = {'verdict': 'ACCEPT', 'outcome': 'exists', 'reason': 'The file is there'}
CLEAN = {'summary': 'Meets the brief', 'checks': [CHECK], 'findings': [], 'suggestions': ['Add a README']}
FINISH = {'summary': 'Done', 'judged': [{'id': 'clear', 'state': 'met', 'evidence': 'Read it'}],
          'questions': [], 'pr_title': 'Add the file', 'pr_body': '# What\n- Adds the file'}


def finding(severity):
    return {'severity': severity, 'about': 'brief', 'finding': f'A {severity} problem', 'fix': 'It is gone'}


def git(cwd, *args):
    return subprocess.check_output(['git', *args], cwd=cwd, env=GIT_ENV, text=True, stderr=subprocess.PIPE).strip()


USAGE_STUB = '''import json, os
from pathlib import Path
Path(os.environ['GOAL_USAGE_FILE']).write_text(json.dumps({'remaining': [99, 99, 99]}))
'''
# Stand-in for the GitHub CLI: logs each call and answers like gh would.
FAKE_GH = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
home, args = Path(os.environ['FAKE_GH_DIR']), sys.argv[1:]
with open(home / 'calls.jsonl', 'a') as out:
    out.write(json.dumps(args) + '\\n')
if args[:2] == ['repo', 'view']:
    print('main')
elif args[:2] == ['pr', 'create']:
    n = len((home / 'calls.jsonl').read_text().splitlines())
    print(f'Creating pull request\\nhttps://github.com/o/r/pull/{n}')
'''


class Repo(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.main, self.wt = self.tmp / 'main', self.tmp / 'wt'
        git(self.tmp, 'init', '-q', '-b', 'main', 'main')
        (self.main / 'a.txt').write_text('a\n')
        git(self.main, 'add', 'a.txt')
        git(self.main, 'commit', '-qm', 'initial')
        git(self.main, 'worktree', 'add', '-q', str(self.wt), '-b', 'mm-run')
        self.base = git(self.wt, 'rev-parse', 'HEAD')
        shutil.copytree(CONDUITS, self.wt / '.atelier/conduits', ignore=shutil.ignore_patterns('__pycache__'))
        self.scripts = self.wt / '.atelier/conduits/mm_brief/scripts'
        (self.scripts / 'usage.py').write_text(USAGE_STUB)
        self.env = dict(GIT_ENV)

    def brief(self, identifier, name):
        path = self.wt / '.atelier/briefs' / f'{identifier}.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(BRIEF.format(name=name))

    def cmd(self, *args):
        return subprocess.run([sys.executable, str(self.scripts / 'run.py'), *map(str, args)],
                              cwd=self.wt, env=self.env, capture_output=True, text=True)

    def ok(self, *args):
        r = self.cmd(*args)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def state(self, identifier='01-b'):
        return json.loads((self.wt / f'.atelier/mm/briefs/{identifier}/state.json').read_text())

    def queue(self):
        return json.loads((self.wt / '.atelier/mm/queue.json').read_text())

    def route(self, expected):
        self.assertEqual(self.ok('route', 50, 30, 30, 1, 'all', 5), expected)

    def result(self, value, expected='CONTINUE'):
        state = self.state(self.queue_active())
        value = copy.deepcopy(value)
        value.setdefault('request_id', state['request']['id'])
        value.setdefault('head', git(self.wt, 'rev-parse', 'HEAD'))
        (self.wt / f'.atelier/mm/briefs/{state["id"]}/result.json').write_text(json.dumps(value))
        self.assertEqual(self.ok('apply'), expected)

    def queue_active(self):
        return next(b['id'] for b in self.queue()['briefs'] if b['status'] == 'ACTIVE')

    def commit(self, name='b.txt', body=None):
        (self.wt / name).write_text(body or name + '\n')
        git(self.wt, 'add', name)
        git(self.wt, 'commit', '-qm', 'add ' + name)

    def build(self, name='b.txt'):
        """Init, start and run one brief up to its first review."""
        self.brief('01-b', name)
        self.ok('init', 1, 6, 6, 'false')
        self.assertEqual(self.ok('start'), '01-b')
        self.route('MANAGE')
        self.result(plan(name))
        self.route('WORK')
        self.commit(name)
        self.result(READY)
        self.route('MANAGE')
        self.result(ACCEPT)
        self.route('REVIEW')

    def review_round(self, findings):
        """One review that leaves findings, then triage to the task and a fix commit."""
        self.result({**CLEAN, 'findings': findings})
        self.route('MANAGE')
        n = len(self.state()['reviews'][-1]['blocking'])
        self.result({'fixes': [{'task': 'add', 'findings': list(range(n)), 'direction': 'Fix them'}]})
        self.route('WORK')
        self.commit('b.txt', f'fix {self.state()["round"]}\n')
        self.result(READY)
        self.route('REVIEW')


class Queue(Repo):
    def test_refuses_main_checkout_dirty_tree_and_missing_briefs(self):
        self.assertIn('no briefs', self.cmd('init', 1, 6, 6, 'false').stderr)
        self.brief('01-b', 'b.txt')
        (self.wt / 'a.txt').write_text('changed\n')
        self.assertIn('not clean', self.cmd('init', 1, 6, 6, 'false').stderr)
        git(self.wt, 'checkout', 'a.txt')
        r = subprocess.run([sys.executable, str(self.scripts / 'run.py'), 'init', '1', '6', '6', 'false'],
                           cwd=self.main, env=GIT_ENV, capture_output=True, text=True)
        self.assertIn('linked worktree', r.stderr)

    def test_brief_needs_title_scope_and_one_to_six_done_items(self):
        path = self.wt / '.atelier/briefs/01-b.md'
        path.parent.mkdir(parents=True)
        path.write_text('# T\n\n## Goal\nx\n\n## In scope\n- x\n\n## Definition of done\n')
        self.assertIn('definition of done', self.cmd('init', 1, 6, 6, 'false').stderr.lower())
        path.rename(path.with_name('Bad Name.md'))
        self.assertIn('file names', self.cmd('init', 1, 6, 6, 'false').stderr)

    def test_brief_runs_on_its_own_branch_and_ends_after_a_clean_review(self):
        self.build()
        self.assertEqual(git(self.wt, 'branch', '--show-current'), 'mm/01-b')
        self.result(CLEAN)
        self.route('MANAGE')
        self.result(FINISH, 'BRIEF_END')
        state = self.state()
        self.assertEqual((state['status'], state['dod_met'], state['round']), ('DONE', True, 1))
        self.assertEqual(state['readings']['baseline']['values'], {'exists': 0})
        self.assertEqual(state['readings']['final']['values'], {'exists': 1})
        report = (self.wt / '.atelier/mm/briefs/01-b/report.md').read_text()
        self.assertIn('Add a README', report)
        self.assertEqual(self.ok('publish'), 'NEXT')
        self.assertEqual(self.ok('start'), 'QUEUE_END')
        self.assertIn('| 01-b | DONE | met | pull_request is false |', self.ok('summary'))

    def test_accept_must_name_an_outcome_the_task_serves(self):
        self.brief('01-b', 'b.txt')
        self.ok('init', 1, 6, 6, 'false')
        self.ok('start')
        self.route('MANAGE')
        self.result(plan('b.txt'))
        self.route('WORK')
        self.commit()
        self.result(READY)
        self.route('MANAGE')
        self.result({**ACCEPT, 'outcome': 'speed'}, 'RECOVER')
        self.assertIn('must name the scorecard outcome', self.state()['reason'])

    def test_files_outside_the_allowed_paths_are_rejected_until_restored(self):
        self.brief('01-b', 'b.txt')
        self.ok('init', 1, 6, 6, 'false')
        self.ok('start')
        self.route('MANAGE')
        self.result(plan('b.txt'))
        self.route('WORK')
        self.commit()
        self.commit('a.txt', 'out of scope\n')
        self.result(READY, 'RECOVER')
        self.assertIn('a.txt', self.state()['reason'])
        self.route('WORK')
        self.commit('a.txt', 'a\n')
        self.result(READY)

    def test_abandon_keeps_commits_under_a_ref_and_drops_them(self):
        self.brief('01-b', 'b.txt')
        self.ok('init', 1, 6, 6, 'false')
        self.ok('start')
        self.route('MANAGE')
        self.result(plan('b.txt'))
        self.route('WORK')
        self.commit()
        head = git(self.wt, 'rev-parse', 'HEAD')
        self.result(READY)
        self.route('MANAGE')
        self.result({'verdict': 'ABANDON', 'reason': 'Wrong approach'})
        self.assertEqual(git(self.wt, 'rev-parse', 'HEAD'), self.base)
        self.assertEqual(git(self.wt, 'rev-parse', 'refs/managing-managers/01-b/add'), head)
        # Nothing left to review: the manager finishes without a pull request.
        self.route('MANAGE')
        self.result({**FINISH, 'judged': [{'id': 'clear', 'state': 'unmet', 'evidence': 'No file'}]}, 'BRIEF_END')
        self.assertEqual((self.state()['committed'], self.state()['dod_met']), (False, False))

    def test_checkpoint_question_is_answered_in_the_same_worker_conversation(self):
        self.brief('01-b', 'b.txt')
        self.ok('init', 1, 6, 6, 'false')
        self.ok('start')
        self.route('MANAGE')
        self.result(plan('b.txt'))
        self.route('WORK')
        session = self.ok('session')
        self.result({'status': 'CHECKPOINT', 'summary': 'Need a choice', 'question': 'Text or JSON?'})
        self.route('MANAGE')
        self.result({'verdict': 'CONTINUE', 'reason': 'Answered', 'direction': 'Plain text'})
        self.route('WORK')
        self.assertEqual(self.ok('session'), session)
        request = json.loads((self.wt / '.atelier/mm/briefs/01-b/request.json').read_text())
        self.assertEqual((request['task']['kind'], request['task']['direction']), ('continue', 'Plain text'))

    def test_task_turns_are_capped(self):
        self.brief('01-b', 'b.txt')
        self.ok('init', 1, 6, 1, 'false')
        self.ok('start')
        self.route('MANAGE')
        self.result(plan('b.txt'))
        self.route('WORK')
        self.commit()
        self.result(READY)
        self.route('MANAGE')
        self.result({'verdict': 'REVISE', 'reason': 'More', 'direction': 'Again'}, 'RECOVER')
        self.assertIn('worker turns', self.state()['reason'])

    def test_reviewers_cannot_leave_changes_behind(self):
        self.build()
        (self.wt / 'a.txt').write_text('reviewer edit\n')
        self.result(CLEAN, 'RECOVER')
        self.assertIn('only workers change files', self.state()['reason'])


class ReviewLoop(Repo):
    def test_high_findings_block_until_round_three_then_only_critical_blocks(self):
        self.build()
        for _ in range(3):
            self.review_round([finding('high'), finding('medium')])
        self.assertEqual(self.state()['round'], 3)
        # Round 4 has a high finding but no critical one: the loop closes and the high one is reported.
        self.result({**CLEAN, 'findings': [finding('high')]})
        self.route('MANAGE')
        state = self.state()
        self.assertEqual((state['phase'], state['round']), ('FINISH', 4))
        self.assertEqual([f['severity'] for f in state['remaining_high']], ['high'])
        self.result(FINISH, 'BRIEF_END')
        report = (self.wt / '.atelier/mm/briefs/01-b/report.md').read_text()
        self.assertIn('Remaining important findings', report)
        self.assertIn('medium: A medium problem', report)

    def test_critical_after_the_last_round_pauses_the_brief_and_the_queue(self):
        self.brief('02-c', 'c.txt')
        self.build()
        for _ in range(5):
            self.review_round([finding('critical')])
        self.result({**CLEAN, 'findings': [finding('critical')]})
        self.route('MANAGE')
        request = json.loads((self.wt / '.atelier/mm/briefs/01-b/request.json').read_text())
        self.assertTrue(request['blocked'])
        self.result({'blocking': 'The parser cannot be fixed without a design decision'}, 'BRIEF_END')
        self.assertEqual(self.state()['status'], 'PAUSED')
        self.assertEqual(self.ok('publish'), 'QUEUE_END')
        self.assertEqual(self.ok('start'), 'QUEUE_END')
        self.assertIn('stopped at 01-b', self.ok('summary'))
        self.assertIn('is paused', self.cmd('init', 1, 6, 6, 'false').stderr)
        # Starting it over: remove its folder and branch from the brief's base, then run again.
        shutil.rmtree(self.wt / '.atelier/mm/briefs/01-b')
        git(self.wt, 'checkout', '-q', 'mm-run')
        git(self.wt, 'branch', '-qD', 'mm/01-b')
        self.ok('init', 1, 6, 6, 'false')
        self.assertEqual(self.ok('start'), '01-b')
        self.assertEqual(git(self.wt, 'rev-parse', 'HEAD'), self.base)

    def test_triage_must_place_every_blocking_finding_and_may_dismiss(self):
        self.build()
        self.result({**CLEAN, 'findings': [finding('critical'), finding('high')]})
        self.route('MANAGE')
        self.result({'fixes': [{'task': 'add', 'findings': [0], 'direction': 'Fix'}]}, 'RECOVER')
        self.route('MANAGE')
        self.result({'fixes': [{'task': 'add', 'findings': [0], 'direction': 'Fix'}],
                     'dismissed': [{'finding': 1, 'reason': 'Out of the brief'}]})
        self.assertEqual(self.state()['dismissed'][0]['reason'], 'Out of the brief')


class PullRequests(Repo):
    def setUp(self):
        super().setUp()
        self.origin = self.tmp / 'origin.git'
        git(self.tmp, 'init', '-q', '--bare', str(self.origin))
        git(self.wt, 'remote', 'add', 'origin', str(self.origin))
        bin_dir, self.gh = self.tmp / 'bin', self.tmp / 'gh'
        bin_dir.mkdir()
        self.gh.mkdir()
        (bin_dir / 'gh').write_text(FAKE_GH)
        (bin_dir / 'gh').chmod(0o755)
        self.env = {**GIT_ENV, 'PATH': f'{bin_dir}{os.pathsep}{GIT_ENV["PATH"]}', 'FAKE_GH_DIR': str(self.gh)}

    def run_brief(self, identifier, name, met=True):
        self.assertEqual(self.ok('start'), identifier)
        self.route('MANAGE')
        self.result(plan(name))
        self.route('WORK')
        self.commit(name)
        self.result(READY)
        self.route('MANAGE')
        self.result(ACCEPT)
        self.route('REVIEW')
        self.result(CLEAN)
        self.route('MANAGE')
        state = 'met' if met else 'partial'
        self.result({**FINISH, 'judged': [{'id': 'clear', 'state': state, 'evidence': 'Read it'}]}, 'BRIEF_END')
        return self.ok('publish')

    def test_two_briefs_make_two_stacked_pull_requests_with_reports(self):
        self.brief('01-b', 'b.txt')
        self.brief('02-c', 'c.txt')
        self.ok('init', 1, 6, 6, 'true')
        self.assertEqual(self.run_brief('01-b', 'b.txt'), 'NEXT')
        self.assertEqual(self.run_brief('02-c', 'c.txt', met=False), 'NEXT')
        self.assertEqual(self.ok('start'), 'QUEUE_END')
        first, second = self.queue()['briefs']
        self.assertEqual(git(self.origin, 'rev-parse', 'mm/01-b'), first['head'])
        self.assertEqual(git(self.origin, 'rev-parse', 'mm/02-c'), second['head'])
        # The second branch stacks on the first, and its pull request targets the first branch.
        git(self.wt, 'merge-base', '--is-ancestor', first['head'], second['head'])
        creates = [json.loads(line) for line in (self.gh / 'calls.jsonl').read_text().splitlines()
                   if line.startswith('["pr", "create"')]
        self.assertEqual([c[c.index('--base') + 1] for c in creates], ['main', 'mm/01-b'])
        self.assertEqual(['--draft' in c for c in creates], [False, True])
        body = (self.wt / '.atelier/mm/briefs/02-c/pr-body.md').read_text()
        self.assertIn('# What', body)
        self.assertIn('## Brief report: Add c.txt', body)
        summary = self.ok('summary')
        self.assertIn('https://github.com/o/r/pull/', summary)
        self.assertIn('| 02-c | DONE | not met |', summary)


class NativeLoop(Repo):
    def setUp(self):
        super().setUp()
        if not shutil.which('atelier'):
            self.skipTest('atelier not installed')
        try:
            import acp  # noqa: F401
        except ImportError:
            self.skipTest('use Atelier Python to run ACP integration tests')
        self.agents = self.tmp / 'agents'
        self.agents.mkdir()

    def script(self, role, turns):
        """Each turn runs a shell command, then writes its result for the current request."""
        scripted = []
        for command, result in turns:
            code = ("import json,subprocess; from pathlib import Path; "
                    "q=json.loads(Path('.atelier/mm/queue.json').read_text()); "
                    "p=Path('.atelier/mm/briefs')/next(b['id'] for b in q['briefs'] if b['status']=='ACTIVE'); "
                    f"r=json.loads({json.dumps(result)!r}); "
                    "r['request_id']=json.loads((p/'request.json').read_text())['id']; "
                    "r.setdefault('head',subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()); "
                    "(p/'result.json').write_text(json.dumps(r))")
            scripted.append({'run': command + '\n' + shlex.quote(sys.executable) + ' -c ' + shlex.quote(code),
                             'reply': 'Finished this stage.'})
        (self.agents / f'{role}.json').write_text(json.dumps({'turns': scripted}))

    def flow(self):
        agent = [sys.executable, str(ROOT / 'tests/fake_agent.py'), str(self.agents)]
        return subprocess.run(['atelier', 'run', 'managing_managers', '--hide-steps', '-i', 'pull_request=false',
                               '--agent', 'queue.brief.manage=claude-code'],
                              cwd=self.wt, env={**GIT_ENV, 'ATELIER_CLAUDE_LAUNCH_CMD': json.dumps(agent + ['claude']),
                                                'ATELIER_CODEX_LAUNCH_CMD': json.dumps(agent + ['codex'])},
                              capture_output=True, text=True, timeout=180)

    def test_two_briefs_with_a_check_in_and_a_fix_round(self):
        self.brief('01-b', 'b.txt')
        self.brief('02-c', 'c.txt')
        self.script('manager', [
            ('true', plan('b.txt')),
            ('true', {'verdict': 'CONTINUE', 'reason': 'Answered', 'direction': 'Plain text'}),
            ('true', ACCEPT),
            ('true', {'fixes': [{'task': 'add', 'findings': [0], 'direction': 'Fix it'}]}),
            ('true', FINISH),
            ('true', plan('c.txt')),
            ('true', ACCEPT),
            ('true', FINISH)])
        self.script('worker', [
            ('true', {'status': 'CHECKPOINT', 'summary': 'Need a choice', 'question': 'Text or JSON?'}),
            ('printf b > b.txt\ngit add b.txt\ngit commit -qm b', READY),
            ('printf fixed > b.txt\ngit commit -qam fix', READY),
            ('printf c > c.txt\ngit add c.txt\ngit commit -qm c', READY)])
        self.script('reviewer', [
            ('true', {**CLEAN, 'findings': [finding('high')]}),
            ('true', CLEAN),
            ('true', CLEAN)])
        r = self.flow()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([(b['status'], b['dod_met']) for b in self.queue()['briefs']],
                         [('DONE', True), ('DONE', True)])
        self.assertEqual(self.state()['round'], 2)
        self.assertEqual(git(self.wt, 'branch', '--show-current'), 'mm/02-c')
        git(self.wt, 'merge-base', '--is-ancestor', 'mm/01-b', 'mm/02-c')
        sessions = lambda role: [(self.agents / f'{role}.session.{n}').read_text()
                                 for n in range(1, int((self.agents / f'{role}.calls').read_text()) + 1)]
        # One manager conversation per brief: five turns for the first, three for the second.
        manager = sessions('manager')
        self.assertEqual((len(set(manager[:5])), len(set(manager[5:]))), (1, 1))
        self.assertNotEqual(manager[0], manager[5])
        # The check-in, the build and the fix all continued one worker conversation.
        self.assertEqual(len(set(sessions('worker')[:3])), 1)
        # Every review round started fresh.
        self.assertEqual(len(set(sessions('reviewer'))), 3)
        self.assertIn('| 02-c | DONE | met |', (self.wt / '.atelier/mm/summary.md').read_text())


if __name__ == '__main__':
    unittest.main()
