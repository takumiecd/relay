"""Real Console/owner-thread integration plus ambiguous HTTP outcome checks."""

from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

import pytest

from relay.client import RelayClient, RelayClientError

ROOT = Path(__file__).resolve().parents[1]


def command(url, *args, **options):
    env = dict(os.environ)
    env.pop('RELAY_TOKEN', None)
    return subprocess.run([sys.executable, '-m', 'relay', '--url', url, '--json', *args],
                          cwd=ROOT, env=env, text=True, capture_output=True, timeout=15, **options)


def wait_for(client, predicate):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        state = client.status()
        if predicate(state):
            return state
        time.sleep(.02)
    raise AssertionError(state)


@pytest.fixture
def console(tmp_path, request):
    from capx.extensions.hybrid.control import AuthorityLost
    from capx.extensions.hybrid.runner import HybridRunner
    from capx.extensions.hybrid.server import Console, TestGenerator, TestVla
    from capx.extensions.vla.env import BackendEnv
    from capx.extensions.vla.recording import TrajectoryRecorder
    from relay.backends.kinematic import KinematicBackend
    ready = queue.Queue()
    stop = threading.Event()
    failures = []
    limited = getattr(request, 'param', False)

    def owner():
        env = runner = server = None
        try:
            env = BackendEnv(KinematicBackend(checkpoints=not limited, max_steps=10000))
            env.attach_recorder(TrajectoryRecorder(tmp_path/'trajectory', {'test_only': True}))
            predictor = None if limited else TestVla(action_contract=env.backend.action_contract,
                native_action=env.teleop_action([.1, 0, 0, 0, 0, 0], -1))
            runner = HybridRunner(env, tmp_path, test_operator=True, predictor=predictor,
                                  generator=None if limited else TestGenerator())
            server = Console(runner, port=0)
            ready.put(f'http://127.0.0.1:{server.port}')
            while not stop.is_set():
                try:
                    runner.tick()
                except AuthorityLost:
                    runner.service()
                except Exception as error:
                    runner.control.switch('STOPPED', reason='execution_error')
                    runner.error = str(error)
                time.sleep(.005)
        except BaseException as error:
            failures.append(error)
            ready.put(error)
        finally:
            if runner:
                runner.close()
            if server:
                server.close()
            if env:
                env.close()

    thread = threading.Thread(target=owner, daemon=True)
    thread.start()
    url = ready.get(timeout=10)
    if isinstance(url, BaseException):
        raise url
    try:
        yield url, tmp_path
    finally:
        stop.set()
        thread.join(timeout=10)
        assert not thread.is_alive() and not failures, failures


def browser_motion(client, key):
    state = client.control('human')['state']
    before = state['applied_steps']
    body = dict(client_id=client.client_id, sequence=0, epoch=state['epoch'],
                keys=[key], speed=.5, deadman=True)
    assert client._http('/api/input', body)['accepted']
    wait_for(client, lambda s: s['applied_steps'] >= before+2)
    client._http('/api/input', dict(body, sequence=1, keys=[], deadman=False))
    client.stop()
    return wait_for(client, lambda s: s['mode'] == 'STOPPED')


def test_module_entrypoint_imports_no_robot_or_ml_dependencies():
    result = subprocess.run([sys.executable, '-S', '-c',
        'import relay.cli, sys; assert not {"capx","rclpy","numpy","torch","genesis","mujoco"}.intersection(sys.modules); '
        'raise SystemExit(relay.cli.main(["--help"]))'], cwd=ROOT, text=True, capture_output=True)
    assert result.returncode == 0 and 'history' in result.stdout


def test_cli_and_browser_share_authority_restore_fork_and_adopt(console):
    url, root = console
    browser = RelayClient(url)
    browser_motion(browser, 'KeyW')
    state = browser.status()
    original_count = state['timeline']['head']
    original = root/'trajectory/steps.jsonl'
    original_hash = hashlib.sha256(original.read_bytes()).hexdigest()

    status = command(url, 'status')
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)['applied_steps'] == original_count
    listed = command(url, 'history', 'list')
    assert json.loads(listed.stdout)['branch'] == '0001'
    switched = command(url, 'control', 'human')
    switched = json.loads(switched.stdout)
    assert switched['completed'] and switched['state']['mode'] == 'HUMAN'
    assert switched['state']['client_id'] != browser.client_id
    assert not browser._http('/api/input', dict(client_id=browser.client_id, sequence=2,
        epoch=state['epoch'], keys=['KeyW'], speed=.5, deadman=True))['accepted']
    stopped = command(url, 'stop')
    assert stopped.returncode == 0 and json.loads(stopped.stdout)['accepted']
    sought = command(url, 'history', 'seek', '--branch', '0001', '--step', '1')
    assert sought.returncode == 0, sought.stdout + sought.stderr
    restored = json.loads(sought.stdout)
    assert restored['completed'] and restored['state']['timeline']['cursor'] == 1
    first = json.loads(original.read_text().splitlines()[0])
    assert restored['state']['eef_position_m'] == first['eef_after_m']
    browser_motion(browser, 'KeyA')
    current = browser.status()['timeline']
    assert current['branch'] == '0002'
    assert hashlib.sha256(original.read_bytes()).hexdigest() == original_hash
    adopted = command(url, 'history', 'adopt', '--branch', '0002', '--step', str(current['head']))
    assert adopted.returncode == 0, adopted.stdout + adopted.stderr
    receipt = json.loads(adopted.stdout)
    selected = receipt['state']['timeline']['adopted']
    assert receipt['completed'] and selected['branch'] == '0002'
    assert selected['selected_by'] == receipt['client_id'] and selected['training_admitted'] is False
    assert len((root/selected['route']).read_text().splitlines()) == current['head']
    assert browser.status()['timeline']['adopted']['id'] == selected['id']


def test_mode_switches_use_ready_providers_and_preserve_instruction(console):
    url, _ = console
    client = RelayClient(url)
    switched = command(url, 'control', 'vla', '--instruction', 'test VLA instruction')
    assert switched.returncode == 0, switched.stdout + switched.stderr
    state = wait_for(client, lambda s: s['applied_steps'] > 0)
    assert state['vla_instruction'] == 'test VLA instruction'
    client.stop()
    wait_for(client, lambda s: s['mode'] == 'STOPPED')
    switched = command(url, 'control', 'llm', '--instruction', 'test LLM instruction')
    assert switched.returncode == 0, switched.stdout + switched.stderr
    state = wait_for(client, lambda s: s['generation'] and s['generation']['stage'] == 'program_completed')
    assert state['generation']['instruction'] == 'test LLM instruction'
    assert state['generation']['generator'] == 'test_generator'


@pytest.mark.parametrize('console', [True], indirect=True)
def test_unsupported_history_and_unready_providers_make_no_changes(console):
    url, _ = console
    client = RelayClient(url)
    epoch = client.status()['epoch']
    for args in [('history','seek','--step','0'), ('history','adopt','--step','1'),
                 ('control','vla'), ('control','llm','--instruction','close')]:
        result = command(url, *args)
        assert result.returncode == 1, result.stdout + result.stderr
    missing = command(url, 'control', 'llm')
    assert missing.returncode == 1 and '--instruction' in missing.stdout
    state = client.status()
    assert state['epoch'] == epoch and state['applied_steps'] == 0


def snapshot(epoch=1, session='test-session'):
    return dict(mode='STOPPED', epoch=epoch, context=dict(epoch=epoch,session_id=session),
                vla_ready=True, llm_ready=True, applied_steps=5, error=None,
                timeline=dict(branch='0001', cursor=5, head=5, adopted=None,
                              branches=[dict(id='0001',head=5,parent=None,parent_step=None)]))


@contextmanager
def endpoint(handler):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self): self.respond()
        def do_POST(self): self.respond()
        def respond(self):
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length',0)))) if self.command=='POST' else None
            calls.append((self.command, self.path, body))
            if self.path == '/':
                code, value = 200, b'<meta name="capx-token" content="test-only-token">'
            elif self.headers.get('X-CapX-Token') != 'test-only-token':
                code, value = 403, dict(error='forbidden')
            else:
                result = handler(self.path, body)
                if result is None:
                    self.close_connection = True
                    return
                code, value = result
            raw = value if isinstance(value,bytes) else json.dumps(value).encode()
            self.send_response(code)
            self.send_header('Content-Length',str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', calls
    finally:
        server.shutdown(); server.server_close(); thread.join()


def test_competing_epoch_refusal_is_not_retried():
    with endpoint(lambda path, body: (200, snapshot()) if body is None else
                  (200, dict(accepted=False,epoch=2,mode='STOPPED'))) as (url,calls):
        result = command(url, 'control', 'human')
    assert result.returncode == 3 and json.loads(result.stdout)['receipt']['accepted'] is False
    assert sum(method=='POST' for method,_,_ in calls) == 1


def test_accepted_seek_times_out_as_pending_not_success_or_repeated_mutation():
    with endpoint(lambda path, body: (200, snapshot()) if body is None else
                  (200, dict(accepted=True,epoch=2,seeking=True))) as (url,calls):
        result = command(url, '--wait-timeout', '.12', 'history', 'seek', '--step', '2')
    data = json.loads(result.stdout)
    assert result.returncode == 4 and data['error']['code'] == 'pending'
    assert data['receipt']['accepted'] is True and data['receipt']['completed'] is False
    assert sum(method=='POST' for method,_,_ in calls) == 1


@pytest.mark.parametrize('outcome', ['other_adoption', 'new_session', 'execution_error', 'read_failure'])
def test_post_acceptance_conflicts_and_failures_keep_receipt(outcome):
    posted = False
    def handle(path, body):
        nonlocal posted
        if body is not None:
            posted = True
            return 200,dict(accepted=True,epoch=2,adopting=True)
        if not posted:
            return 200,snapshot()
        state = snapshot(2, session='changed' if outcome=='new_session' else 'test-session')
        if outcome=='read_failure':
            return 500,dict(error='read unavailable')
        if outcome=='execution_error':
            state['error']='failed to write selection'
        state['timeline']['adopted']=dict(id='someone-else',selected_by='other',branch='0001',end_step=2)
        return 200,state
    with endpoint(handle) as (url,calls):
        result = command(url, 'history', 'adopt', '--step', '2')
    data = json.loads(result.stdout)
    assert result.returncode == (1 if outcome in {'execution_error','read_failure'} else 3)
    assert data['receipt']['accepted'] and not data['receipt']['completed']
    assert sum(method=='POST' for method,_,_ in calls) == 1


def test_stop_posts_before_state_get_and_preserves_ack_when_snapshot_is_unavailable():
    with endpoint(lambda path, body: (500,dict(error='snapshot unavailable')) if body is None else
                  (200,dict(accepted=True,epoch=99,mode='STOPPED'))) as (url,calls):
        result = command(url, 'stop')
    assert result.returncode == 0 and json.loads(result.stdout)['response']['mode'] == 'STOPPED'
    assert calls[1][0] == 'POST' and calls[1][2]['expected_epoch'] == 0
    assert json.loads(result.stdout)['state_confirmation'] == 'unavailable'


def test_lost_post_response_is_not_retried():
    with endpoint(lambda path,body: None) as (url,calls):
        result = command(url, 'stop')
    assert result.returncode == 1 and '自動再送' in result.stdout
    assert sum(method=='POST' for method,_,_ in calls) == 1


def test_transient_mixed_epoch_snapshot_is_reread_before_mutation():
    reads = 0
    def handle(path, body):
        nonlocal reads
        if body is not None:
            assert body['expected_epoch'] == 2
            return 200,dict(accepted=True,epoch=3,mode='HUMAN')
        reads += 1
        state = snapshot(2 if reads < 3 else 3)
        if reads == 1:
            state['epoch'] = 1
        return 200,state
    with endpoint(handle) as (url,calls):
        result = command(url, 'control', 'human')
    assert result.returncode == 0, result.stdout + result.stderr
    assert reads >= 3 and sum(method=='POST' for method,_,_ in calls) == 1


def test_stop_confirms_published_epoch_without_reposting():
    reads = 0
    def handle(path, body):
        nonlocal reads
        if body is not None:
            return 200,dict(accepted=True,epoch=2,mode='STOPPED')
        reads += 1
        return 200,snapshot(1 if reads == 1 else 2)
    with endpoint(handle) as (url,calls):
        result = command(url, 'stop')
    receipt = json.loads(result.stdout)
    assert result.returncode == 0 and receipt['state_confirmation'] == 'confirmed'
    assert receipt['state']['epoch'] == 2 and sum(method=='POST' for method,_,_ in calls) == 1


def test_new_start_after_stop_is_reported_with_the_accepted_stop_receipt():
    def handle(path, body):
        if body is not None:
            return 200,dict(accepted=True,epoch=2,mode='STOPPED')
        state = snapshot(3)
        state['mode'] = 'VLA'
        return 200,state
    with endpoint(handle) as (url,calls):
        result = command(url, 'stop')
    data = json.loads(result.stdout)
    assert result.returncode == 3 and data['receipt']['accepted']
    assert data['receipt']['state_confirmation'] == 'superseded'
    assert data['receipt']['state']['mode'] == 'VLA'
    assert sum(method=='POST' for method,_,_ in calls) == 1


@pytest.mark.parametrize('url', ['https://example.com:443','http://127.0.0.1:8876/?token=secret',
    'http://user:secret@localhost:8876', 'http://localhost:0', 'http://localhost:8876/other'])
def test_connection_configuration_is_rejected_before_network(url):
    with pytest.raises(RelayClientError):
        RelayClient(url)
