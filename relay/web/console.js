import { KeyboardController, MOVEMENT_KEYS, isEditing } from "./keyboard.mjs";
import { BranchGraph, branchName } from "./graph.mjs";
const $ = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="capx-token"]').content;
const client = crypto.randomUUID();
const MODE_NAMES = { STOPPED: "停止中", HUMAN: "手動", VLA: "VLA", LLM: "LLM" };
function executionNotice(value) {
  const raw = String(value.error || "");
  if (raw) {
    const ik = /MoveIt|IK solution|IK failed/.test(raw);
    return { message: ik ? "手先の位置・姿勢を計算できず停止しました。目標を近づけるか、Hで手動へ戻して逆方向を試してください。" : "実行を停止しました。詳細を確認し、コードまたは接続先を確認してください。", details: raw, error: true };
  }
  if (value.manual_rejection) {
    const detail = String(value.manual_rejection.details || "");
    const actionable = /^(関節\d+|手先の追従誤差|逆運動学)/.test(detail);
    return { message: actionable ? detail : value.manual_rejection.message,
      details: actionable ? "" : detail, error: false };
  }
  if (value.feedback_fresh === false)
    return { message: "関節状態の更新が止まっています。新しい指令は送れません。接続先を確認してください。", details: "JointState feedback is stale", error: true };
  if (value.controller_outcome && value.controller_outcome !== "succeeded")
    return { message: "指令の結果を確認できなかったため、この試行を停止しました。記録を保存し、接続先の状態を確認してください。", details: value.controller_outcome, error: true };
  return null;
}
let state = {
  mode: "STOPPED",
  epoch: -1,
  client_id: null,
  applied_steps: 0,
  trial_index: 1,
};
let commandSequence = 0,
  pendingCommand = null,
  inputSequence = 0,
  inputRequests = 0;
let lastKey = "—",
  frameBusy = false;
let flashMessage = null,
  lastFrameKey = null,
  connected = false;
const frameURLs = Object.create(null);
let seekPreview = null, draggingHistory = false;
const owned = () => state.mode === "HUMAN" && state.client_id === client;
let teleopReference = "world", referenceCatalog = null;
function referenceFrames() {
  return state.teleop?.reference_frames?.length ? state.teleop.reference_frames : [
    { id: "world", label: state.teleop?.control_space === "joint" ? "関節（固定）" : "ワールド",
      description: "W/S ±X、A/D ±Y、R/F 上下" },
  ];
}
function syncReferenceFrames() {
  const frames = referenceFrames(), catalog = JSON.stringify(frames);
  const selector = $("teleop-reference");
  if (catalog !== referenceCatalog) {
    const stored = localStorage.getItem("relay.teleop-reference");
    teleopReference = frames.some((frame) => frame.id === stored) ? stored
      : frames.some((frame) => frame.id === state.teleop?.default_reference)
        ? state.teleop.default_reference : "world";
    selector.replaceChildren(...frames.map((frame) => {
      const option = document.createElement("option");
      option.value = frame.id;
      option.textContent = frame.label;
      return option;
    }));
    referenceCatalog = catalog;
  }
  selector.value = teleopReference;
  selector.disabled = frames.length < 2 || !connected || pendingCommand !== null
    || !!state.resetting || !!state.seeking || !!state.adopting
    || keyboard.held.has("Space") || (owned() && !!state.deadman);
}
function focusWorkspace() {
  document.body.focus({ preventScroll: true });
  keyboard.focused = true;
}
function flash(text, error = false) {
  flashMessage = { text, error, until: performance.now() + 3500 };
  render();
}
async function api(path, data, timeout = 1800) {
  const abort = new AbortController(),
    timer = setTimeout(() => abort.abort(), timeout);
  try {
    const response = await fetch(path, {
      method: data ? "POST" : "GET",
      signal: abort.signal,
      headers: {
        "X-CapX-Token": token,
        ...(data ? { "Content-Type": "application/json" } : {}),
      },
      body: data ? JSON.stringify(data) : undefined,
      cache: "no-store",
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "通信に失敗しました");
    return result;
  } finally {
    clearTimeout(timer);
  }
}
function mergeStatus(next) {
  if (next.epoch >= state.epoch) state = { ...state, ...next };
}
async function command(path, body, { preserveKeys = false, message } = {}) {
  const stop = path === "/api/mode" && body.mode === "STOPPED";
  if (pendingCommand !== null && !stop) return;
  if (!preserveKeys) {
    keyboard.gripperKey = null;
    keyboard.release();
  }

  const id = ++commandSequence;
  pendingCommand = id;
  const payload = {
    ...body,
    client_id: client,
    sequence: id,
    expected_epoch: state.epoch,
  };
  render();
  try {
    const next = await api(path, payload);
    // A slow pre-stop response must never restore an older local mode.
    if (id !== commandSequence) return;
    mergeStatus(next);
    if (next.accepted === false)
      flash("操作状態が変わりました。もう一度操作してください");
    else if (message) flash(message);
  } catch (error) {
    if (id === commandSequence)
      flash(
        stop
          ? "停止の応答を確認できません。接続を確認してください"
          : error.message,
        true,
      );
  } finally {
    if (pendingCommand === id) pendingCommand = null;
    render();
    if (preserveKeys) sendInput();
  }
}
function mode(value, options = {}) {
  if (value !== "STOPPED" && state.ended) {
    flash("この試行は終了しました。新しい試行を始めてください");
    return;
  }
  if (
    (value === "VLA" && !state.vla_ready) ||
    (value === "LLM" && !state.llm_ready)
  ) {
    flash("接続先が準備できていません");
    return;
  }
  const body = { mode: value };
  if (value === "LLM" || value === "VLA") {
    const instruction = $("llm-instruction").value.trim();
    if (!instruction && value === "LLM") {
      flash("操作指示を入力してください");
      $("llm-instruction").focus();
      return;
    }
    body.instruction = instruction || state.instruction;
  }
  return command("/api/mode", body, options);
}
const keyboard = new KeyboardController({
  context: () => ({
    mode: state.mode,
    owned: owned(),
    pending: pendingCommand !== null || state.seeking || state.adopting,
  }),
  stop: () => mode("STOPPED", { message: "Escで停止しました" }),
  mode: (value) => mode(value),
  claim: () =>
    mode("HUMAN", {
      preserveKeys: true,
      message: "この画面へ操作を引き継ぎました",
    }),
  change: (force) => {
    render();
    if (force || !keyboard?.held.has("Space")) sendInput(!!force);
  },
  hint: (text) => flash(text),
  key: (value) => {
    lastKey = value;
    render();
  },
});
window.addEventListener("keydown", (event) => keyboard.down(event), {
  capture: true,
});
window.addEventListener("keyup", (event) => keyboard.up(event), {
  capture: true,
});
window.addEventListener("blur", () => {

  keyboard.blur();
});
window.addEventListener("focus", () => {
  keyboard.focused = true;
});
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {

    keyboard.blur();
  }
});
document.addEventListener("focusin", (event) => {
  if (isEditing(event.target)) keyboard.release();
});
document.addEventListener(
  "pointerdown",
  (event) => {
    if (!isEditing(event.target)) keyboard.focused = true;
  },
  { capture: true },
);
for (const button of document.querySelectorAll("[data-mode]"))
  button.onclick = () => {
    focusWorkspace();
    mode(button.dataset.mode);
  };
$("stop").onclick = () => {
  focusWorkspace();
  mode("STOPPED", { message: "停止しました" });
};
function newTrial() {
  focusWorkspace();
  command("/api/trial", {}, { message: "記録を残して新しい試行を準備します" });
}
$("new-trial").onclick = newTrial;
$("next-trial").onclick = newTrial;
$("control-focus").onclick = () => {
  focusWorkspace();
  if (!owned()) mode("HUMAN");
  else flash("Spaceを押しながらWASD / RFで操作できます");
};
$("run-code").onclick = () => {
  const code = $("code").value;
  focusWorkspace();
  command("/api/code", { code });
};
$("code").oninput = () => { $("code").dataset.userEdited = "1"; };
$("generate-code").onclick = () => {
  focusWorkspace();
  mode("LLM");
};
async function seek(step, branch = state.timeline?.branch) {
  if (!state.timeline || state.seeking || state.resetting || state.adopting) return;
  const head = state.timeline.branches.find((item) => item.id === branch)?.head;
  if (!Number.isInteger(step) || step < 0 || step > head) {
    flash("記録されているstepを選んでください");
    return;
  }
  focusWorkspace();
  seekPreview = step;
  await command("/api/seek", { branch, step });
}
const branchGraph = new BranchGraph($("branch-graph"), seek, (text) => { $("graph-hint").textContent = text; });
function setHistoryExpanded(expanded) {
  $("history-body").hidden = !expanded;
  $("toggle-history").setAttribute("aria-expanded", String(expanded));
  const label = expanded ? "履歴を折りたたむ" : "履歴を展開する";
  $("toggle-history").setAttribute("aria-label", label);
  $("toggle-history").title = label;
  localStorage.setItem("capx.history.expanded", String(expanded));
}
$("toggle-history").onclick = () => {
  setHistoryExpanded($("history-body").hidden);
  focusWorkspace();
};
setHistoryExpanded(localStorage.getItem("capx.history.expanded") !== "false");
$("adopt-route").onclick = () => {
  const history = state.timeline;
  if (!history) return;
  focusWorkspace();
  command("/api/adopt", { branch: history.branch, end_step: history.head },
    { message: `step ${history.head}までの経路を保存しています` });
};
$("history-step").onpointerdown = () => { draggingHistory = true; };
$("history-step").oninput = () => {
  seekPreview = Number($("history-step").value);
  $("step-number").value = seekPreview;
  $("history-status").textContent = `移動先 step ${seekPreview}`;
};
$("history-step").onchange = () => {
  draggingHistory = false;
  seek(Number($("history-step").value));
};
$("history-step").onpointerup = () => { draggingHistory = false; };
$("history-step").onpointercancel = () => { draggingHistory = false; seekPreview = null; };
$("step-back").onclick = () => seek(state.timeline.cursor - 1);
$("step-forward").onclick = () => seek(state.timeline.cursor + 1);
$("history-head").onclick = () => seek(state.timeline.head);
$("step-number").onchange = () => seek(Number($("step-number").value));
$("step-number").onkeydown = (event) => {
  if (event.key === "Enter") { event.preventDefault(); seek(Number(event.target.value)); }
};
$("history-branch").onchange = () => {
  const branch = state.timeline.branches.find((item) => item.id === $("history-branch").value);
  seek(Math.min(state.timeline.cursor, branch.head), branch.id);
};
$("resume-here").onclick = () => { focusWorkspace(); mode("HUMAN"); };
function renderHistory(busy) {
  const history = state.timeline;
  $("history-panel").hidden = state.capabilities?.checkpoints === false;
  const unavailable = !history || busy;
  for (const id of ["history-step", "step-number", "history-branch", "history-head"])
    $(id).disabled = unavailable;
  $("step-back").disabled = unavailable || history.cursor <= 0;
  $("step-forward").disabled = unavailable || history.cursor >= history.head;
  $("resume-here").disabled = unavailable || state.ended || state.epoch < 0;
  $("adopt-route").disabled = unavailable || history.head === 0;
  if (!history) return;
  branchGraph.render(history, busy);
  const adopted = history.adopted;
  const adoptedBranch = adopted && history.branches.find((branch) => branch.id === adopted.branch);
  $("adopted-route").textContent = adopted
    ? `${branchName(adoptedBranch)} · step ${adopted.end_step}までを採用済み` +
      (Number.isInteger(adopted.code_candidate_count) ? ` · コード候補 ${adopted.code_candidate_count}件` : "")
    : "まだ経路を採用していません";
  $("adopted-route").classList.toggle("selected", !!adopted);
  const sameSelection = adopted?.branch === history.branch && adopted?.end_step === history.head;
  $("adopt-route").textContent = sameSelection ? "この経路は採用済み" : `この経路を採用 · step ${history.head}まで`;
  $("adopt-route").disabled ||= sameSelection;
  const optionsKey = JSON.stringify(history.branches);
  if ($("history-branch").dataset.options !== optionsKey) {
    $("history-branch").replaceChildren(...history.branches.map((branch) => {
      const option = document.createElement("option");
      option.value = branch.id;
      option.textContent = (branch.parent ? `分岐 ${Number(branch.id)}` : "元の経路") + ` · ${branch.head} steps`;
      return option;
    }));
    $("history-branch").dataset.options = optionsKey;
  }
  $("history-branch").value = history.branch;
  $("history-step").max = $("step-number").max = history.head;
  if (!draggingHistory && seekPreview === null) {
    $("history-step").value = history.cursor;
    if (document.activeElement !== $("step-number")) $("step-number").value = history.cursor;
  }
  $("history-status").textContent = state.seeking
    ? "状態を復元しています…"
    : seekPreview !== null ? `移動先 step ${seekPreview}`
    : `step ${history.cursor} / ${history.head}`;
  $("history-hint").textContent = history.cursor < history.head
    ? "ここから動かすと新しい分岐になります。元の経路も残ります。"
    : "バーを離すとその状態へ移動・停止します。P / Lで自動操作も再開できます。";
}
let speed = Number(localStorage.getItem("capx.speed") || 25);
$("speed").value = Number.isFinite(speed)
  ? Math.max(2, Math.min(100, speed))
  : 25;
function updateSpeed() {
  localStorage.setItem("capx.speed", $("speed").value);
  $("speed-value").innerHTML = $("speed").value + "<span>%</span>";
}
$("speed").oninput = updateSpeed;
updateSpeed();
async function sendInput(forceRelease = false) {
  if (
    !owned() ||
    ((pendingCommand !== null || state.seeking || state.adopting) && !forceRelease) ||
    (inputRequests > 0 && !forceRelease)
  )
    return;
  const enabled =
    !forceRelease &&
    keyboard.focused &&
    !document.hidden &&
    !isEditing(document.activeElement) &&
    keyboard.held.has("Space") &&
    !state.ended;
  const selecting =
    !forceRelease &&
    keyboard.focused &&
    !document.hidden &&
    !isEditing(document.activeElement);
  const gripKeys =
    selecting && keyboard.gripperKey ? [keyboard.gripperKey] : [];
  const sequence = ++inputSequence;
  inputRequests++;
  try {
    await api(
      "/api/input",
      {
        client_id: client,
        sequence,
        epoch: state.epoch,
        keys: enabled
          ? [...keyboard.held]
              .filter(
                (key) =>
                  MOVEMENT_KEYS.has(key) && !["KeyC", "KeyV"].includes(key),
              )
              .concat(gripKeys)
          : gripKeys,
        axes: [0, 0, 0, 0, 0, 0],
        reference: teleopReference,
        speed: Number($("speed").value) / 100,
        deadman: enabled,
      },
      900,
    );
  } catch (error) {
    if (enabled)
      flash("入力の通信を確認しています。手動モードは継続します", true);
  } finally {
    inputRequests--;
  }
}
setInterval(() => sendInput(), 50);
$("teleop-reference").onchange = () => {
  if (keyboard.held.has("Space") || (owned() && state.deadman)) {
    $("teleop-reference").value = teleopReference;
    flash("Spaceを離してから操作の基準を選んでください");
    return;
  }
  teleopReference = $("teleop-reference").value;
  localStorage.setItem("relay.teleop-reference", teleopReference);
  keyboard.release(); // A release packet also invalidates older in-flight inputs.
  focusWorkspace();
  render();
};
function render() {
  syncReferenceFrames();
  const jointControl = state.teleop?.control_space === "joint";
  const frame = referenceFrames().find((item) => item.id === teleopReference);
  $("position-label").textContent = jointControl ? "J1 / J2 / J3 [rad]" : "手先 XYZ [m]";
  $("control-space").textContent = jointControl ? "関節操作：W/S=J1、A/D=J2、R/F=J3、Z/X=J4、Q/E=J6" : frame.description;
  $("rf-label").textContent = jointControl ? "J3" : "上下";
  $("qe-label").textContent = jointControl ? "J6" : "回転";
  $("motion-help").textContent = jointControl ? "キーは関節角を変更します。手先XYZやIKの指令ではありません。J5はコードのmove_jointsで操作できます。" : frame.description + "。回転はワールド基準。";
  $("llm-instruction").placeholder = jointControl ? "例：関節1を現在位置から0.04rad動かしてください。" : "例：現在位置から手先を上に2cm動かして、グリッパーを閉じてください。";
  if (jointControl && !$("code").dataset.initialized && !$("code").dataset.userEdited) {
    $("code").value = "s = get_robot_state()\nq = s['robot_joint_pos'][:]\nq[0] += 0.04\nRESULT = move_joints(q)";
    $("code").dataset.initialized = "joint";
  }
  const busy = pendingCommand !== null || state.resetting || state.seeking || state.adopting;
  const localHeld = keyboard?.held.has("Space");
  const receiving = owned() && localHeld && state.input_fresh && state.deadman;
  const blocked = owned() && !!state.manual_rejection;
  const notice = executionNotice(state);
  $("connection").lastChild.textContent = connected
    ? "接続済み"
    : "接続を確認中";
  $("test").hidden = !(state.test_vla || state.test_llm || state.test_operator);
  $("instruction").textContent = state.instruction || "環境を読み込んでいます";
  $("trial").textContent =
    "· TRIAL " + String(state.trial_index || 1).padStart(2, "0");
  renderHistory(busy);
  $("step-label").textContent = state.timeline ? "経路step" : "記録済み指令";
  $("steps").textContent = state.timeline ? `${state.timeline.cursor} / ${state.timeline.head}` : String(state.applied_steps ?? 0);
  $("mode-chip").textContent = state.adopting ? "採用を保存中" : state.seeking ? "移動中" : state.resetting
    ? "準備中"
    : state.ended
      ? "試行終了"
      : state.mode === "LLM" && state.llm_generating ? "LLM生成中"
      : state.mode === "LLM" && state.program_running && state.generation?.generator === "operator_code" ? "Pythonコード"
      : MODE_NAMES[state.mode];
  $("mode-chip").dataset.mode = state.mode;
  $("message").textContent = state.resetting
    ? "記録を保存して新しい試行を準備しています"
    : busy
      ? "切り替えています"
      : state.ended
        ? "この試行は終了しました"
        : state.mode === "HUMAN" && !owned()
          ? "Space / Hでこの画面へ引き継ぎ"
          : blocked ? "この方向は一旦停止。別のキーで再開できます。"
          : state.mode === "HUMAN"
            ? "手動モードを継続しています"
            : state.mode === "STOPPED"
              ? "停止中"
              : state.message || "停止中";
  $("status-dot").classList.toggle(
    "active",
    state.mode !== "STOPPED" && !state.ended && !blocked,
  );
  $("episode-banner").hidden = !state.ended || state.resetting;
  $("episode-title").textContent = state.task_completed
    ? "タスク完了"
    : "この試行は終了しました";
  $("episode-help").textContent = state.capabilities?.reset === false
    ? "この接続先はリセット・巻き戻し非対応です。記録を保存して終了し、接続先の状態を確認して新しいセッションを開始してください。"
    : state.capabilities?.checkpoints === false
      ? "新しい試行を始められます。この環境は巻き戻し非対応です。"
      : "履歴から途中へ戻るか、新しい試行を始められます。";
  for (const button of document.querySelectorAll("[data-mode]")) {
    button.classList.toggle("active", button.dataset.mode === state.mode);
    button.disabled =
      busy ||
      !!state.ended ||
      state.epoch < 0 ||
      (button.dataset.mode === "VLA" && !state.vla_ready) ||
      (button.dataset.mode === "LLM" && !state.llm_ready);
  }
  $("new-trial").disabled = $("next-trial").disabled = busy || state.epoch < 0 || state.capabilities?.reset === false;
  $("recording-note-text").textContent = state.capabilities?.checkpoints === false
    ? "操作と画像を記録します。この環境は巻き戻し未対応です。"
    : "すべての経路を残しながら操作できます。";
  $("run-code").disabled = busy || !!state.ended || state.code_ready === false;
  $("generate-code").disabled = busy || !!state.ended || !state.llm_ready || state.llm_generating || state.program_running;
  $("llm-model").textContent = state.llm_provider || "未設定";
  $("llm-model").title = state.llm_provider || "";
  const generation = state.generation;
  const stageNames = { running: "コード実行中", program_completed: "コード完了", interrupted: "引き継ぎにより中断", program_error: "コードエラー", program_timeout: "実行時間の上限", route_adoption: "経路の採用で停止", timeline_seek: "履歴の移動で停止", new_trial: "新しい試行で停止" };
  $("generation-status").textContent = state.llm_generating ? "生成中です。Escで停止、Hで手動に引き継げます。"
    : state.error ? "コード実行を停止しました。画面下の詳細を確認してください。"
    : generation ? `${stageNames[generation.stage] || generation.stage} · ${generation.applied_actions ?? 0} actions`
    : state.code_ready === false ? "このセッションではPython実行を無効にしています。"
    : "画像と状態からPythonコードを生成します。Escで停止できます。";
  $("generated-code").textContent = generation?.code || "まだ生成していません";
  $("generation-instruction").textContent = generation?.instruction || "";
  $("control-focus").disabled = !!state.ended || busy;
  $("stop").disabled = false;
  $("vla-provider").textContent = state.test_vla
    ? "試験入力"
    : state.vla_ready
      ? "接続済み"
      : "接続待ち";
  $("llm-provider").textContent = state.test_llm
    ? "試験コード"
    : state.llm_ready
      ? "API設定済み"
      : "未接続";
  $("owner").textContent = owned()
    ? "この画面が操作中"
    : state.mode === "HUMAN"
      ? "引継ぎできます"
      : "Hで開始";
  $("owner").classList.toggle("active", owned());
  $("control-focus").classList.toggle("on", receiving && !blocked);
  $("control-focus").classList.toggle("blocked", blocked);
  $("deadman-text").textContent = blocked ? "この方向は一旦停止しています" : receiving
    ? "操作を受け付けています"
    : localHeld && owned()
      ? "入力を確認しています"
      : "押している間だけ動きます";
  $("input-state").textContent = state.ended
    ? "試行終了"
    : owned()
      ? blocked ? "別のキー・速度で操作し直せます" : localHeld
        ? receiving
          ? "入力を受信しました"
          : "入力を確認中"
        : "入力を待っています"
      : "操作は停止しています";
  $("last-key").textContent = "キー：" + lastKey;
  for (const key of document.querySelectorAll("[data-key]"))
    key.classList.toggle("held", keyboard?.held.has(key.dataset.key));
  const current =
    flashMessage && performance.now() < flashMessage.until
      ? flashMessage
      : null;
  $("notice").textContent = notice?.message || current?.text || "";
  $("notice").classList.toggle("error", !!notice?.error || !!current?.error);
  $("notice").classList.toggle("warning", !!notice && !notice.error);
  $("diagnostic-details").hidden = !notice?.details;
  $("diagnostic-error").textContent = notice?.details || "";
  const positionValues = jointControl ? state.joint_positions_rad?.slice(0,3) : state.eef_position_m;
  if (positionValues) {
    $("position").replaceChildren(
      ...positionValues.flatMap((value, index) => {
        const node = document.createTextNode(Number(value).toFixed(3));
        if (!index) return [node];
        const separator = document.createElement("em");
        separator.textContent = "/";
        return [separator, node];
      }),
    );
  }
}
let polling = false;
async function update() {
  if (polling) return;
  polling = true;
  try {
    const next = await api("/api/state");
    connected = true;
    if (next.epoch >= state.epoch && pendingCommand === null) {
      if (next.epoch !== state.epoch) {
        keyboard.gripperKey = null;
        keyboard.release();

      }
      mergeStatus(next);
      if (!next.seeking && !draggingHistory) seekPreview = null;
    }
    render();
  } catch (error) {
    connected = false;
    flash("接続を確認しています", true);
  } finally {
    polling = false;
  }
}
setInterval(update, 120);
update();
async function frames() {
  if (frameBusy) return;
  const names = Array.isArray(state.cameras) ? state.cameras : [];
  if (!names.length) return;
  syncCameras(names);
  const key = [
    state.context?.session_id,
    state.epoch,
    state.context?.observation_id,
  ].join(":");
  if (key === lastFrameKey && names.every((name) => frameURLs[name])) return;
  frameBusy = true;
  try {
    for (const name of names) {
      const response = await fetch("/camera/" + encodeURIComponent(name) + ".png", {
        headers: { "X-CapX-Token": token },
        cache: "no-store",
      });
      if (!response.ok) continue;
      const url = URL.createObjectURL(await response.blob()),
        old = frameURLs[name];
      cameraPanel(name).querySelector("img").src = url;
      frameURLs[name] = url;
      if (old) URL.revokeObjectURL(old);
    }
    lastFrameKey = key;
  } catch (error) {
  } finally {
    frameBusy = false;
  }
}
setInterval(frames, 120);
frames();
function primary(name) {
  const panel = cameraPanel(name);
  if (!panel) return;
  $("cameras").prepend(panel);
  localStorage.setItem("capx.camera", name);
  focusWorkspace();
}
let dragged = null;
let cameraCatalog = null;
function cameraPanel(name) {
  return [...$("cameras").children].find((panel) => panel.dataset.camera === name);
}
function syncCameras(names) {
  const catalog = JSON.stringify(names);
  if (catalog === cameraCatalog) return;
  cameraCatalog = catalog;
  $("cameras").dataset.count = String(names.length);
  $("cameras").dataset.multi = String(names.length > 2);
  $("cameras").style.setProperty("--camera-rows", String(Math.max(1, names.length - 1)));
  for (const panel of [...$("cameras").children]) {
    if (!names.includes(panel.dataset.camera)) {
      const name = panel.dataset.camera;
      if (frameURLs[name]) URL.revokeObjectURL(frameURLs[name]);
      delete frameURLs[name];
      panel.remove();
    }
  }
  for (const name of names) {
    let panel = cameraPanel(name);
    if (!panel) {
      panel = document.createElement("article");
      panel.className = "camera";
      panel.draggable = true;
      panel.dataset.camera = name;
      panel.innerHTML = '<div class="camera-head"><span></span><button>主表示にする ↗</button></div><div class="viewport"><img></div><div class="camera-caption">ドラッグで配置を変更</div>';
      panel.querySelector(".camera-head span").textContent = state.camera_labels?.[name] || name;
      panel.querySelector("img").alt = state.camera_labels?.[name] || name;
      $("cameras").append(panel);
    }
    panel.querySelector("button").onclick = () => primary(name);
    bindCameraDrag(panel);
  }
  const preference = localStorage.getItem("capx.camera");
  if (names.includes(preference)) $("cameras").prepend(cameraPanel(preference));
  lastFrameKey = null;
}
function bindCameraDrag(panel) {
  panel.ondragstart = () => {
    dragged = panel;
    panel.classList.add("dragging");
  };
  panel.ondragend = () => {
    panel.classList.remove("dragging");
    dragged = null;
  };
  panel.ondragover = (event) => event.preventDefault();
  panel.ondrop = (event) => {
    event.preventDefault();
    if (dragged && dragged !== panel) primary(dragged.dataset.camera);
  };
}
