import {
  Editor,
  ProcessTerminal,
  Text,
  TuiMainScreen,
  matchesKey,
} from "@earendil-works/pi-tui";
import { Bridge, type WireMessage } from "./bridge.js";
import { choose, clean, selectionTheme, Transcript } from "./views.js";

if (!process.stdin.isTTY || !process.stdout.isTTY) {
  console.error("cairn tui requires an interactive terminal.");
  process.exit(1);
}
const tui = new TuiMainScreen(new ProcessTerminal());
const transcript = new Transcript();
const status = new Text("Connecting to Cairn…", 1, 1);
const editor = new Editor(tui, {
  borderColor: (s) => s,
  selectList: selectionTheme,
});
tui.addChild(transcript);
tui.addChild(status);
tui.addChild(editor);
tui.setFocus(editor);
let active: number | null = null;
let turn: string | null = null;
let initialized = false;
let closing = false;
let providers: { name: string; models: string[] }[] = [];
let provider = "";
let model = "";
let closeOverlay: (() => void) | null = null;
let approvalId: string | null = null;
const bridge = new Bridge(process.env.CAIRN_TUI_PYTHON || "python3");
function send(op: string, fields = {}) {
  try {
    return bridge.send(op, fields);
  } catch {
    transcript.notice("Backend unavailable or input exceeds 64 KiB.");
    tui.requestRender();
    return null;
  }
}
function operation(op: string, fields = {}) {
  active = send(op, fields);
  turn = null;
  status.setText("Working… Ctrl+C cancels; /quit exits after cleanup.");
}
function shutdown() {
  if (closing) return;
  closing = true;
  closeOverlay?.();
  closeOverlay = null;
  status.setText("Closing — waiting for runtime cleanup…");
  send("shutdown");
  tui.requestRender();
}
bridge.onMessage = (m: WireMessage) => {
  switch (m.type) {
    case "hello":
      transcript.notice(m.banner);
      operation("init");
      break;
    case "catalog":
      providers = m.providers;
      break;
    case "ready":
      initialized = true;
      providers = m.providers;
      provider = m.provider;
      model = m.model;
      status.setText(
        `${clean(m.provider)} / ${clean(m.model)} · /model /provider /tools /quit · Enter send · Shift+Enter newline`,
      );
      break;
    case "approval": {
      approvalId = m.approval_id;
      const title =
        m.kind === "provider"
          ? `Trust provider ${m.provider}?\nEndpoint: ${m.endpoint}\nCredential variable: ${m.credential_variable}\nModels: ${JSON.stringify(m.models)}\n${m.notice}`
          : `${m.capability} permission: ${m.tool}\n${m.summary}\n${m.justification}`;
      const items = [
        { value: "deny", label: "Deny" },
        { value: "allow_once", label: "Allow Once" },
      ];
      if (m.kind === "permission")
        items.push({ value: "allow_session", label: "Allow Session" });
      closeOverlay?.();
      closeOverlay = choose(tui, title, items, (value) => {
        closeOverlay = null;
        send("approval", {
          approval_id: m.approval_id,
          request_id: m.request_id,
          turn_id: m.turn_id,
          choice: value || "deny",
        });
      });
      break;
    }
    case "approval_closed":
      if (approvalId === m.approval_id) {
        closeOverlay?.();
        closeOverlay = null;
        approvalId = null;
      }
      break;
    case "event":
      turn = m.turn_id!;
      transcript.observe(m);
      break;
    case "idle":
      if (active === m.request_id) {
        active = null;
        turn = null;
        if (!closing)
          status.setText(
            initialized
              ? `${clean(provider)} / ${clean(model)} · /model /provider /tools /quit · Ctrl+C cancel`
              : "Provider unavailable. /provider retries initialization; /quit exits.",
          );
      }
      break;
    case "cancelled":
      transcript.notice("[Cancelled; runtime cleanup settled]");
      break;
    case "error":
      transcript.notice(`Backend: ${clean(m.code)}. Output may be incomplete.`);
      break;
  }
  tui.requestRender();
};
bridge.onExit = (ok) => {
  closeOverlay?.();
  transcript.notice(
    ok ? "Cairn closed." : "Backend exited unexpectedly. Output is incomplete.",
  );
  tui.renderNow();
  tui.stop();
  process.exitCode = ok ? 0 : 1;
};
editor.onSubmit = (text) => {
  if (closing) return;
  if (text.trim() === "/quit" || text.trim() === "/exit") {
    shutdown();
    return;
  }
  if (active !== null) {
    transcript.notice(
      "Busy — cancel the current operation before switching or submitting.",
    );
    tui.requestRender();
    return;
  }
  if (!text.trim()) return;
  editor.setText("");
  editor.addToHistory(text);
  if (text.trim() === "/tools") {
    transcript.toggleTools();
    tui.requestRender();
    return;
  }
  if (!initialized && (text.trim() !== "/provider" || providers.length === 0)) {
    operation("init");
    return;
  }
  if (["/model", "/provider"].includes(text.trim())) {
    const op = text.trim() === "/model" ? "model" : "provider";
    const names =
      op === "model"
        ? providers.find((p) => p.name === provider)!.models
        : providers.map((p) => p.name);
    closeOverlay = choose(
      tui,
      `Select ${op}`,
      names.map((name) => ({ value: name, label: name })),
      (name) => {
        closeOverlay = null;
        if (name !== null) operation(op, { name });
      },
    );
    return;
  }
  if (text.startsWith("/")) {
    transcript.notice("Commands: /provider /model /tools /quit");
    tui.requestRender();
    return;
  }
  transcript.begin(text);
  operation("prompt", { text });
};
tui.addInputListener((data) => {
  if (matchesKey(data, "ctrl+c")) {
    if (active !== null) {
      send("cancel", { request_id: active, turn_id: turn });
      status.setText("Cancelling — waiting for cleanup…");
    } else {
      closeOverlay?.();
      closeOverlay = null;
      editor.setText("");
    }
    tui.requestRender();
    return { consume: true };
  }
  if (matchesKey(data, "ctrl+d")) {
    shutdown();
    return { consume: true };
  }
});
process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);
tui.start();
