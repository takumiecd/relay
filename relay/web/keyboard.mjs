// Physical key handling is separate from network/UI state so its contracts are testable.
export const MOVEMENT_KEYS = new Set([
  "KeyW",
  "KeyS",
  "KeyA",
  "KeyD",
  "KeyR",
  "KeyF",
  "KeyQ",
  "KeyE",
  "KeyZ",
  "KeyX",
  "KeyC",
  "KeyV",
  "Space",
  "ShiftLeft",
  "ShiftRight",
]);
export function keyCode(event) {
  if (
    event.key === "Escape" ||
    event.key === "Esc" ||
    event.code === "Escape" ||
    event.keyCode === 27
  )
    return "Escape";
  if (event.code && event.code !== "Unidentified") return event.code;
  if (event.key === " " || event.key === "Spacebar" || event.keyCode === 32)
    return "Space";
  if (/^[a-z]$/i.test(event.key || "")) return "Key" + event.key.toUpperCase();
  return event.key === "Shift" ? "ShiftLeft" : "";
}
export function isEditing(target) {
  return (
    !!target &&
    (target.isContentEditable ||
      ["TEXTAREA", "SELECT"].includes(target.tagName) ||
      (target.tagName === "INPUT" &&
        !["range", "button", "submit", "checkbox"].includes(target.type)))
  );
}
export class KeyboardController {
  constructor({ context, stop, mode, claim, change, hint, key }) {
    Object.assign(this, { context, stop, mode, claim, change, hint, key });
    this.held = new Set();
    this.gripperKey = null;
    this.focused = true;
  }
  prevent(event) {
    event.preventDefault();
    event.stopPropagation?.();
  }
  down(event) {
    const code = keyCode(event);
    if (code === "Escape") {
      this.prevent(event);
      this.release();
      this.key("Esc");
      if (!event.repeat) this.stop();
      return;
    }
    if (
      isEditing(event.target) ||
      event.ctrlKey ||
      event.metaKey ||
      event.altKey
    )
      return;
    this.focused = true; // A delivered physical key is authoritative in embedded browsers too.
    if (code === "Space") {
      this.prevent(event); // Never scroll or activate a focused button, in any mode.
      this.held.add(code);
      this.key("Space");
      const ctx = this.context();
      if (ctx.mode === "HUMAN" && !ctx.owned && !ctx.pending) this.claim();
      else if (ctx.mode !== "HUMAN") this.hint("Hで手動へ切り替えてください");
      this.change();
      return;
    }
    if (["KeyH", "KeyP", "KeyL"].includes(code)) {
      this.prevent(event);
      if (!event.repeat)
        this.mode({ KeyH: "HUMAN", KeyP: "VLA", KeyL: "LLM" }[code]);
      return;
    }
    if (MOVEMENT_KEYS.has(code) && this.context().mode === "HUMAN") {
      this.prevent(event);
      this.held.add(code);
      if (code === "KeyC" || code === "KeyV") this.gripperKey = code;
      this.key(code.replace("Key", ""));
      this.change();
    }
  }
  up(event) {
    const code = keyCode(event);
    if (code === "Space" && (!isEditing(event.target) || this.held.has(code)))
      this.prevent(event);
    this.held.delete(code);
    if (code === "Space") this.change();
  }
  release() {
    this.held.clear();
    this.change(true);
  }
  blur() {
    this.focused = false;
    this.release();
  }
}
