"""Owner-thread scheduler. Inference and generated Python never own MuJoCo."""

from __future__ import annotations

from collections import deque
import json
import inspect
from pathlib import Path
import threading
import time

from capx.extensions.vla.api import VlaApi
from capx.extensions.vla.recording import write_json
from .code_worker import CodeProcess, TOOLS
from .control import AuthorityLost, ControlSession
from .llm import extract_code


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
        self.api = VlaApi(env, ResponsivePredictor(self))
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
        self.pending_lock = threading.Lock()
        self.env.after_step = self._after_step
        self.service()

    @property
    def api_docs(self):
        # Describe exactly the functions available in the code process.
        return "\n\n".join(
            f"{name}{inspect.signature(getattr(self.api, name))}\n"
            f"{inspect.getdoc(getattr(self.api, name)) or ''}"
            for name in sorted(TOOLS)
        )

    def submit_code(self, code, client_id):
        code = extract_code(code)
        with self.pending_lock:
            status = self.control.switch("LLM", reason="operator_code")
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
        metadata = getattr(self.predictor, "ready", None)
        if isinstance(metadata, dict) and metadata != self.policy_metadata:
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
        self.program = CodeProcess(code, self.env.instruction, lease, timeout=self.code_timeout)
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
            self.error = str(result.get("error") or result.get("info", {}).get("stderr", reason))[
                :500
            ]
        program.close()
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
            elif name in TOOLS:
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
        if self.llm_job and self.llm_job.done.is_set():
            job, self.llm_job = self.llm_job, None
            if self.control.valid(job.lease):
                if job.error:
                    self.error = self.message = str(job.error)[:300]
                    self.control.switch("STOPPED", reason="llm_error")
                else:
                    data = job.result
                    self._start_program(
                        extract_code(data["code"]), job.context, data["metadata"], data
                    )
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
                action, lease, provenance = item
                self.env.apply_native(
                    action,
                    context=self.env.context(),
                    source=lease.source,
                    lease=lease,
                    provenance=provenance,
                )
                self.message = "操作を受付中"
            else:
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
                            or reply.get("action_contract") != job.context["action_contract"]
                        ):
                            raise ValueError("VLA reply context or contract mismatch")
                        actions = self.env.validate_chunk(reply["native_actions"])
                        self.chunk = deque(
                            (
                                a.tolist(),
                                dict(
                                    request_id=reply.get("request_id"),
                                    chunk_index=i,
                                    chunk_size=len(actions),
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
                        action,
                        context=self.chunk_context,
                        source=source,
                        lease=self.chunk_lease,
                        provenance=provenance,
                    )
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
                    (self.env.policy_observation(), self.env.instruction, context),
                    self.control.begin(source),
                    context,
                )
        elif mode == "LLM" and self.llm_job is None and self.generator is not None:
            # Generation itself owns no code run; capture an epoch lease for late-result rejection.
            from .control import Lease

            lease = Lease(self.env.epoch, "LLM", "CODE", None)
            self.llm_job = Job(
                self.generator.generate,
                (self.env.policy_observation(), self.env.instruction, self.api_docs),
                lease,
                self.env.context(),
            )
        self.service()

    def status(self):
        return dict(
            self.control.status(),
            message=self.message,
            error=self.error,
            applied_steps=self.env.applied_steps,
            ended=self.env.ended,
            task_completed=self.env.task_completed(),
            vla_ready=self.predictor is not None and bool(getattr(self.predictor, "ready", True)),
            test_vla=bool(getattr(self.predictor, "is_test", False)),
            llm_ready=self.generator is not None,
            test_llm=getattr(self.generator, "metadata", {}).get("generator") == "test_generator",
            program_running=self.program is not None,
        )

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.control.switch("STOPPED", reason="session_closed")
        self._finish_program("session_closed")
        for job in (self.vla_job, self.delegated):
            if job:
                self._cancel_job(job)
        self.service()
        self.env.after_step = None
