import { KeyboardController, MOVEMENT_KEYS, isEditing } from "./keyboard.mjs";
const $ = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="capx-token"]').content;
const client = crypto.randomUUID();
const MODE_NAMES = { STOPPED: "停止中", HUMAN: "手動", VLA: "VLA", LLM: "LLM" };
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
let padArmed = false,
  lastPadButtons = [],
  lastKey = "—",
  frameBusy = false;
let flashMessage = null,
  lastFrameKey = null,
  connected = false;
const frameURLs = {};
const owned = () => state.mode === "HUMAN" && state.client_id === client;
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
  if (!preserveKeys) keyboard.release();
  padArmed = false;
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
  return command("/api/mode", { mode: value }, options);
}
const keyboard = new KeyboardController({
  context: () => ({
    mode: state.mode,
    owned: owned(),
    pending: pendingCommand !== null,
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
  padArmed = false;
  keyboard.blur();
});
window.addEventListener("focus", () => {
  keyboard.focused = true;
});
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    padArmed = false;
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
function padInput() {
  const pad = Array.from(navigator.getGamepads?.() || []).find(
    (pad) => pad && pad.mapping === "standard",
  );
  if (!pad) {
    lastPadButtons = [];
    padArmed = false;
    return { axes: [0, 0, 0, 0, 0, 0], keys: [], enabled: false };
  }
  const pressed = (index) => !!pad.buttons[index]?.pressed,
    deadzone = (value) => (Math.abs(value || 0) < 0.15 ? 0 : value);
  if (
    !isEditing(document.activeElement) &&
    keyboard.focused &&
    !document.hidden
  ) {
    for (const [index, value] of [
      [1, "HUMAN"],
      [3, "VLA"],
      [2, "LLM"],
      [9, "STOPPED"],
    ])
      if (pressed(index) && !lastPadButtons[index]) mode(value);
  }
  if (!owned() || pendingCommand !== null || !keyboard.focused || !pressed(5))
    padArmed = false;
  else if (!lastPadButtons[5]) padArmed = true;
  lastPadButtons = pad.buttons.map((button) => button.pressed);
  return {
    axes: [
      -deadzone(pad.axes[1]),
      -deadzone(pad.axes[0]),
      (pad.buttons[7]?.value || 0) - (pad.buttons[6]?.value || 0),
      0,
      deadzone(pad.axes[3]),
      -deadzone(pad.axes[2]),
    ],
    keys: [...(pressed(4) ? ["KeyC"] : []), ...(pressed(0) ? ["KeyV"] : [])],
    enabled: pressed(5) && padArmed,
  };
}
async function sendInput(forceRelease = false) {
  const pad = padInput();
  if (
    !owned() ||
    (pendingCommand !== null && !forceRelease) ||
    (inputRequests > 0 && !forceRelease)
  )
    return;
  const enabled =
    !forceRelease &&
    keyboard.focused &&
    !document.hidden &&
    !isEditing(document.activeElement) &&
    (keyboard.held.has("Space") || pad.enabled) &&
    !state.ended;
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
          ? [
              ...new Set(
                [...keyboard.held]
                  .filter((key) => MOVEMENT_KEYS.has(key))
                  .concat(pad.keys),
              ),
            ]
          : [],
        axes: enabled ? pad.axes : [0, 0, 0, 0, 0, 0],
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
function render() {
  const busy = pendingCommand !== null || state.resetting;
  const localHeld = keyboard?.held.has("Space") || padArmed;
  const receiving = owned() && localHeld && state.input_fresh && state.deadman;
  $("connection").lastChild.textContent = connected
    ? "接続済み"
    : "接続を確認中";
  $("test").hidden = !(state.test_vla || state.test_llm || state.test_operator);
  $("instruction").textContent = state.instruction || "環境を読み込んでいます";
  $("trial").textContent =
    "· TRIAL " + String(state.trial_index || 1).padStart(2, "0");
  $("steps").textContent = state.applied_steps || 0;
  $("mode-chip").textContent = state.resetting
    ? "準備中"
    : state.ended
      ? "試行終了"
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
          : state.mode === "HUMAN"
            ? "手動モードを継続しています"
            : state.mode === "STOPPED"
              ? "停止中"
              : state.message || "停止中";
  $("status-dot").classList.toggle(
    "active",
    state.mode !== "STOPPED" && !state.ended,
  );
  $("episode-banner").hidden = !state.ended || state.resetting;
  $("episode-title").textContent = state.task_completed
    ? "タスク完了"
    : "この試行は終了しました";
  for (const button of document.querySelectorAll("[data-mode]")) {
    button.classList.toggle("active", button.dataset.mode === state.mode);
    button.disabled =
      busy ||
      !!state.ended ||
      state.epoch < 0 ||
      (button.dataset.mode === "VLA" && !state.vla_ready) ||
      (button.dataset.mode === "LLM" && !state.llm_ready);
  }
  $("new-trial").disabled = $("next-trial").disabled = busy || state.epoch < 0;
  $("run-code").disabled = busy || !!state.ended;
  $("control-focus").disabled = !!state.ended || !!state.resetting;
  $("stop").disabled = false;
  $("vla-provider").textContent = state.test_vla
    ? "試験入力"
    : state.vla_ready
      ? "接続済み"
      : "接続待ち";
  $("llm-provider").textContent = state.test_llm
    ? "試験コード"
    : state.llm_ready
      ? "API接続"
      : "未接続";
  $("owner").textContent = owned()
    ? "この画面が操作中"
    : state.mode === "HUMAN"
      ? "引継ぎできます"
      : "Hで開始";
  $("owner").classList.toggle("active", owned());
  $("control-focus").classList.toggle("on", receiving);
  $("deadman-text").textContent = receiving
    ? "操作を受け付けています"
    : localHeld && owned()
      ? "入力を確認しています"
      : "押している間だけ動きます";
  $("input-state").textContent = state.ended
    ? "試行終了"
    : owned()
      ? localHeld
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
  $("notice").textContent = state.error || current?.text || "";
  $("notice").classList.toggle("error", !!state.error || !!current?.error);
  if (state.eef_position_m) {
    $("position").replaceChildren(
      ...state.eef_position_m.flatMap((value, index) => {
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
        keyboard.release();
        padArmed = false;
      }
      mergeStatus(next);
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
  const key = [
    state.context?.session_id,
    state.epoch,
    state.context?.observation_id,
  ].join(":");
  if (key === lastFrameKey && frameURLs.wrist && frameURLs.agentview) return;
  frameBusy = true;
  try {
    for (const name of ["wrist", "agentview"]) {
      const response = await fetch("/camera/" + name + ".png", {
        headers: { "X-CapX-Token": token },
        cache: "no-store",
      });
      if (!response.ok) continue;
      const url = URL.createObjectURL(await response.blob()),
        old = frameURLs[name];
      $(name).src = url;
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
  const panel = document.querySelector('[data-camera="' + name + '"]');
  $("cameras").prepend(panel);
  localStorage.setItem("capx.camera", name);
  focusWorkspace();
}
for (const button of document.querySelectorAll("[data-primary]"))
  button.onclick = () => primary(button.dataset.primary);
const preference = localStorage.getItem("capx.camera");
if (["wrist", "agentview"].includes(preference)) primary(preference);
let dragged = null;
for (const panel of document.querySelectorAll(".camera")) {
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
