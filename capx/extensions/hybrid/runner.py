"""Owner-thread scheduler. Inference and generated Python never own the backend."""

from __future__ import annotations

from collections import deque
import json
import inspect
from pathlib import Path
from dataclasses import asdict
import threading
import time
import uuid

from capx.extensions.vla.api import VlaApi
from capx.extensions.vla.actions import PolicyActions
from capx.extensions.vla.recording import TrajectoryRecorder, export_views, write_json
from .code_worker import CodeProcess, TOOLS
from .control import AuthorityLost, ControlSession
from .llm import extract_code
from .history import Timeline


class Job:
    def __init__(self, function, args, lease, context):
        self.lease, self.context = lease, context
        self.done = threading.Event()
        self.result = self.error = None

        def run():
            try:
                self.result = function(*args)
            except Exception as error:
                self.error = error
            finally:
                self.done.set()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()


class ResponsivePredictor:
    """For run_vla called by code: wait cooperatively while keeping the UI live."""

    def __init__(self, runner):
        self.runner = runner
        self.is_test = getattr(runner.predictor, "is_test", False)

    @property
    def ready(self):
        return getattr(self.runner.predictor, 'ready', None)

    def predict(self, observation, instruction, context):
        runner = self.runner
        if runner.predictor is None:
            raise ValueError("Connect a VLA worker before delegating run_vla")
        source = "TEST_VLA" if self.is_test else "VLA"
        lease = runner.control.begin(source)
        while any(j and not j.done.is_set() for j in (runner.vla_job, runner.delegated)):
            runner.service()
            if not runner.control.valid(lease):
                raise AuthorityLost("VLA delegation interrupted")
            time.sleep(0.01)
        job = Job(runner.predictor.predict, (observation, instruction, context), lease, context)
        runner.delegated = job
        try:
            while not job.done.wait(0.01):
                runner.service()
                if not runner.control.valid(lease):
                    raise AuthorityLost("VLA delegation interrupted")
            if not runner.control.valid(lease):
                raise AuthorityLost("Late VLA delegation discarded")
            if job.error:
                raise job.error
            return job.result
        finally:
            if not job.done.is_set():
                runner._cancel_job(job)
            # Keep cancelled requests until their transport finishes; no parallel mailbox request.
            if job.done.is_set():
                runner.delegated = None


class HybridRunner:
    def __init__(
        self,
        env,
        output,
        *,
        predictor=None,
        generator=None,
        policy_actions=None,
        test_operator=False,
        code_timeout=30,
        code_budget=100,
        realtime=True,
    ):
        self.env = env
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.program_root = (
            env.recorder.root if env.recorder is not None else self.output
        ) / "programs"
        self.program_root.mkdir(exist_ok=True)
        self.predictor, self.generator = predictor, generator
        self.control = ControlSession(env, test_operator=test_operator)
        self.policy_actions = policy_actions or PolicyActions(env)
        self.api = VlaApi(env, ResponsivePredictor(self), policy_actions=self.policy_actions)
        self.code_timeout, self.code_budget = code_timeout, code_budget
        self.realtime = realtime
        self.next_tick = time.monotonic()
        self.on_service = None
        self.vla_job = self.llm_job = self.delegated = None
        self.program = None
        self.chunk = deque()
        self.chunk_lease = self.chunk_context = None
        self.current_epoch = self.env.epoch
        self.policy_metadata = None
        self.error = None
        self.message = "停止中"
        self.closed = False
        self.pending_code = None
        self.llm_request = None
        self.last_generation = None
        self.vla_instruction = env.instruction
        self.pending_reset = False
        self.pending_seek = None
        self.seeking = False
        self.pending_adoption = None
        self.adopting = False
        self.timeline = self._make_timeline(self.output)
        self.env.before_action = self._before_action
        self.trial_index = 1
        self.trial_root = self.output
        self.pending_lock = threading.Lock()
        self.env.after_step = self._after_step
        self.service()

    def _make_timeline(self, root):
        capabilities = getattr(self.env, 'capabilities', None)
        return Timeline(self.env, root) if (self.env.recorder is not None
                                           and capabilities is not None
                                           and capabilities.checkpoints) else None

    @property
    def api_docs(self):
        # Describe exactly the functions available in the code process.
        return "\n\n".join(
            f"{name}{inspect.signature(getattr(self.api, name))}\n"
            f"{inspect.getdoc(getattr(self.api, name)) or ''}"
            for name in sorted(self.available_tools)
        )

    @property
    def available_tools(self):
        capabilities = getattr(self.env, 'capabilities', None)
        tools = set(TOOLS)
        if capabilities is not None and not capabilities.cartesian_control:
            tools.discard('move_to_pose')
        if self.predictor is None or not bool(getattr(self.predictor, 'ready', True)):
            tools.discard('run_vla')
        return tools

    def request_mode(self, mode, *, client_id, sequence, expected_epoch, instruction=None):
        if instruction is not None and (mode not in {'LLM', 'VLA'} or not isinstance(instruction, str)
                or not instruction.strip() or len(instruction) > 2000):
            raise ValueError('Automatic control instruction must be a non-empty string of at most 2000 characters')
        if mode == 'LLM' and self.generator is None:
            raise ValueError('Connect an LLM endpoint before requesting code')
        with self.pending_lock, self.control.lock:
            if (self.pending_reset or self.seeking or self.adopting) and mode != "STOPPED":
                raise ValueError("履歴を処理中です")
            result = self.control.command(
                mode, client_id=client_id, sequence=sequence, expected_epoch=expected_epoch
            )
            if result['accepted'] and mode == 'LLM':
                self.llm_request = dict(epoch=self.env.epoch,
                    instruction=(instruction or self.env.instruction).strip(), client_id=client_id)
                self.control._event('code_requested', instruction=self.llm_request['instruction'],
                    client_id=client_id, provider=getattr(self.generator, 'metadata', {}))
            if result['accepted'] and mode == 'VLA':
                self.vla_instruction = (instruction or self.env.instruction).strip()
                self.control._event('vla_requested', instruction=self.vla_instruction, client_id=client_id)
            return result

    def request_trial(self, *, client_id, sequence, expected_epoch):
        if getattr(self.env, 'capabilities', None) is not None and not self.env.capabilities.reset:
            raise ValueError('This backend does not support resetting trials')
        with self.pending_lock:
            if self.adopting:
                raise ValueError("採用する経路を保存中です")
            with self.control.lock:
                if expected_epoch != self.env.epoch:
                    return dict(self.control.status(), accepted=False, resetting=self.pending_reset)
                result = self.control.command(
                    "STOPPED", client_id=client_id, sequence=sequence, expected_epoch=expected_epoch
                )
            if result["accepted"]:
                self.pending_reset = True
                self.pending_seek = None
                self.seeking = False
                self.pending_code = None
            return dict(result, resetting=self.pending_reset)

    def request_seek(self, branch, step, *, client_id, sequence, expected_epoch):
        with self.pending_lock, self.control.lock:
            if self.timeline is None or self.pending_reset or self.seeking or self.adopting:
                raise ValueError("履歴を準備中です")
            self.timeline.validate_target(branch, step)
            if expected_epoch != self.env.epoch:
                return dict(self.control.status(), accepted=False)
            result = self.control.command("STOPPED", client_id=client_id, sequence=sequence,
                                          expected_epoch=expected_epoch)
            if result["accepted"]:
                self.pending_code = None
                self.pending_seek = (branch, step)
                self.seeking = True
            return dict(result, seeking=self.seeking)

    def request_adoption(self, branch, end_step, *, client_id, sequence, expected_epoch):
        with self.pending_lock, self.control.lock:
            if self.timeline is None or self.pending_reset or self.seeking or self.adopting:
                raise ValueError("履歴を処理中です")
            self.timeline.validate_target(branch, end_step)
            if end_step == 0:
                raise ValueError("操作を含む経路を選んでください")
            if expected_epoch != self.env.epoch:
                return dict(self.control.status(), accepted=False)
            result = self.control.command("STOPPED", client_id=client_id, sequence=sequence,
                                          expected_epoch=expected_epoch)
            if result["accepted"]:
                self.pending_code = None
                self.pending_adoption = (branch, end_step, client_id)
                self.adopting = True
            return dict(result, adopting=self.adopting)

    def _adopt(self):
        with self.pending_lock:
            target, self.pending_adoption = self.pending_adoption, None
        if target is None:
            return
        try:
            self._finish_program("route_adoption")
            result = self.timeline.adopt(target[0], target[1], client_id=target[2])
            with self.control.lock:
                self.control._event("route_adopted", selection=result)
            self.message = f"経路 {target[0]} のstep {target[1]}までを採用しました"
        finally:
            with self.pending_lock:
                self.adopting = False
        self.service()

    def _seek(self):
        with self.pending_lock:
            target, self.pending_seek = self.pending_seek, None
        if target is None:
            return
        try:
            self._finish_program("timeline_seek")
            self.timeline.seek(*target)
            # Reset deadman/gripper targets to the selected physical state.
            self.control.switch("STOPPED", reason="timeline_seek")
            self.program_root = self.timeline.current["recorder"].root / "programs"
            self.program_root.mkdir(exist_ok=True)
            self.next_tick = time.monotonic()
            self.service()
            self.message = f"step {target[1]} に移動しました。操作モードを選ぶと再開できます"
        finally:
            with self.pending_lock:
                self.seeking = False

    def _before_action(self):
        if self.timeline:
            self.timeline.before_action()
            self.program_root = self.env.recorder.root / "programs"
            self.program_root.mkdir(exist_ok=True)

    def save_trial(self, reason):
        if self.timeline:
            self.timeline.save_all()
        elif self.env.recorder is not None and self.env.recorder.count:
            export_views(self.env.recorder.root)
        write_json(self.trial_root / "actions.json", self.env.trace)
        write_json(self.trial_root / "trial-summary.json", {
            "trial_index": self.trial_index, "steps": self.env.applied_steps,
            "task_completed": self.env.task_completed(), "reason": reason,
            "context": self.env.context(), "timeline": self.timeline.status() if self.timeline else None,
            "training_admitted": False,
        })

    def _new_trial(self):
        try:
            self._prepare_new_trial()
        finally:
            with self.pending_lock:
                self.pending_reset = False
        self.service()
        self.message = "新しい試行を開始できます"

    def _prepare_new_trial(self):
        self._finish_program("new_trial")
        self.save_trial("new_trial")
        metadata = dict(self.timeline.current["recorder"].metadata) if self.timeline else (dict(self.env.recorder.metadata) if self.env.recorder is not None else {})
        for key in ("parent_trajectory", "parent_step", "branch_id"):
            metadata.pop(key, None)
        self.trial_index += 1
        self.trial_root = self.output / "trials" / f"{self.trial_index:04d}"
        self.trial_root.mkdir(parents=True, exist_ok=False)
        self.env.reset(seed=0)
        self.control.switch("STOPPED", reason="new_trial")
        self.env.attach_recorder(TrajectoryRecorder(self.trial_root / "trajectory", metadata))
        self.timeline = self._make_timeline(self.trial_root)
        self.program_root = self.env.recorder.root / "programs"
        self.program_root.mkdir()
        self.policy_metadata = None
        self.vla_instruction = self.env.instruction
        self.error = None
        self.next_tick = time.monotonic()

    def submit_code(self, code, client_id, sequence=None, expected_epoch=None):
        code = extract_code(code)
        with self.pending_lock:
            if self.pending_reset or self.seeking or self.adopting:
                raise ValueError("履歴を処理中です")
            status = (
                self.control.switch("LLM", reason="operator_code")
                if sequence is None
                else self.control.command(
                    "LLM", client_id=client_id, sequence=sequence, expected_epoch=expected_epoch
                )
            )
            if status.get("accepted") is False:
                return status
            self.pending_code = (
                code,
                self.env.context(),
                {"generator": "operator_code", "client_id": client_id},
            )
            return status

    def _cancel_job(self, job):
        cancel = getattr(self.predictor, "cancel_pending", None)
        if cancel:
            cancel(job.context)

    def service(self):
        """Drain authority events and publish snapshots; creation thread only."""
        self.env._owner()
        flush = getattr(self.env, 'flush_control', None)
        if flush is not None:
            flush()
        if self.control.mode == 'VLA' and self.predictor is not None and not bool(getattr(self.predictor, 'ready', True)):
            self.control.switch('STOPPED', reason='vla_disconnected')
        metadata = getattr(self.predictor, "ready", None)
        if isinstance(metadata, dict) and metadata != self.policy_metadata:
            if self.policy_metadata is not None and (self.control.mode == 'VLA' or self.delegated is not None):
                self.control.switch('STOPPED', reason='vla_policy_changed')
            self.policy_metadata = dict(metadata)
            with self.control.lock:
                self.control._event("policy_ready", metadata=self.policy_metadata)
            root = self.env.recorder.root if self.env.recorder is not None else self.output
            write_json(root / "policy.json", self.policy_metadata)
        events = self.control.drain_events()
        if events:
            with (self.output / "events.jsonl").open("a") as stream:
                for event in events:
                    stream.write(json.dumps(event, ensure_ascii=False) + "\n")
                    recorder = self.env.recorder
                    if recorder is not None:
                        recorder.record_event(self.env, event)
        if self.current_epoch != self.env.epoch:
            self.current_epoch = self.env.epoch
            if self.control.last_reason not in {
                "vla_error",
                "llm_error",
                "execution_error",
                "program_error",
            }:
                self.error = None
            self.chunk.clear()
            self.chunk_lease = self.chunk_context = None
            self.message = {
                "STOPPED": "停止中",
                "HUMAN": "Spaceを押しながら操作",
                "VLA": "VLAの指示を待っています",
                "LLM": "コードを準備中",
            }[self.control.mode]
        for name in ("vla_job", "delegated"):
            job = getattr(self, name)
            if job and not self.control.valid(job.lease):
                self._cancel_job(job)
                if job.done.is_set():
                    setattr(self, name, None)
        if self.on_service:
            self.on_service()

    def _after_step(self):
        if self.timeline:
            self.timeline.record_step()
        self.service()
        if self.program:
            self.program.steps += 1
            if self.program.steps >= self.code_budget:
                self.control.switch("STOPPED", reason="code_action_budget")
            if not self.control.valid(self.program.lease):
                raise AuthorityLost("Program control changed")
        if self.realtime:
            self.next_tick += 1 / self.env.control_freq
            remaining = self.next_tick - time.monotonic()
            if remaining > 0:
                time.sleep(min(remaining, 1 / self.env.control_freq))
            elif remaining < -1 / self.env.control_freq:
                self.next_tick = time.monotonic()

    def _start_program(self, code, context, metadata, generation=None):
        # Fork before program metadata is written, even if its first tool is read-only.
        with self.control.lock:
            self._before_action()
        lease = self.control.start_program(context, metadata)
        directory = self.program_root / lease.program_id
        directory.mkdir()
        (directory / "code.py").write_text(code + "\n")
        write_json(
            directory / "metadata.json",
            dict(
                metadata,
                context=context,
                start_observation_id=context["observation_id"],
                observation=self.env.recorder._observation(self.env)
                if self.env.recorder is not None
                else None,
            ),
        )
        if generation:
            write_json(directory / "generation.json", generation)
        self.last_generation = dict(program_id=lease.program_id, code=code,
            instruction=metadata.get('instruction', self.env.instruction),
            generator=metadata.get('generator'), model=metadata.get('model'), stage='running')
        self.program = CodeProcess(code, metadata.get('instruction', self.env.instruction), lease, timeout=self.code_timeout)
        self.message = "コードを実行中"

    def _finish_program(self, reason, result=None):
        program = self.program
        if program is None:
            return
        write_json(
            self.program_root / program.lease.program_id / "result.json",
            dict(
                reason=reason,
                result=result,
                applied_actions=program.steps,
                end_context=self.env.context(),
            ),
        )
        if reason == "program_error":
            # Keep the final exception message visible; the full traceback is in result.json.
            self.error = str(result.get("error") or result.get("info", {}).get("stderr", reason)).strip()[-500:]
        program.close()
        if self.last_generation and self.last_generation.get('program_id') == program.lease.program_id:
            self.last_generation.update(stage=reason, applied_actions=program.steps)
        self.program = None
        if self.control.valid(program.lease):
            self.control.switch("STOPPED", reason=reason)
        self.message = {
            "program_completed": "コード完了",
            "interrupted": "操作を引き継ぎました",
        }.get(reason, reason)

    def _poll_program(self):
        program = self.program
        if program is None:
            return
        if not self.control.valid(program.lease):
            return self._finish_program("interrupted")
        if time.monotonic() - program.started > program.timeout:
            return self._finish_program("program_timeout")
        message = program.receive()
        if message is None:
            return
        if message["type"] in {"done", "failed"}:
            ok = message["type"] == "done" and message["info"]["sandbox_rc"] == 0
            return self._finish_program("program_completed" if ok else "program_error", message)
        if message["type"] != "tool":
            return self._finish_program("invalid_program_message")
        try:
            program.tool_calls += 1
            name = message["name"]
            if name == "__observe__":
                result = self.env.get_observation()
            elif name == "__reward__":
                result = self.env.compute_reward()
            elif name in self.available_tools:
                result = getattr(self.api, name)(*message["args"], **message["kwargs"])
            else:
                raise ValueError("Unknown code tool")
            program.reply(ok=True, result=result)
        except Exception as error:
            program.reply(ok=False, error=str(error))
            if not self.control.valid(program.lease):
                self._finish_program("interrupted")

    def tick(self):
        self.env._owner()
        self.service()
        if self.env.ended and self.control.mode != "STOPPED":
            self.control.switch("STOPPED", reason="episode_ended")
        self._poll_program()
        if self.pending_reset:
            self._new_trial()
            return
        if self.seeking:
            self._seek()
            return
        if self.adopting:
            self._adopt()
            return
        if self.llm_job and self.llm_job.done.is_set():
            job, self.llm_job = self.llm_job, None
            write_json(job.record_path, dict(job.request_record,
                status='discarded' if not self.control.valid(job.lease) else 'error' if job.error else 'received',
                error=str(job.error)[:500] if job.error else None, generation=job.result))
            if self.control.valid(job.lease):
                if job.error:
                    self.error = self.message = str(job.error)[:300]
                    self.control.switch("STOPPED", reason="llm_error")
                else:
                    try:
                        data = job.result
                        metadata = dict(data['metadata'], **job.request_metadata)
                        self._start_program(extract_code(data['code']), job.context, metadata, data)
                    except (ValueError, SyntaxError, KeyError, TypeError) as error:
                        self.error = self.message = str(error)[:300]
                        self.control.switch('STOPPED', reason='llm_error')
        with self.pending_lock:
            pending, self.pending_code = self.pending_code, None
        if pending and self.control.mode == "LLM" and pending[1] == self.env.context():
            self._start_program(*pending)
        if self.program is not None:
            return
        mode = self.control.mode
        if mode == "HUMAN":
            item = self.control.human_action()
            if item:
                command, lease, provenance = item
                action = self.env.teleop_action(command[:6], command[-1])
                self.env.apply_native(
                    action,
                    context=self.env.context(),
                    source=lease.source,
                    lease=lease,
                    provenance=provenance,
                )
                if self.control.valid(lease):
                    self.message = "操作を受付中"
            else:
                hold = getattr(self.env, 'hold', None)
                if hold is not None:
                    hold()
                self.message = "Spaceを押しながら操作"
        elif mode == "VLA":
            source = "TEST_VLA" if getattr(self.predictor, "is_test", False) else "VLA"
            if self.vla_job and self.vla_job.done.is_set():
                job, self.vla_job = self.vla_job, None
                if self.control.valid(job.lease) and self.env.context() == job.context:
                    if job.error:
                        self.control.switch("STOPPED", reason="vla_error")
                        self.error = self.message = str(job.error)[:300]
                    else:
                        reply = job.result
                        if (
                            reply.get("context") != job.context
                            or reply.get("action_contract") != self.policy_actions.action_contract
                        ):
                            raise ValueError("VLA reply context or contract mismatch")
                        actions = self.policy_actions.validate_chunk(reply["native_actions"], reply['action_contract'])
                        self.chunk = deque(
                            (
                                a.tolist(),
                                dict(
                                    request_id=reply.get("request_id"),
                                    chunk_index=i,
                                    chunk_size=len(actions),
                                    instruction=job.instruction,
                                    **self.policy_actions.provenance(a.tolist(), reply.get('diagnostics'), self.policy_metadata),
                                ),
                            )
                            for i, a in enumerate(actions)
                        )
                        self.chunk_lease, self.chunk_context = job.lease, job.context
            if self.chunk:
                if (
                    self.control.valid(self.chunk_lease)
                    and self.chunk_context == self.env.context()
                ):
                    action, provenance = self.chunk.popleft()
                    self.env.apply_native(
                        self.policy_actions.to_native(action),
                        context=self.chunk_context,
                        source=source,
                        lease=self.chunk_lease,
                        provenance=provenance,
                    )
                    if self.control.valid(self.chunk_lease):
                        self.chunk_context = self.env.context()
                        self.message = "VLAが操作中"
            elif (
                self.vla_job is None
                and self.predictor is not None
                and bool(getattr(self.predictor, "ready", True))
                and self.control.mode == "VLA"
            ):
                context = self.env.context()
                self.vla_job = Job(
                    self.predictor.predict,
                    (self.env.policy_observation(), self.vla_instruction, context),
                    self.control.begin(source),
                    context,
                )
                self.vla_job.instruction = self.vla_instruction
        elif mode == "LLM" and self.llm_job is None and self.generator is not None:
            # Generation itself owns no code run; capture an epoch lease for late-result rejection.
            from .control import Lease

            lease = Lease(self.env.epoch, "LLM", "CODE", None)
            request = self.llm_request if self.llm_request and self.llm_request['epoch'] == self.env.epoch else {}
            instruction = request.get('instruction', self.env.instruction)
            self.llm_job = Job(
                self.generator.generate,
                (self.env.code_observation(), instruction, self.api_docs),
                lease,
                self.env.context(),
            )
            self.llm_job.request_metadata = dict(instruction=instruction,
                client_id=request.get('client_id'), request_epoch=lease.epoch)
            directory = self.trial_root / 'llm-requests'
            directory.mkdir(exist_ok=True)
            self.llm_job.record_path = directory / (uuid.uuid4().hex + '.json')
            self.llm_job.request_record = dict(self.llm_job.request_metadata,
                context=self.llm_job.context, provider=getattr(self.generator, 'metadata', {}))
            write_json(self.llm_job.record_path, dict(self.llm_job.request_record, status='pending'))
        self.service()

    def status(self):
        return dict(
            self.control.status(),
            message=self.message,
            error=self.error,
            trial_index=self.trial_index,
            resetting=self.pending_reset,
            seeking=self.seeking,
            adopting=self.adopting,
            timeline=self.timeline.status() if self.timeline else None,
            backend_id=getattr(getattr(self.env, 'backend', None), 'backend_id', None),
            capabilities=asdict(self.env.capabilities) if getattr(self.env, 'capabilities', None) else None,
            applied_steps=self.env.applied_steps,
            ended=self.env.ended,
            task_completed=self.env.task_completed(),
            vla_ready=self.predictor is not None and bool(getattr(self.predictor, "ready", True)),
            test_vla=bool(getattr(self.predictor, "is_test", False)),
            llm_ready=self.generator is not None,
            test_llm=getattr(self.generator, "metadata", {}).get("generator") == "test_generator",
            program_running=self.program is not None,
            llm_generating=bool(self.llm_job and self.control.valid(self.llm_job.lease)),
            generation=self.last_generation,
            llm_provider=getattr(self.generator, 'metadata', {}).get('model'),
            vla_instruction=self.vla_instruction,
            policy_control=self.policy_actions.metadata,
        )

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.control.switch("STOPPED", reason="session_closed")
        self._finish_program("session_closed")
        if self.llm_job is not None:
            job = self.llm_job
            write_json(job.record_path, dict(job.request_record, status='session_closed',
                generation=job.result if job.done.is_set() else None))
        for job in (self.vla_job, self.delegated):
            if job:
                self._cancel_job(job)
        self.service()
        self.env.after_step = None
        self.env.before_action = None
