import {
  Container,
  Markdown,
  SelectList,
  Text,
  type Component,
  type TUI,
  type SelectItem,
} from "@earendil-works/pi-tui";
import type { WireMessage } from "./bridge.js";

const plain = (s: string) => s;
const cyan = (s: string) => `\x1b[36m${s}\x1b[39m`;
export const selectionTheme = {
  selectedPrefix: cyan,
  selectedText: cyan,
  description: plain,
  scrollInfo: plain,
  noMatch: plain,
};
const markdownTheme = {
  heading: cyan,
  link: plain,
  linkUrl: plain,
  code: cyan,
  codeBlock: plain,
  codeBlockBorder: plain,
  quote: plain,
  quoteBorder: plain,
  hr: plain,
  listBullet: plain,
  bold: plain,
  italic: plain,
  strikethrough: plain,
  underline: plain,
};
// Untrusted model/tool/config strings cannot inject terminal control sequences.
export const clean = (value: unknown) =>
  String(value ?? "").replace(/[\x00-\x08\x0b-\x1f\x7f-\x9f]/g, "");
export const markdown = (text: string) =>
  new Markdown(clean(text), 1, 0, markdownTheme);

export function choose(
  tui: TUI,
  title: string,
  items: SelectItem[],
  onChoice: (value: string | null) => void,
): () => void {
  const list = new SelectList(
    items.map((i) => ({
      ...i,
      label: clean(i.label),
      description: clean(i.description),
    })),
    8,
    selectionTheme,
  );
  const box = new (class extends Container {
    handleInput(data: string) {
      list.handleInput(data);
    }
  })();
  box.addChild(new Text(clean(title), 1, 1));
  box.addChild(list);
  box.addChild(new Text("↑↓ select · Enter confirm · Esc cancel", 1, 1));
  const overlay = tui.showOverlay(box, { width: "85%", maxHeight: "85%" });
  const done = (value: string | null) => {
    overlay.hide();
    onChoice(value);
    tui.requestRender();
  };
  list.onSelect = (item) => done(item.value);
  list.onCancel = () => done(null);
  return () => {
    overlay.hide();
    tui.requestRender();
  };
}

class ToolCard implements Component {
  status = "running";
  output = "";
  expanded = false;
  constructor(
    readonly tool: string,
    readonly summary: string,
  ) {}
  invalidate() {}
  render(width: number) {
    return new Text(
      `[${this.status}] ${clean(this.tool)} ${clean(this.summary)}${this.expanded ? "\n" + clean(this.output).slice(0, 4000) : ""}`,
      1,
      0,
    ).render(width);
  }
}

/** Append ordered runtime events; one Markdown component per stream attempt. */
export class Transcript extends Container {
  private stream: Markdown | null = null;
  private streamText = "";
  private streamStep: number | null = null;
  private streamCompleted = false;
  private tool: ToolCard | null = null;
  private cards: ToolCard[] = [];
  begin(text: string) {
    this.stream = null;
    this.streamText = "";
    this.streamStep = null;
    this.streamCompleted = false;
    this.addChild(new Text(`You> ${clean(text)}`, 1, 1));
  }
  notice(text: string) {
    this.addChild(new Text(clean(text), 1, 0));
  }
  toggleTools() {
    for (const card of this.cards) card.expanded = !card.expanded;
  }
  observe(m: WireMessage) {
    const d = m.data;
    switch (m.event) {
      case "model_stream_start":
        this.stream = markdown("");
        this.streamText = "";
        this.streamStep = d.step;
        this.streamCompleted = false;
        this.addChild(this.stream);
        break;
      case "model_text_delta":
        if (this.stream) {
          this.streamText += clean(d.text);
          this.stream.setText(this.streamText);
        }
        break;
      case "model_stream_end":
        this.streamCompleted = d.status === "completed";
        if (!this.streamCompleted)
          this.notice(
            `[${d.status === "abandoned" ? "abandoned attempt" : "incomplete output"}]`,
          );
        break;
      case "agent_finish":
        if (
          !(
            this.streamCompleted &&
            this.streamStep === d.step &&
            this.streamText === clean(d.content)
          )
        )
          this.addChild(markdown(clean(d.content)));
        break;
      case "tool_call":
        this.tool = new ToolCard(clean(d.tool), clean(d.summary));
        this.cards.push(this.tool);
        this.addChild(this.tool);
        break;
      case "tool_result":
        if (this.tool) {
          this.tool.status = d.exit_code === 0 ? "completed" : "failed";
          this.tool.output =
            clean(d.output) + (d.truncated ? "\n[truncated]" : "");
        }
        break;
      case "tool_error":
      case "tool_denied":
        if (this.tool)
          this.tool.status = m.event === "tool_denied" ? "denied" : "failed";
        break;
      case "turn_settled":
        if (this.tool?.status === "running") this.tool.status = "incomplete";
        this.notice(`[Turn ${clean(d.outcome)}]`);
        break;
    }
  }
}
