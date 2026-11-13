import assert from "node:assert/strict";
import { mkdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { chromium, request } from "playwright";

const url = process.env.CAIRN_WEB_E2E_URL;
const tokenPath = process.env.CAIRN_WEB_E2E_TOKEN_FILE;
assert(url && tokenPath, "Use an isolated Cairn Server and supply its URL and token file.");
const token = readFileSync(tokenPath, "utf8").trim();
const browser = await chromium.launch({ channel: process.env.CAIRN_WEB_E2E_CHANNEL });
const api = await request.newContext({ baseURL: url, extraHTTPHeaders: { Authorization: "Bearer " + token } });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, colorScheme: "light" });
page.setDefaultTimeout(20000);
const errors = [];
page.on("pageerror", error => errors.push(error.name));
const images = process.env.CAIRN_WEB_E2E_SCREENSHOTS;
const prefix = "/api/runtime/";

async function shot(name) {
  if (!images) return;
  mkdirSync(images, { recursive: true });
  await page.screenshot({ path: join(images, name + ".png"), animations: "disabled" });
}
async function post(path, data) {
  const response = await api.post(prefix + path, { data });
  assert(response.ok(), "Server rejected " + path + " (" + response.status() + ")");
  return response.json();
}
async function read(path) {
  const response = await api.get(prefix + path);
  assert(response.ok(), "Could not read " + path);
  return response.json();
}
async function assertNoOverflow() {
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "Page exceeds viewport");
}
async function authenticate(path = url) {
  await page.goto(path);
  await page.getByLabel("Username").fill("cairn");
  await page.getByLabel("Password").fill(token);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.getByRole("status").filter({ hasText: "Server 已连接" }).waitFor();
}
async function send(text) {
  await page.getByRole("textbox", { name: "消息" }).fill(text);
  await page.getByRole("button", { name: "发送", exact: true }).click();
}
async function conversationLink(pattern) {
  await page.locator(".conversation-tree li").first().waitFor();
  const link = page.getByRole("link", { name: pattern });
  if (!(await link.count())) {
    const more = page.locator(".side-more");
    if (await more.count()) await more.click();
  }
  return link;
}

try {
  await authenticate();
  await assertNoOverflow();
  if (process.env.CAIRN_WEB_E2E_REAL_MODEL === "1") {
    const select = page.getByLabel("模型");
    const approved = await select.locator("option").evaluateAll(options => options.map(option => option.value).filter(Boolean));
    assert(approved.length, "No approved real model");
    await select.selectOption({ value: approved[0] });
    assert.equal(await select.inputValue(), approved[0]);
    await send("Reply with exactly CAIRN_WORKBENCH_OK. Do not call tools.");
    await page.locator(".wb-message.assistant").filter({ hasText: "CAIRN_WORKBENCH_OK" }).waitFor({ timeout: 90000 });
    await page.locator('.status-badge[data-status="completed"]').waitFor();
    const conversationId = new URL(page.url()).searchParams.get("conversation");
    const detail = await read("conversations/" + conversationId);
    assert.equal(detail.runs.length, 1);
    assert.equal(detail.runs[0].metadata.model_id, approved[0]);
    console.log("Real provider conversation and approved model selection passed.");
  } else {
    await shot("Cairn-workbench-home");
    const cliPrompt = "CLI browser takeover";
    const cli = (await read("conversations")).find(item => item.initial_prompt === cliPrompt && item.source === "cli");
    assert(cli, "The test fixture must start a real CLI conversation.");
    const shell = await api.get("/chat.html");
    assert(shell.ok());
    assert((await shell.text()).includes("正在加载 Cairn…"), "The chat shell must paint before JavaScript loads");

    // A deep link must return to the same conversation after login, without a reload.
    const fresh = await browser.newContext();
    try {
      const freshPage = await fresh.newPage();
      await freshPage.goto(new URL(`/chat.html?conversation=${cli.conversation_id}`, url).href);
      await freshPage.getByLabel("Username").fill("cairn");
      await freshPage.getByLabel("Password").fill(token);
      await freshPage.getByRole("button", { name: "Sign in" }).click();
      await freshPage.locator(".wb-chat").waitFor();
      assert.equal(new URL(freshPage.url()).pathname, "/chat.html");
      assert.equal(new URL(freshPage.url()).searchParams.get("conversation"), cli.conversation_id);
    } finally {
      await fresh.close();
    }

    let failHistory = true;
    await page.route("**/history", async route => {
      if (!failHistory) return route.continue();
      await route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ error: "internal_error" }) });
    });
    await (await conversationLink(new RegExp(cliPrompt))).click();
    await page.getByRole("alert").waitFor();
    assert.match(await page.getByRole("alert").innerText(), /Server 处理失败/);
    failHistory = false;
    await page.getByRole("button", { name: "重试" }).click();
    await page.locator(".wb-message.assistant").filter({ hasText: "离线共享对话已连接" }).first().waitFor();
    await page.unroute("**/history");

    await page.goto(new URL("/chat.html?conversation=conv-missing", url).href);
    await page.getByRole("alert").waitFor();
    assert.match(await page.getByRole("alert").innerText(), /对话不存在/);
    await (await conversationLink(new RegExp(cliPrompt))).click();
    await page.locator(".wb-message.assistant").filter({ hasText: "离线共享对话已连接" }).first().waitFor();

    await page.context().setOffline(true);
    await page.getByRole("link", { name: "工作台", exact: true }).click();
    await (await conversationLink(new RegExp(cliPrompt))).click();
    await page.getByRole("alert").waitFor();
    assert.match(await page.getByRole("alert").innerText(), /无法连接 Server/);
    await page.context().setOffline(false);
    await page.getByRole("button", { name: "重试" }).click();
    await page.locator(".wb-message.assistant").filter({ hasText: "离线共享对话已连接" }).first().waitFor();

    await page.getByRole("link", { name: "工作台", exact: true }).click();
    await send("Web-created conversation");
    await page.getByRole("heading", { name: "Web-created conversation" }).waitFor();
    const createdId = new URL(page.url()).searchParams.get("conversation");
    assert(createdId && createdId !== cli.conversation_id);
    await page.reload();
    await page.locator(".wb-message.user").filter({ hasText: "Web-created conversation" }).waitFor();
    await page.locator(".wb-message.assistant").filter({ hasText: "离线共享对话已连接" }).first().waitFor();
    await (await conversationLink(new RegExp(cliPrompt))).click();
    await page.locator(".wb-message.assistant").filter({ hasText: "离线共享对话已连接" }).first().waitFor();

    const legacyId = process.env.CAIRN_WEB_E2E_LEGACY_CONVERSATION;
    if (legacyId) {
      await (await conversationLink(/Legacy browser history/)).click();
      await page.locator(".wb-message.user").filter({ hasText: "Legacy browser history" }).waitFor();
      await page.locator(".wb-message.assistant").filter({ hasText: "离线共享对话已连接" }).first().waitFor();
      const legacy = await read("conversations/" + legacyId);
      assert.equal(legacy.runs[0].session_id, null);
      await send("Legacy follow-up");
      await page.locator(".wb-message.user").filter({ hasText: "Legacy follow-up" }).waitFor();
      await page.waitForFunction(
        async id => {
          const detail = await (await fetch("/api/runtime/conversations/" + id)).json();
          return detail.runs.length === 2 && detail.runs[1].status === "completed";
        },
        legacyId
      );
      const continuedLegacy = await read("conversations/" + legacyId);
      assert.equal(continuedLegacy.runs[1].session_id !== null, true);
      await page.locator(".wb-message.user").filter({ hasText: "Legacy browser history" }).waitFor();
      await (await conversationLink(new RegExp(cliPrompt))).click();
      await page.locator(".wb-message.assistant").filter({ hasText: "离线共享对话已连接" }).first().waitFor();
    }

    const lostId = process.env.CAIRN_WEB_E2E_MISSING_SESSION_CONVERSATION;
    if (lostId) {
      await (await conversationLink(/Missing session continuation/)).click();
      await page.locator(".wb-message.user").filter({ hasText: "Missing session continuation" }).waitFor();
      await send("Continue after session loss");
      await page.locator(".wb-message.user").filter({ hasText: "Continue after session loss" }).waitFor();
      await page.waitForFunction(
        async id => {
          const detail = await (await fetch("/api/runtime/conversations/" + id)).json();
          return detail.runs.length === 2 && detail.runs[1].status === "completed" && detail.runs[1].metadata.history_degraded;
        },
        lostId
      );
      await page.getByRole("status").filter({ hasText: "旧 Session 记录不可用" }).waitFor();
      const lost = await read("conversations/" + lostId);
      assert.equal(lost.runs[0].session_id, "00000000-0000-4000-8000-000000000000");
      assert(lost.runs[1].session_id !== "00000000-0000-4000-8000-000000000000");
      assert.equal(lost.runs[1].metadata.history_degraded, "session_missing");
      await page.locator(".wb-message.assistant").filter({ hasText: "离线共享对话已连接" }).last().waitFor();
      await (await conversationLink(new RegExp(cliPrompt))).click();
      await page.locator(".wb-message.assistant").filter({ hasText: "离线共享对话已连接" }).first().waitFor();
    }

    let lostReply = false;
    let messagePosts = 0;
    await page.route("**/messages", async route => {
      if (route.request().method() !== "POST") return route.continue();
      messagePosts += 1;
      if (lostReply) return route.continue();
      lostReply = true;
      const response = await route.fetch();
      assert(response.ok());
      await route.abort();
    });
    await send("Web continues the CLI conversation");
    const retry = page.getByRole("button", { name: "用原编号重试" });
    await retry.waitFor();
    await page.getByRole("alert").waitFor();
    // A pending retry must not lock new input.
    await page.getByRole("textbox", { name: "消息" }).fill("New input while retry is pending");
    assert(await page.locator("button.send-button").isEnabled());
    await page.getByRole("textbox", { name: "消息" }).fill("");
    await retry.click();
    await retry.waitFor({ state: "detached" });
    assert.equal(messagePosts, 1, "A pending retry must read the existing Run instead of re-posting");
    await page.unroute("**/messages");
    await page.locator('.status-badge[data-status="completed"]').waitFor();
    const continued = await read("conversations/" + cli.conversation_id);
    assert.equal(continued.runs.length, 2);
    assert.equal(continued.runs[1].source, "web");

    // A busy response is queued and retried automatically, not left as a stuck request.
    let busy = true;
    await page.route("**/messages", async route => {
      if (!busy) return route.continue();
      busy = false;
      await route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ error: "conversation_busy" }) });
    });
    const beforeQueuedAssistant = await page.locator(".wb-message.assistant").count();
    await send("Queue while another run is active");
    await page.locator(".queued-tray").waitFor();
    await page.waitForFunction(
      count => document.querySelectorAll(".wb-message.assistant").length > count,
      beforeQueuedAssistant,
      { timeout: 15000 }
    );
    await page.locator(".queued-tray").waitFor({ state: "detached" });
    await page.unroute("**/messages");
    assert.equal((await read("conversations/" + cli.conversation_id)).runs.length, 3);
    await shot("Cairn-workbench-chat");
    const chatUrl = page.url();
    assert((await read("tasks")).every(item => item.mode === "development"));

    const wx = await post("wechat/conversations", { account_id: "browser-fixture-bot", sender_id: "browser-fixture-sender", prompt: "微信工作台验收" });
    await post("conversations/" + wx.conversation_id + "/messages", { prompt: "微信工作台验收", request_id: "browser-wx-1", source: "wechat" });
    await page.getByRole("link", { name: /微信工作台验收/ }).click();
    await page.getByRole("heading", { name: "微信工作台验收" }).waitFor();
    await post("conversations/" + wx.conversation_id + "/messages", { prompt: "微信新增运行，无需刷新", request_id: "browser-wx-2", source: "wechat" });
    await page.locator(".wb-message.user").filter({ hasText: "微信新增运行，无需刷新" }).waitFor();

    await page.getByRole("link", { name: "开发任务", exact: true }).click();
    await page.getByRole("button", { name: "新建任务" }).click();
    assert.equal(await page.getByLabel("权限模式").inputValue(), "development");
    await send("Build the workbench acceptance artifact");
    await page.locator('.status-badge[data-status="completed"]').waitFor({ timeout: 30000 });
    await page.getByRole("heading", { name: "工作树" }).waitFor();
    await shot("Cairn-workbench-task");
    const taskUrl = page.url();
    await page.getByRole("button", { name: "产物" }).click();
    const drawer = page.getByRole("dialog", { name: "任务产物" });
    await drawer.waitFor();
    await drawer.getByText("离线开发链路完成", { exact: false }).waitFor();
    await shot("Cairn-workbench-artifacts");
    await page.keyboard.press("Escape");
    await drawer.waitFor({ state: "detached" });
    await page.getByRole("button", { name: "查看流程图" }).click();
    await page.locator(".react-flow__node").first().waitFor();
    await page.locator(".react-flow__node").first().click();
    await page.getByText(/已记录事件 #/).waitFor();
    const node = await page.locator(".react-flow__node").first().boundingBox();
    assert(node);
    await page.mouse.move(node.x + node.width / 2, node.y + node.height / 2);
    await page.mouse.down();
    await page.mouse.move(node.x + node.width / 2 + 30, node.y + node.height / 2 + 45, { steps: 8 });
    await page.mouse.up();
    const undo = page.getByRole("button", { name: "撤销布局" });
    await undo.click();
    assert(await page.getByRole("button", { name: "重做布局" }).isEnabled());
    await shot("Cairn-workbench-workflow");
    const workflowUrl = page.url();
    assert(await page.locator(".react-flow__handle.connectable").count() === 0);
    await page.goto(taskUrl);
    await page.getByLabel("跟进说明").fill("Verify the existing artifact");
    await page.getByRole("button", { name: "发送跟进" }).click();
    await page.locator(".wb-detail-grid section").filter({ has: page.getByRole("heading", { name: "最近执行" }) }).locator("dd").first().filter({ hasText: /^2$/ }).waitFor();
    await page.locator('.status-badge[data-status="completed"]').first().waitFor();

    let approvalUrl;
    const recoveryId = process.env.CAIRN_WEB_E2E_RECOVERY_TASK;
    if (recoveryId) {
      await page.getByRole("link", { name: "待我确认", exact: true }).click();
      await page.getByRole("link", { name: /Interrupted browser recovery/ }).click();
      const decision = page.getByRole("button", { name: "记录核对决定" });
      await decision.waitFor();
      approvalUrl = page.url();
      const recoveryPath = prefix + "tasks/" + recoveryId + "/reconcile";
      assert.equal((await page.request.post(new URL(recoveryPath, url).href, { data: {}, headers: { Origin: "http://127.0.0.1:1" } })).status(), 403);
      const anonymous = await request.newContext({ baseURL: url });
      try {
        assert.equal((await anonymous.post(recoveryPath, { data: {} })).status(), 401);
      } finally { await anonymous.dispose(); }
      await page.context().clearCookies();
      await authenticate(approvalUrl);
      await decision.waitFor();
      assert.equal(page.url(), approvalUrl);
      assert(await decision.isDisabled());
      await page.getByLabel("核对说明").fill("Reviewed the existing test artifact.");
      await page.getByRole("checkbox", { name: "我已核对实际影响" }).check();
      await shot("Cairn-workbench-approval");
      await decision.click();
      await page.getByRole("heading", { name: "工作树" }).waitFor();
      assert.equal((await read("tasks/" + recoveryId)).status, "paused");
    }

    await page.getByRole("link", { name: "运行", exact: true }).click();
    await page.getByRole("link", { name: /微信新增运行，无需刷新/ }).click();
    await page.getByRole("heading", { name: "执行时间线" }).waitFor();
    await shot("Cairn-workbench-run");
    const runUrl = page.url();
    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByRole("link", { name: "工作台", exact: true }).click();
    // The toggle cycles light -> dark -> system; pin a known starting point.
    await page.evaluate(() => localStorage.setItem("cairn-theme", "light"));
    await page.reload();
    await page.getByRole("button", { name: "切换主题" }).click();
    await page.waitForSelector('html[data-theme="dark"]', { state: "attached" });
    await assertNoOverflow();
    await shot("Cairn-workbench-mobile");
    const toggle = page.getByRole("button", { name: "切换会话侧栏" });
    await toggle.click();
    assert.equal(await toggle.getAttribute("aria-expanded"), "true");
    await page.keyboard.press("Escape");
    assert.equal(await toggle.getAttribute("aria-expanded"), "false");
    for (const [name, path, heading] of [
      ["chat", chatUrl, "CLI browser takeover"],
      ["task", taskUrl, "工作树"],
      ["workflow", workflowUrl, "流程"],
      ["run", runUrl, "执行时间线"],
      ...(approvalUrl ? [["approval", approvalUrl, "恢复核对"]] : []),
    ]) {
      await page.goto(path);
      await page.getByRole("heading", { name: heading, exact: true }).waitFor();
      await assertNoOverflow();
      await shot("Cairn-workbench-mobile-" + name);
      if (name === "workflow") {
        const first = page.locator(".react-flow__node").first();
        await first.click();
        await page.getByText(/已记录事件 #/).waitFor();
      }
      if (name === "task") {
        await page.getByRole("button", { name: "产物" }).click();
        const mobileDrawer = page.getByRole("dialog", { name: "任务产物" });
        await mobileDrawer.waitFor();
        const box = await mobileDrawer.boundingBox();
        assert(box && box.x >= 0 && box.x + box.width <= 390);
        await shot("Cairn-workbench-mobile-artifacts");
        await page.keyboard.press("Escape");
      }
    }
    await page.getByRole("link", { name: "设置", exact: true }).click();
    await page.getByRole("heading", { name: "微信连接" }).waitFor();
    await assertNoOverflow();

    // A plain-HTTP origin (no secure context) does not expose
    // crypto.randomUUID; sends must still produce their request.
    await page.addInitScript(() => {
      Object.defineProperty(Crypto.prototype, "randomUUID", { get: () => undefined });
    });
    let plainHttpPost = false;
    await page.route("**/api/runtime/conversations", async route => {
      if (route.request().method() !== "POST") return route.continue();
      plainHttpPost = true;
      await route.abort();
    });
    const errorsBeforePlainHttp = errors.length;
    await page.goto(new URL("/chat.html", url).href);
    await send("Send without crypto.randomUUID");
    await page.waitForTimeout(750);
    assert(plainHttpPost, "A plain-HTTP send must still post a conversation");
    assert.equal(errors.length, errorsBeforePlainHttp, "A plain-HTTP send must not raise a page error");
    await page.unroute("**/api/runtime/conversations");
    console.log("Plain-HTTP request identity passed.");

    const longTitleId = process.env.CAIRN_WEB_E2E_LONG_TITLE_CONVERSATION;
    if (longTitleId) {
      await page.setViewportSize({ width: 1024, height: 900 });
      await page.goto(new URL(`/chat.html?conversation=${longTitleId}`, url).href);
      await page.locator(".wb-chat-header h1").filter({ hasText: "Long conversation title" }).waitFor();
      const headerTitle = page.locator(".wb-chat-header h1");
      const headerText = await headerTitle.innerText();
      const fullTitle = await headerTitle.getAttribute("title");
      assert(fullTitle.includes("超长标题"), "The full prompt must stay available on hover");
      assert(headerText.length <= 65, "The displayed title must be shortened");
      const sidebarTitle = page.locator(`a[href*="${longTitleId}"] span`).first();
      await sidebarTitle.waitFor();
      assert.equal(await sidebarTitle.innerText(), headerText, "Header and sidebar must show the same title");
      const titleLayout = await page.evaluate(() => {
        const main = document.querySelector(".wb-main");
        const header = document.querySelector(".wb-chat-header");
        const title = header.querySelector("h1");
        const mainBox = main.getBoundingClientRect();
        const buttons = [...header.querySelectorAll("button")];
        return {
          mainOverflow: main.scrollWidth - main.clientWidth,
          headerOverflow: Math.round(header.getBoundingClientRect().right - mainBox.right),
          titleOverflow: title.scrollWidth - title.clientWidth,
          textOverflow: getComputedStyle(title).textOverflow,
          buttonsInside: buttons.every(button => button.getBoundingClientRect().right <= mainBox.right + 1),
        };
      });
      assert(titleLayout.titleOverflow > 0, "The long title fixture must actually overflow");
      assert.equal(titleLayout.textOverflow, "ellipsis");
      assert(titleLayout.mainOverflow <= 1, "A long title must not widen the main pane");
      assert(titleLayout.headerOverflow <= 0, "The chat header must stay inside the main pane");
      assert(titleLayout.buttonsInside, "Header actions must stay inside the main pane");
      console.log("Long conversation title layout passed.");
    }

    const renameId = process.env.CAIRN_WEB_E2E_RENAME_CONVERSATION;
    if (renameId) {
      await page.setViewportSize({ width: 1280, height: 900 });
      await page.goto(new URL("/chat.html", url).href);
      await page.getByRole("status").filter({ hasText: "Server 已连接" }).waitFor();
      const tree = page.locator(".conversation-tree");
      await tree.locator("li").first().waitFor();
      const listed = await read("conversations");
      const expectedIds = listed.map(item => item.conversation_id);
      const domIds = await tree.locator("li a").evaluateAll(nodes => nodes.map(node => {
        const href = node.getAttribute("href") ?? "";
        return new URL(href, location.origin).searchParams.get("conversation") ?? "";
      }));
      assert.deepEqual(domIds, expectedIds.slice(0, 12), "The tree shows the newest conversations first");
      const more = tree.locator(".side-more");
      if (expectedIds.length > 12) {
        assert.match(await more.innerText(), /^展开其余 \d+ 条$/, "The tree offers the remaining conversations");
        await more.click();
        await page.waitForFunction(total => document.querySelectorAll(".conversation-tree li").length === total, expectedIds.length);
        const expanded = await tree.locator("li a").evaluateAll(nodes => nodes.map(node => {
          const href = node.getAttribute("href") ?? "";
          return new URL(href, location.origin).searchParams.get("conversation") ?? "";
        }));
        assert.deepEqual(expanded, expectedIds, "Expanding reveals every conversation in activity order");
      } else {
        assert.equal(await more.count(), 0, "No expand control is needed for a short tree");
      }

      const row = tree.locator("li").filter({ has: page.locator(`a[href*="${renameId}"]`) });
      assert.equal(await row.count(), 1, "The rename fixture must be visible");
      await row.hover();
      await row.locator(".wb-session-more").click();
      const menu = page.locator(".wb-session-menu");
      await menu.waitFor();
      await menu.getByRole("menuitem", { name: "重命名" }).click();
      const renameInput = tree.getByLabel("重命名对话");
      await renameInput.fill("Browser renamed conversation");
      await renameInput.press("Enter");
      await page.waitForFunction(([id, text]) => {
        const link = document.querySelector(`a[href*="${id}"]`);
        return link?.querySelector("span")?.textContent === text;
      }, [renameId, "Browser renamed conversation"]);
      const renamed = await read("conversations/" + renameId);
      assert.equal(renamed.conversation.title, "Browser renamed conversation");
      await row.locator("a").click();
      await page.locator(".wb-chat-header h1").filter({ hasText: "Browser renamed conversation" }).waitFor();

      page.once("dialog", dialog => dialog.accept());
      await row.hover();
      await row.locator(".wb-session-more").click();
      await page.locator(".wb-session-menu").getByRole("menuitem", { name: "删除对话" }).click();
      await page.waitForFunction(id => !document.querySelector(`a[href*="${id}"]`), renameId);
      const remaining = await read("conversations");
      assert(!remaining.some(item => item.conversation_id === renameId), "Deleting removes the conversation");
      console.log("Conversation tree ordering, rename and delete passed.");
    }
    console.log("Workbench browser E2E passed.");
  }
  assert.deepEqual(errors, []);
} finally {
  await api.dispose();
  await browser.close();
}
