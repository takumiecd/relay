import test from "node:test";
import assert from "node:assert/strict";
import { KeyboardController } from "../../capx/extensions/hybrid/keyboard.mjs";
function fixture(mode = "STOPPED", owned = true) {
  const calls = [],
    ctx = { mode, owned, pending: false };
  const keyboard = new KeyboardController({
    context: () => ctx,
    stop: () => calls.push("stop"),
    mode: (value) => calls.push(value),
    claim: () => calls.push("claim"),
    change: (value) => calls.push(["change", value]),
    hint: (value) => calls.push(["hint", value]),
    key: (value) => calls.push(["key", value]),
  });
  return { keyboard, calls, ctx };
}
function event(code, options = {}) {
  return {
    code,
    key: code,
    target: { tagName: "BODY" },
    prevented: false,
    preventDefault() {
      this.prevented = true;
    },
    stopPropagation() {},
    ...options,
  };
}
test("Space never scrolls or activates a focused button, including while stopped", () => {
  for (const mode of ["STOPPED", "VLA", "LLM", "HUMAN"]) {
    const { keyboard } = fixture(mode);
    const down = event("Space", { key: " ", target: { tagName: "BUTTON" } });
    const up = event("Space", { key: " ", target: { tagName: "BUTTON" } });
    keyboard.down(down);
    keyboard.up(up);
    assert.equal(down.prevented, true);
    assert.equal(up.prevented, true);
    assert.equal(keyboard.held.size, 0);
  }
});
test("Esc stops from the editor, with code/key/legacy aliases, and clears movement", () => {
  for (const fields of [
    { code: "Escape" },
    { code: "", key: "Escape" },
    { code: "Unidentified", key: "Esc" },
    { code: "", key: "", keyCode: 27 },
  ]) {
    const { keyboard, calls } = fixture("HUMAN");
    keyboard.held.add("Space");
    keyboard.held.add("KeyW");
    const key = event("", { ...fields, target: { tagName: "TEXTAREA" } });
    keyboard.down(key);
    assert.equal(key.prevented, true);
    assert.equal(keyboard.held.size, 0);
    assert.ok(calls.includes("stop"));
  }
});
test("Spaces and shortcut letters remain editable inside code", () => {
  const { keyboard, calls } = fixture("HUMAN");
  for (const code of ["Space", "KeyH", "KeyL", "KeyP", "KeyW"]) {
    const key = event(code, { target: { tagName: "TEXTAREA" } });
    keyboard.down(key);
    assert.equal(key.prevented, false);
  }
  assert.equal(keyboard.held.size, 0);
  assert.equal(calls.length, 0);
});
test("Range focus does not swallow Space or movement", () => {
  const { keyboard } = fixture("HUMAN");
  for (const code of ["Space", "KeyW"]) {
    const key = event(code, { target: { tagName: "INPUT", type: "range" } });
    keyboard.down(key);
    assert.ok(key.prevented);
    assert.ok(keyboard.held.has(code));
  }
});
test("Blur releases immediately; a new real key works in an embedded page", () => {
  const { keyboard, calls } = fixture("HUMAN");
  keyboard.down(event("Space"));
  keyboard.down(event("KeyR"));
  keyboard.blur();
  assert.equal(keyboard.focused, false);
  assert.equal(keyboard.held.size, 0);
  assert.ok(
    calls.some((c) => Array.isArray(c) && c[0] === "change" && c[1] === true),
  );
  keyboard.down(event("Space"));
  assert.equal(keyboard.focused, true);
  assert.ok(keyboard.held.has("Space"));
});
test("Reloaded manual page explicitly reclaims ownership on a physical Space press", () => {
  const { keyboard, calls } = fixture("HUMAN", false);
  keyboard.down(event("Space"));
  assert.ok(calls.includes("claim"));
  assert.ok(keyboard.held.has("Space"));
});
test("Legacy printable key values normalize to physical motion keys", () => {
  const { keyboard } = fixture("HUMAN");
  keyboard.down(event("", { key: "r" }));
  assert.ok(keyboard.held.has("KeyR"));
});

test("C/V select a persistent gripper intent before Space, and last selection wins", () => {
  const { keyboard } = fixture("HUMAN");
  keyboard.down(event("KeyC"));
  keyboard.up(event("KeyC"));
  assert.equal(keyboard.gripperKey, "KeyC");
  assert.equal(keyboard.held.has("KeyC"), false);
  keyboard.down(event("Space"));
  assert.equal(keyboard.gripperKey, "KeyC");
  keyboard.down(event("KeyV"));
  keyboard.up(event("KeyV"));
  assert.equal(keyboard.gripperKey, "KeyV");
  keyboard.up(event("Space"));
  assert.equal(keyboard.held.size, 0);
  assert.equal(keyboard.gripperKey, "KeyV");
});
