import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { StringDecoder } from "node:string_decoder";

export interface WireMessage {
  v: 1;
  type: string;
  request_id?: number;
  turn_id?: string | null;
  [key: string]: any;
}

/** One local Python child. Transport writes never imply operation completion. */
export class Bridge {
  private child: ChildProcessWithoutNullStreams;
  private nextId = 0;
  private buffer = "";
  private decoder = new StringDecoder("utf8");
  private failed = false;
  private hello = false;
  private pending = new Map<number, string>();
  private turn: string | null = null;
  private sequence = 0;
  onMessage: (message: WireMessage) => void = () => {};
  onExit: (ok: boolean) => void = () => {};

  constructor(python: string, args = ["-m", "cairn.terminal.bridge"]) {
    this.child = spawn(python, args, {
      stdio: "pipe",
      detached: process.platform !== "win32",
    });
    // Provider stderr can contain credentials: drain it, never display it.
    this.child.stderr.on("data", () => {});
    this.child.stdout.on("data", (chunk: Buffer) =>
      this.receive(this.decoder.write(chunk)),
    );
    this.child.stdin.on("error", () => this.fail());
    this.child.on("error", () => this.fail());
    this.child.on("close", (code) =>
      this.onExit(code === 0 && !this.failed && this.saidBye),
    );
  }
  private saidBye = false;
  send(op: string, fields: Record<string, unknown> = {}): number {
    const id = ++this.nextId;
    const line = JSON.stringify({ v: 1, id, op, ...fields }) + "\n";
    if (Buffer.byteLength(line) > 65536)
      throw new Error("Input exceeds 64 KiB.");
    if (this.failed || !this.child.stdin.writable)
      throw new Error("Backend unavailable.");
    this.pending.set(id, op);
    this.child.stdin.write(line);
    return id;
  }
  private fail() {
    if (this.failed) return;
    this.failed = true;
    this.onMessage({ v: 1, type: "error", code: "backend_transport_failed" });
    // EOF asks Python to settle its owned turn. Never force-kill cleanup.
    this.child.stdin.end();
  }
  private receive(text: string) {
    if (this.failed) return;
    this.buffer += text;
    if (Buffer.byteLength(this.buffer) > 2 * 1024 * 1024) return this.fail();
    let end: number;
    while ((end = this.buffer.indexOf("\n")) >= 0) {
      const line = this.buffer.slice(0, end);
      this.buffer = this.buffer.slice(end + 1);
      try {
        const m = JSON.parse(line) as WireMessage;
        if (
          !m ||
          m.v !== 1 ||
          ![
            "hello",
            "catalog",
            "ready",
            "event",
            "approval",
            "approval_closed",
            "ack",
            "idle",
            "cancelled",
            "error",
            "bye",
          ].includes(m.type)
        )
          throw Error();
        if (
          ["ready", "event", "approval", "ack", "idle", "cancelled"].includes(
            m.type,
          ) &&
          !Number.isSafeInteger(m.request_id)
        )
          throw Error();
        if (m.type === "catalog" || m.type === "ready") {
          if (
            !Array.isArray(m.providers) ||
            m.providers.some(
              (p: any) =>
                typeof p?.name !== "string" ||
                !Array.isArray(p.models) ||
                p.models.some((s: unknown) => typeof s !== "string"),
            )
          )
            throw Error();
          if (
            m.type === "ready" &&
            (typeof m.provider !== "string" || typeof m.model !== "string")
          )
            throw Error();
        }
        if (m.type === "hello") {
          if (this.hello || typeof m.banner !== "string") throw Error();
          this.hello = true;
        } else if (!this.hello) throw Error();
        if (m.request_id != null && !this.pending.has(m.request_id))
          throw Error();
        if (m.type === "event") {
          if (
            typeof m.turn_id !== "string" ||
            !Number.isSafeInteger(m.sequence) ||
            m.sequence <= this.sequence ||
            !m.data ||
            typeof m.event !== "string"
          )
            throw Error();
          if (m.event === "turn_started") this.turn = m.turn_id;
          if (m.turn_id !== this.turn) throw Error();
          this.sequence = m.sequence;
        }
        if (
          m.type === "approval" &&
          (typeof m.approval_id !== "string" ||
            !["provider", "permission"].includes(m.kind))
        )
          throw Error();
        if (m.type === "idle" || m.type === "ack")
          this.pending.delete(m.request_id!);
        if (
          m.type === "error" &&
          m.request_id != null &&
          (m.code === "busy" ||
            ["cancel", "approval"].includes(
              this.pending.get(m.request_id) || "",
            ))
        )
          this.pending.delete(m.request_id);
        if (m.type === "idle") this.turn = null;
        if (m.type === "bye") this.saidBye = true;
        this.onMessage(m);
      } catch {
        this.fail();
        return;
      }
    }
  }
}
