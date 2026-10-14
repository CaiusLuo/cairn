import assert from "node:assert/strict";
import test from "node:test";
import { Transcript, clean } from "./views.js";

test("ordered stream, tools and final output render once with bounded expansion", () => {
  const view = new Transcript();
  view.begin("hello");
  const emit = (event: string, data: Record<string, unknown>) =>
    view.observe({ v: 1, type: "event", event, data });
  emit("model_stream_start", { step: 1 });
  emit("model_reasoning_delta", { text: "consider the task" });
  emit("model_text_delta", { text: "checking" });
  emit("model_stream_end", { status: "completed" });
  emit("tool_call", { tool: "bash", summary: "pwd" });
  emit("tool_result", { exit_code: 0, output: "detail " + "z".repeat(5000) });
  emit("model_stream_start", { step: 2 });
  emit("model_text_delta", { text: "final answer" });
  emit("model_stream_end", { status: "completed" });
  emit("agent_finish", { step: 2, content: "final answer" });
  emit("turn_settled", { outcome: "completed" });
  let text = view.render(80).join("\n");
  assert.equal(text.split("final answer").length - 1, 1);
  assert.ok(text.includes("Thinking (provisional)"));
  assert.ok(text.indexOf("consider the task") < text.indexOf("checking"));
  assert.ok(text.indexOf("checking") < text.indexOf("[completed] bash pwd"));
  assert.ok(
    text.indexOf("[completed] bash pwd") < text.indexOf("final answer"),
  );
  assert.ok(!text.includes("detail"));
  view.toggleTools();
  text = view.render(80).join("\n");
  assert.ok(text.includes("detail"));
  assert.ok((text.match(/z/g) || []).length <= 4000);
  assert.equal(clean("\x1b]52;clipboard\x07"), "]52;clipboard");
});

test("abandoned and cancelled output stays visibly incomplete", () => {
  const view = new Transcript();
  view.observe({
    v: 1,
    type: "event",
    event: "model_stream_start",
    data: { step: 1 },
  });
  view.observe({
    v: 1,
    type: "event",
    event: "model_reasoning_delta",
    data: { text: "partial thought" },
  });
  view.observe({
    v: 1,
    type: "event",
    event: "model_stream_end",
    data: { status: "abandoned" },
  });
  view.observe({
    v: 1,
    type: "event",
    event: "turn_settled",
    data: { outcome: "cancelled" },
  });
  const text = view.render(80).join("\n");
  assert.ok(
    text.includes("partial") &&
      text.includes("abandoned attempt") &&
      text.includes("cancelled"),
  );
});
