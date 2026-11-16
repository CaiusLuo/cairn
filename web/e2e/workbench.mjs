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
    await page.getByRole("img", { name: "CLI" }).first().waitFor();
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
    await page.getByRole("img", { name: "Web" }).first().waitFor();
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
    await page.getByRole("img", { name: "微信" }).first().waitFor();
    if (images) await page.locator("#workbench-sidebar").screenshot({ path: join(images, "Cairn-workbench-sessions.png"), animations: "disabled" });
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
    // Removed graph view: old bookmarked URLs still reach the task.
    await page.goto(taskUrl + "&view=workflow");
    await page.getByRole("heading", { name: "工作树" }).waitFor();
    await page.goto(taskUrl);
    await page.getByLabel("跟进说明").fill("Verify the existing artifact");
    await page.getByRole("button", { name: "发送跟进" }).click();
    await page.locator(".wb-detail-grid section").filter({ has: page.getByRole("heading", { name: "最近执行" }) }).locator("dd").first().filter({ hasText: /^2$/ }).waitFor();
    await page.locator('.status-badge[data-status="completed"]').first().waitFor();

    await page.getByRole("link", { name: "知识", exact: true }).click();
    await page.getByRole("heading", { name: "知识", exact: true }).waitFor();
    const clipText = "剪贴验收原文\n\n- 第一行保留\n- **Markdown** 原样保存";
    await page.getByLabel("片段原文").fill(clipText);
    await page.getByLabel("片段标签").fill("验收, 剪贴");
    await page.getByRole("button", { name: "保存片段" }).click();
    await page.getByRole("heading", { name: "剪贴验收原文", exact: true }).waitFor();
    const knowledgeUrl = page.url();
    const storedClips = (await (await api.get("/api/knowledge/clips?query=" + encodeURIComponent("剪贴验收原文"))).json()).clips;
    assert.equal(storedClips.length, 1);
    assert.equal(storedClips[0].content, clipText);
    assert.deepEqual(storedClips[0].tags, ["验收", "剪贴"]);
    assert.equal(storedClips[0].content_hash.length, 64);

    await page.getByLabel("搜索知识片段").fill("Markdown");
    await page.getByRole("heading", { name: "剪贴验收原文", exact: true }).waitFor();
    await page.getByRole("button", { name: /剪贴验收原文/ }).click();
    const clipDialog = page.getByRole("dialog", { name: "知识片段" });
    await clipDialog.waitFor();
    await clipDialog.getByText("SHA-256", { exact: false }).waitFor();
    await clipDialog.getByRole("button", { name: "预览 Markdown" }).click();
    await clipDialog.locator("strong").filter({ hasText: "Markdown" }).waitFor();
    await clipDialog.getByRole("button", { name: "显示原文" }).click();
    await clipDialog.getByRole("button", { name: "编辑内容" }).click();
    await clipDialog.getByLabel("编辑片段内容").fill(clipText + "\n追加行");
    await clipDialog.getByRole("button", { name: "保存修改" }).click();
    await clipDialog.getByText("追加行", { exact: false }).waitFor();
    const editedClips = (await (await api.get("/api/knowledge/clips?query=" + encodeURIComponent("追加行"))).json()).clips;
    assert.equal(editedClips[0].content, clipText + "\n追加行");
    await clipDialog.getByRole("button", { name: "归档片段" }).click();
    await clipDialog.getByText("已归档", { exact: true }).first().waitFor();
    await page.keyboard.press("Escape");
    await clipDialog.waitFor({ state: "detached" });
    await page.getByRole("checkbox", { name: "显示已归档" }).check();
    await page.getByRole("heading", { name: "剪贴验收原文", exact: true }).waitFor();
    await page.getByRole("button", { name: "恢复", exact: true }).click();
    await page.getByRole("heading", { name: "剪贴验收原文", exact: true }).waitFor();
    assert.equal((await (await api.get("/api/knowledge/clips?query=" + encodeURIComponent("追加行"))).json()).clips[0].archived_at, null);
    await shot("Cairn-workbench-knowledge");

    // The Server can commit while its response is lost. The next retry must
    // reuse the persisted request identity instead of creating a second clip.
    const lostText = "响应丢失恢复\n\n这条只应保存一次";
    const lostTags = "恢复, 丢失";
    let dropClipResponse = true;
    await page.route("**/api/knowledge/clips*", async route => {
      if (!dropClipResponse || route.request().method() !== "POST") return route.continue();
      dropClipResponse = false;
      const response = await route.fetch();
      assert(response.ok(), "The simulated committed clip was rejected");
      await route.abort("failed");
    });
    await page.getByLabel("搜索知识片段").fill("");
    await page.getByLabel("片段原文").fill(lostText);
    await page.getByLabel("片段标签").fill(lostTags);
    await page.getByRole("button", { name: "保存片段" }).click();
    await page.getByRole("status").filter({ hasText: "上次保存尚未确认" }).waitFor();
    await page.unroute("**/api/knowledge/clips*");
    await page.getByLabel("片段原文").fill(lostText + " 改");
    await page.getByRole("button", { name: "保存片段", exact: true }).waitFor();
    assert.equal(await page.getByRole("button", { name: "重试上次保存" }).count(), 0);
    await page.getByLabel("片段原文").fill(lostText);
    await page.getByRole("button", { name: "重试上次保存" }).waitFor();
    await page.reload();
    await page.getByRole("heading", { name: "知识", exact: true }).waitFor();
    await page.getByRole("status").filter({ hasText: "上次保存尚未确认" }).waitFor();
    await page.getByRole("button", { name: "重试保存" }).click();
    await page.getByRole("status").filter({ hasText: "上次保存尚未确认" }).waitFor({ state: "detached" });
    await page.getByRole("heading", { name: "响应丢失恢复", exact: true }).waitFor();
    const recovered = (await (await api.get("/api/knowledge/clips?query=" + encodeURIComponent("响应丢失恢复"))).json()).clips;
    assert.equal(recovered.length, 1);
    assert.equal(recovered[0].content, lostText);
    assert.deepEqual(recovered[0].tags, ["恢复", "丢失"]);
    assert.equal(await page.evaluate(() => localStorage.getItem("cairn.web.pending.clip")), null);
    console.log("Committed but lost knowledge response recovered with the same request.");

    for (let index = 0; index < 120; index += 1) {
      const response = await api.post("/api/knowledge/clips", {
        data: { content: `批量片段 ${index}`, source: "web", tags: ["bulk-pagination"], request_id: `bulk-${index}` },
      });
      assert(response.ok(), "Could not seed bulk clip " + index);
    }
    await page.getByLabel("搜索知识片段").fill("bulk-pagination");
    await page.locator(".knowledge-card").filter({ hasText: "响应丢失恢复" }).waitFor({ state: "detached" });
    await page.waitForFunction(() => document.querySelectorAll(".knowledge-card").length === 50);
    await page.getByRole("button", { name: "加载更多" }).click();
    await page.waitForFunction(() => document.querySelectorAll(".knowledge-card").length === 100);
    await page.getByRole("button", { name: "加载更多" }).click();
    await page.waitForFunction(() => document.querySelectorAll(".knowledge-card").length === 120);
    await page.getByRole("button", { name: "加载更多" }).waitFor({ state: "detached" });
    const bulkTitles = await page.locator(".knowledge-card h2").allTextContents();
    assert.equal(bulkTitles.length, 120);
    assert.equal(new Set(bulkTitles).size, 120);
    await page.getByLabel("搜索知识片段").fill("批量片段 119");
    await page.waitForFunction(() => document.querySelectorAll(".knowledge-card").length === 1);
    assert.equal(await page.getByRole("button", { name: "加载更多" }).count(), 0);
    await page.getByLabel("搜索知识片段").fill("");
    await page.waitForFunction(() => document.querySelectorAll(".knowledge-card").length === 50);

    // A filter change while the next page is in flight must not leave the
    // load-more control stuck in its loading state.
    const knowledgeList = /\/api\/knowledge\/clips\?/;
    let delayNextKnowledgePage = true;
    await page.route(knowledgeList, async route => {
      if (!delayNextKnowledgePage || new URL(route.request().url()).searchParams.get("offset") !== "50") return route.continue();
      delayNextKnowledgePage = false;
      await new Promise(resolve => setTimeout(resolve, 600));
      return route.continue();
    });
    await page.getByRole("button", { name: "加载更多" }).click();
    await page.getByLabel("搜索知识片段").fill("批量片段 119");
    await page.waitForFunction(() => document.querySelectorAll(".knowledge-card").length === 1);
    await page.getByLabel("搜索知识片段").fill("");
    await page.waitForFunction(() => document.querySelectorAll(".knowledge-card").length === 50);
    await page.getByRole("button", { name: "加载更多" }).waitFor();
    assert.equal(await page.getByRole("button", { name: "加载更多" }).isEnabled(), true);

    await page.getByRole("button", { name: "加载更多" }).click();
    await page.waitForFunction(() => document.querySelectorAll(".knowledge-card").length === 100);
    await page.getByRole("checkbox", { name: "显示已归档" }).check();
    await page.waitForFunction(() => document.querySelectorAll(".knowledge-card").length === 50);
    await page.getByRole("button", { name: "加载更多" }).waitFor();
    console.log("Knowledge pagination, search reset and archive reset passed with 120 clips.");

    await page.getByRole("button", { name: "复盘", exact: true }).click();
    await page.getByRole("tab", { name: "日报", exact: true }).waitFor();
    assert.equal(new URL(page.url()).searchParams.get("pane"), "reviews");
    await page.getByRole("heading", { name: "2026-01-15 日报", exact: true }).first().waitFor();
    const dailyCards = page.locator(".review-card").filter({ hasText: "2026-01-15" });
    await page.waitForFunction(() => document.querySelectorAll(".review-card").length === 3);
    assert.equal(await dailyCards.count(), 2);
    const newestDaily = dailyCards.first();
    assert.match((await newestDaily.textContent()) ?? "", /v2/);
    assert.match((await dailyCards.last().textContent()) ?? "", /v1/);

    await newestDaily.getByRole("button").first().click();
    const dailyDialog = page.getByRole("dialog", { name: "日报详情" });
    await dailyDialog.waitFor();
    await dailyDialog.getByText("第二版日报: 补充了复盘投递的状态核对。", { exact: false }).first().waitFor();
    await dailyDialog.getByText("已接收", { exact: true }).waitFor();
    await dailyDialog.getByText("取代", { exact: false }).waitFor();
    await dailyDialog.locator("details.review-snapshot").filter({ hasText: "clip-review-source" }).locator("summary").click();
    await dailyDialog.getByText("保留了可核对的知识片段。", { exact: false }).waitFor();
    await shot("Cairn-workbench-review-detail");
    await page.keyboard.press("Escape");
    await dailyDialog.waitFor({ state: "detached" });

    // Browser history reopens the detail it pushed.
    await newestDaily.getByRole("button").first().click();
    await dailyDialog.waitFor();
    await page.goBack();
    await dailyDialog.waitFor({ state: "detached" });
    await page.goForward();
    await dailyDialog.waitFor();
    await page.keyboard.press("Escape");
    await dailyDialog.waitFor({ state: "detached" });
    await newestDaily.getByRole("button").first().click();
    await dailyDialog.waitFor();
    await page.reload();
    await page.getByRole("heading", { name: "知识", exact: true }).waitFor();
    await dailyDialog.waitFor();
    assert(new URL(page.url()).searchParams.get("review"));
    await page.keyboard.press("Escape");
    await dailyDialog.waitFor({ state: "detached" });

    // A -> B detail identity. The previous report must not remain visible
    // while the next review_id loads, and a failed load must not fall back to
    // the previous report either.
    const detailPattern =
      /\/api\/knowledge\/reviews\/(?!schedule(?:\/|$))[^/?]+$/;
    await dailyCards.last().getByRole("button").first().click();
    await dailyDialog.getByText("第一版日报", { exact: false }).first().waitFor();
    await page.keyboard.press("Escape");
    await dailyDialog.waitFor({ state: "detached" });

    let releaseDetail;
    const detailGate = new Promise(resolve => { releaseDetail = resolve; });
    let detailStartedResolve;
    const detailStarted = new Promise(resolve => { detailStartedResolve = resolve; });
    await page.route(detailPattern, async route => {
      if (route.request().method() !== "GET") return route.continue();
      detailStartedResolve();
      await detailGate;
      await route.continue().catch(() => {});
    });
    await newestDaily.getByRole("button").first().click();
    await detailStarted;
    await dailyDialog.getByRole("status").filter({ hasText: "正在读取复盘详情" }).waitFor();
    assert.equal(await dailyDialog.getByText("第一版日报", { exact: false }).count(), 0);
    releaseDetail();
    await dailyDialog.getByText("第二版日报", { exact: false }).first().waitFor();
    await page.unroute(detailPattern);
    await page.keyboard.press("Escape");
    await dailyDialog.waitFor({ state: "detached" });

    await page.route(detailPattern, async route => {
      if (route.request().method() !== "GET") return route.continue();
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ error: "review_store_unavailable" }),
      });
    });
    await dailyCards.last().getByRole("button").first().click();
    await dailyDialog.getByText("复盘存储暂时不可用", { exact: false }).waitFor();
    assert.equal(await dailyDialog.getByText("第二版日报", { exact: false }).count(), 0);
    await page.unroute(detailPattern);
    await page.keyboard.press("Escape");
    await dailyDialog.waitFor({ state: "detached" });

    await page.getByLabel("按日期筛选").fill("2026-01-15");
    await page.getByRole("button", { name: "筛选", exact: true }).click();
    assert.equal(new URL(page.url()).searchParams.get("day"), "2026-01-15");
    await page.waitForFunction(() => document.querySelectorAll(".review-card").length === 2);
    await page.getByLabel("按日期筛选").fill("2026-01-17");
    await page.getByRole("button", { name: "筛选", exact: true }).click();
    await page.getByText("该筛选下没有复盘报告。").waitFor();
    await page.getByRole("button", { name: "清除", exact: true }).click();
    await page.waitForFunction(() => document.querySelectorAll(".review-card").length === 3);

    await page.getByLabel("按日期筛选").fill("2026-01-16");
    await page.getByRole("button", { name: "筛选", exact: true }).click();
    const failedCard = page.locator(".review-card").filter({ hasText: "2026-01-16" });
    await failedCard.getByText("生成失败", { exact: true }).waitFor();
    await failedCard.getByText("Server 未配置模型 Provider", { exact: false }).waitFor();
    await page.getByRole("button", { name: "清除", exact: true }).click();

    await page.getByRole("tab", { name: "周报", exact: true }).click();
    const weeklyCard = page.locator(".review-card").filter({ hasText: "2026-W03" });
    await weeklyCard.getByText("2026-W03 周报", { exact: true }).waitFor();
    await page.getByLabel("按 ISO 周次筛选").fill("2026-W03");
    await page.getByRole("button", { name: "筛选", exact: true }).click();
    assert.equal(new URL(page.url()).searchParams.get("week"), "2026-W03");
    await weeklyCard.waitFor();
    await page.getByLabel("按 ISO 周次筛选").fill("2026-W04");
    await page.getByRole("button", { name: "筛选", exact: true }).click();
    await page.getByText("该筛选下没有复盘报告。").waitFor();
    await page.getByRole("button", { name: "清除", exact: true }).click();
    await weeklyCard.getByRole("button").first().click();
    const weeklyDialog = page.getByRole("dialog", { name: "周报详情" });
    await weeklyDialog.waitFor();
    await weeklyDialog.getByText("投递结果不明", { exact: true }).waitFor();
    await weeklyDialog.getByText("消息可能已经送达", { exact: false }).waitFor();
    const resend = weeklyDialog.getByRole("button", { name: "重新投递", exact: true });
    assert.equal(await resend.isDisabled(), true);
    await weeklyDialog.getByRole("checkbox", { name: "我已确认可能重复发送" }).check();
    assert.equal(await resend.isEnabled(), true);
    await resend.click();
    await weeklyDialog.getByRole("status").filter({ hasText: "已重新入队" }).waitFor();
    await page.keyboard.press("Escape");
    await weeklyDialog.waitFor({ state: "detached" });
    await shot("Cairn-workbench-reviews");

    // Reduced Motion must leave the list visible, and a reload must restore
    // the pane, type and detail route from the URL.
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.getByRole("tab", { name: "日报", exact: true }).click();
    await page.waitForFunction(() => document.querySelectorAll(".review-card").length === 3);
    await page.reload();
    await page.getByRole("heading", { name: "知识", exact: true }).waitFor();
    assert.equal(new URL(page.url()).searchParams.get("pane"), "reviews");
    await page.waitForFunction(() => document.querySelectorAll(".review-card").length === 3);
    await page.emulateMedia({ reducedMotion: "no-preference" });

    // Manual generation runs on the Server. Pick the cutoff period that
    // contains the current fixture traffic: today before 20:30 Asia/Shanghai,
    // otherwise tomorrow. This fixture has no provider, so the honest outcome
    // is a failed report with its real error reason.
    await page.getByRole("button", { name: "生成日报", exact: true }).click();
    await page.getByRole("heading", { name: "生成日报", exact: true }).waitFor();
    const reviewDay = await page.evaluate(async () => {
      const policy = await (
        await fetch("/api/runtime/management?section=review")
      ).json();
      const zone = policy.review?.timezone ?? "Asia/Shanghai";
      const cutoff = policy.review?.daily?.time ?? "20:30";
      const [datePart, timePart] = new Date()
        .toLocaleString("sv-SE", { timeZone: zone })
        .split(" ");
      const day = new Date(datePart + "T00:00:00Z");
      if (timePart >= cutoff) day.setUTCDate(day.getUTCDate() + 1);
      return day.toISOString().slice(0, 10);
    });
    await page.getByLabel("日期", { exact: true }).fill(reviewDay);
    await page.getByRole("button", { name: "开始生成", exact: true }).click();
    const generatedDialog = page.getByRole("dialog", { name: "日报详情" });
    await generatedDialog.waitFor();
    await generatedDialog.locator('.review-state[data-status="failed"]').waitFor({ timeout: 15000 });
    await generatedDialog.getByText("Server 未配置模型 Provider", { exact: false }).waitFor();
    assert(new URL(page.url()).searchParams.get("review"));
    await page.keyboard.press("Escape");
    await generatedDialog.waitFor({ state: "detached" });
    console.log("Review console history, filters, versions, evidence and delivery passed.");

    // Pagination race: seed enough daily reports to expose page 2, hold that
    // response, switch to weekly, then release the stale daily page. Only the
    // weekly records may remain.
    await page.evaluate(async () => {
      for (let index = 0; index < 22; index += 1) {
        const day = new Date(Date.UTC(2024, 11, 1 + index))
          .toISOString()
          .slice(0, 10);
        const response = await fetch("/api/knowledge/reviews/daily", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            date: day,
            timezone: "Asia/Shanghai",
            request_id: "e2e-page-" + index,
          }),
        });
        if (!response.ok) throw new Error("daily seed failed: " + response.status);
      }
    });
    await page.getByRole("tab", { name: "周报", exact: true }).click();
    await page.getByRole("tab", { name: "日报", exact: true }).click();
    await page.waitForFunction(() => document.querySelectorAll(".review-card").length === 20);
    const loadMore = page.getByRole("button", { name: "加载更多" });
    await loadMore.waitFor();

    const listPattern = /\/api\/knowledge\/reviews\?/;
    let releaseNextPage;
    const nextPageGate = new Promise(resolve => { releaseNextPage = resolve; });
    let nextPageStartedResolve;
    const nextPageStarted = new Promise(resolve => { nextPageStartedResolve = resolve; });
    await page.route(listPattern, async route => {
      const target = new URL(route.request().url());
      if (
        target.searchParams.get("type") === "daily" &&
        target.searchParams.get("offset") === "20"
      ) {
        nextPageStartedResolve();
        await nextPageGate;
        await route.continue().catch(() => {});
      } else {
        await route.continue();
      }
    });
    await loadMore.click();
    await nextPageStarted;
    await page.getByRole("tab", { name: "周报", exact: true }).click();
    await page.locator(".review-card").filter({ hasText: "2026-W03" }).waitFor();
    releaseNextPage();
    await page.waitForTimeout(300);
    assert.equal(await page.locator(".review-card").count(), 1);
    assert.equal(
      await page.locator(".review-card").filter({ hasText: "日报" }).count(),
      0,
    );
    await page.unroute(listPattern);
    await page.getByRole("tab", { name: "日报", exact: true }).click();
    await page.waitForFunction(() => document.querySelectorAll(".review-card").length === 20);
    await loadMore.waitFor();
    assert.equal(await loadMore.isEnabled(), true);
    console.log("Review pagination race and detail identity passed.");

    // Channels: four fixed platforms, real CRUD against the isolated Server,
    // with capability and lifecycle route mocks only where no real adapter exists.
    const channelSecret = ["e2e", "channel", "secret", Date.now().toString(36)].join("-");
    async function createChannel(platform, name, credentials, requestId) {
      const response = await api.post(`/api/channels/${platform}/connections`, {
        data: {
          request_id: requestId,
          display_name: name,
          enabled: true,
          ...(credentials ? { credentials } : {}),
        },
      });
      assert.equal(response.status(), 201, "Could not seed " + name);
      return response.json();
    }
    async function readChannels() {
      const response = await api.get("/api/channels");
      assert.equal(response.status(), 200, "Could not read channels");
      return response.json();
    }

    await createChannel("telegram", "E2E Telegram", { bot_token: channelSecret }, "e2e-channel-telegram");
    for (let index = 1; index <= 3; index += 1) {
      await createChannel(
        "discord",
        `E2E Discord ${index}`,
        { bot_token: `e2e-discord-secret-${index}` },
        `e2e-channel-discord-${index}`,
      );
    }

    await page.getByRole("link", { name: "连接", exact: true }).click();
    await page.getByRole("heading", { name: "连接", exact: true }).waitFor();
    await page.waitForFunction(() => document.querySelectorAll(".channels-platform").length === 4);
    const telegramCard = page.locator('.channels-platform[data-platform="telegram"]');
    const discordCard = page.locator('.channels-platform[data-platform="discord"]');
    const qqCard = page.locator('.channels-platform[data-platform="qq"]');
    assert.match(await telegramCard.textContent(), /已配置 1 · 运行中 0/);
    assert.match(await discordCard.textContent(), /已配置 3 · 运行中 0/);
    assert.match(await qqCard.textContent(), /未连接/);
    await shot("Cairn-workbench-channels");

    // Exercise all three overview states without inventing a real adapter:
    // the mocked overview is still shaped exactly like the Server contract.
    const overviewPattern = /\/api\/channels$/;
    const mockedOverview = {
      platforms: [
        { platform: "wechat", display_state: "unconfigured", connections: 0, running: 0 },
        { platform: "qq", display_state: "running", connections: 1, running: 1 },
        { platform: "telegram", display_state: "silent", connections: 1, running: 0 },
        { platform: "discord", display_state: "unconfigured", connections: 0, running: 0 },
      ],
      connections: [],
    };
    await page.route(overviewPattern, async route => {
      if (route.request().method() !== "GET") return route.continue();
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(mockedOverview) });
    });
    await page.reload();
    await page.waitForFunction(() => document.querySelectorAll(".channels-platform").length === 4);
    assert.equal(await page.locator('.channels-platform[data-platform="wechat"] .channel-state').textContent(), "未连接");
    assert.equal(await page.locator('.channels-platform[data-platform="qq"] .channel-state').textContent(), "运行中");
    assert.equal(await page.locator('.channels-platform[data-platform="telegram"] .channel-state').textContent(), "沉默中");
    await page.unroute(overviewPattern);
    await page.reload();
    await page.waitForFunction(() => document.querySelectorAll(".channels-platform").length === 4);

    // 0 / 1 / N configured connections on the real fixture.
    await discordCard.click();
    await page.getByRole("heading", { name: "Discord连接", exact: true }).waitFor();
    await page.waitForFunction(() => document.querySelectorAll(".channels-connection").length === 3);
    await page.goBack();
    await page.getByRole("heading", { name: "连接", exact: true }).waitFor();
    await qqCard.click();
    await page.getByText("还没有 QQ 连接。").waitFor();
    await page.goBack();
    await page.getByRole("heading", { name: "连接", exact: true }).waitFor();
    await telegramCard.click();
    await page.getByRole("heading", { name: "Telegram连接", exact: true }).waitFor();
    await page.waitForFunction(() => document.querySelectorAll(".channels-connection").length === 1);

    // The rail link is an App-level navigation. It must reset this page's URL
    // state instead of leaving a detail view behind an overview URL.
    await page.getByRole("link", { name: "连接", exact: true }).click();
    await page.getByRole("heading", { name: "连接", exact: true }).waitFor();
    assert.equal(new URL(page.url()).search, "");
    await telegramCard.click();
    await page.getByRole("heading", { name: "Telegram连接", exact: true }).waitFor();

    // Create and edit through the capability-driven form. Credentials are
    // write-only, and editing metadata must not rotate the credential reference.
    await page.getByRole("button", { name: "新建连接", exact: true }).click();
    await page.getByRole("heading", { name: "新建连接", exact: true }).waitFor();
    assert.equal(new URL(page.url()).searchParams.get("mode"), "new");
    const createdSecret = ["e2e", "created", "token", Date.now().toString(36)].join("-");
    await page.getByLabel("连接名称").fill("E2E Telegram 2");
    await page.getByLabel("Bot Token").fill(createdSecret);
    await shot("Cairn-workbench-channel-form");
    await page.getByRole("button", { name: "保存连接", exact: true }).click();
    await page.getByRole("heading", { name: "E2E Telegram 2", exact: true }).waitFor();
    const channelId = new URL(page.url()).searchParams.get("connection_id");
    assert(channelId, "Saving must open the created connection");
    assert.equal(new URL(page.url()).searchParams.get("mode"), null);
    assert(!(await page.locator("body").innerText()).includes(createdSecret));
    assert(await page.getByRole("button", { name: "启动", exact: true }).isDisabled());
    await page.getByText(/没有该平台的真实 Adapter/).first().waitFor();
    await shot("Cairn-workbench-channel-detail");

    const beforeEdit = (await readChannels()).connections.find(view => view.connection.connection_id === channelId);
    assert(beforeEdit, "The created connection must be listed");
    assert(beforeEdit.connection.credential_ref);
    await page.getByRole("button", { name: "编辑", exact: true }).click();
    await page.getByRole("heading", { name: "编辑连接", exact: true }).waitFor();
    assert.equal(await page.getByLabel("Bot Token").inputValue(), "");
    await page.getByLabel("连接名称").fill("E2E Telegram 2 renamed");
    await page.getByRole("button", { name: "保存修改", exact: true }).click();
    await page.getByRole("heading", { name: "E2E Telegram 2 renamed", exact: true }).waitFor();
    const afterNameEdit = (await readChannels()).connections.find(view => view.connection.connection_id === channelId);
    assert.equal(afterNameEdit.connection.credential_ref, beforeEdit.connection.credential_ref);

    await page.getByRole("button", { name: "编辑", exact: true }).click();
    await page.getByLabel("Bot Token").fill("e2e-replacement-token-value");
    await page.getByRole("button", { name: "保存修改", exact: true }).click();
    await page.getByRole("heading", { name: "E2E Telegram 2 renamed", exact: true }).waitFor();
    const afterCredentialEdit = (await readChannels()).connections.find(view => view.connection.connection_id === channelId);
    assert.notEqual(afterCredentialEdit.connection.credential_ref, beforeEdit.connection.credential_ref);
    assert(!JSON.stringify(afterCredentialEdit).includes("e2e-replacement-token-value"));

    // Refresh, Back and Forward keep the URL as the authority for the view.
    await page.reload();
    await page.getByRole("heading", { name: "E2E Telegram 2 renamed", exact: true }).waitFor();
    assert.equal(new URL(page.url()).searchParams.get("connection_id"), channelId);
    await page.goBack();
    await page.getByRole("heading", { name: "E2E Telegram 2 renamed", exact: true }).waitFor();
    assert.equal(new URL(page.url()).searchParams.get("connection_id"), channelId);
    await page.goBack();
    await page.getByRole("heading", { name: "E2E Telegram 2 renamed", exact: true }).waitFor();
    assert.equal(new URL(page.url()).searchParams.get("connection_id"), channelId);
    await page.goBack();
    await page.getByRole("heading", { name: "Telegram连接", exact: true }).waitFor();
    assert.equal(new URL(page.url()).searchParams.get("connection_id"), null);
    await page.goForward();
    await page.getByRole("heading", { name: "E2E Telegram 2 renamed", exact: true }).waitFor();
    await page.goForward();
    await page.getByRole("heading", { name: "E2E Telegram 2 renamed", exact: true }).waitFor();
    await page.goForward();
    await page.getByRole("heading", { name: "E2E Telegram 2 renamed", exact: true }).waitFor();

    // A lost write response keeps the original submission intent. Retrying the
    // same fields must reuse that request id instead of applying a second
    // update under a new identity.
    const beforeLost = (await readChannels()).connections.find(
      view => view.connection.connection_id === channelId,
    );
    assert(beforeLost, "The connection must still exist before the lost-write checks");
    const lostIds = [];
    let loseFirstUpdate = true;
    const updatePattern = new RegExp(`/api/channels/connections/${channelId}$`);
    await page.route(updatePattern, async route => {
      if (route.request().method() !== "PATCH") return route.continue();
      lostIds.push(route.request().postDataJSON().request_id);
      if (loseFirstUpdate) {
        loseFirstUpdate = false;
        await route.fetch();
        await route.abort("failed");
        return;
      }
      await route.continue();
    });
    await page.getByRole("button", { name: "编辑", exact: true }).click();
    await page.getByLabel("宿主所有者（可选）").fill("e2e-owner-after-loss");
    await page.getByRole("button", { name: "保存修改", exact: true }).click();
    await page.locator('[data-channel-error="transport"]').waitFor();
    await page.getByRole("button", { name: "保存修改", exact: true }).click();
    await page.getByRole("heading", { name: "E2E Telegram 2 renamed", exact: true }).waitFor();
    assert.equal(lostIds.length, 2, "Only the original and its retry may reach the Server");
    assert.equal(lostIds[1], lostIds[0], "An identical retry must keep its request id");
    const afterLost = (await readChannels()).connections.find(
      view => view.connection.connection_id === channelId,
    );
    assert.equal(afterLost.connection.owner_identity, "e2e-owner-after-loss");
    assert.equal(afterLost.connection.revision, beforeLost.connection.revision + 1);
    await page.unroute(updatePattern);

    // Changing the payload after the same lost response is a new intent. The
    // console reconciles the old receipt and asks for a fresh submission; it
    // must not send the old request id with the new credential.
    const beforeCredentialLoss = (await readChannels()).connections.find(
      view => view.connection.connection_id === channelId,
    );
    const credentialIds = [];
    let loseCredentialUpdate = true;
    await page.route(updatePattern, async route => {
      if (route.request().method() !== "PATCH") return route.continue();
      credentialIds.push(route.request().postDataJSON().request_id);
      if (loseCredentialUpdate) {
        loseCredentialUpdate = false;
        await route.fetch();
        await route.abort("failed");
        return;
      }
      await route.continue();
    });
    await page.getByRole("button", { name: "编辑", exact: true }).click();
    const changedCredential = ["e2e", "lost", "credential", Date.now().toString(36)].join("-");
    const nextCredential = ["e2e", "next", "credential", Date.now().toString(36)].join("-");
    await page.getByLabel("Bot Token").fill(changedCredential);
    await page.getByRole("button", { name: "保存修改", exact: true }).click();
    await page.locator('[data-channel-error="transport"]').waitFor();
    await page.getByLabel("Bot Token").fill(nextCredential);
    await page.getByRole("button", { name: "保存修改", exact: true }).click();
    await page.locator('[data-channel-error="stale-intent"]').waitFor();
    assert.equal(credentialIds.length, 1, "A changed payload must not replay the old request");
    await page.getByRole("button", { name: "保存修改", exact: true }).click();
    await page.getByRole("heading", { name: "E2E Telegram 2 renamed", exact: true }).waitFor();
    assert.equal(credentialIds.length, 2);
    assert.notEqual(credentialIds[1], credentialIds[0], "A new intent needs a new request id");
    const afterCredentialLoss = (await readChannels()).connections.find(
      view => view.connection.connection_id === channelId,
    );
    assert.notEqual(
      afterCredentialLoss.connection.credential_ref,
      beforeCredentialLoss.connection.credential_ref,
    );
    assert(!JSON.stringify(afterCredentialLoss).includes(nextCredential));
    assert(!JSON.stringify(afterCredentialLoss).includes(changedCredential));
    await page.unroute(updatePattern);
    console.log("Channel lost-response intent boundaries passed.");

    await page.getByRole("button", { name: "删除", exact: true }).click();
    const deleteDialog = page.getByRole("alertdialog", { name: "确认删除连接" });
    await deleteDialog.waitFor();
    await deleteDialog.getByRole("button", { name: "取消", exact: true }).click();
    await deleteDialog.waitFor({ state: "detached" });
    await page.getByRole("heading", { name: "E2E Telegram 2 renamed", exact: true }).waitFor();
    await page.getByRole("button", { name: "删除", exact: true }).click();
    await page.getByRole("alertdialog", { name: "确认删除连接" }).getByRole("button", { name: "确认删除", exact: true }).click();
    await page.getByRole("heading", { name: "Telegram连接", exact: true }).waitFor();
    await page.waitForFunction(() => document.querySelectorAll(".channels-connection").length === 1);
    assert(!(await readChannels()).connections.some(view => view.connection.connection_id === channelId));
    console.log("Channel console CRUD, history and write-only credentials passed.");

    // Request receipts: identical replay returns the same connection, a reused
    // request id with different intent is rejected, and secrets never echo.
    const idempotentSecret = ["e2e", "idempotent", "token", Date.now().toString(36)].join("-");
    const idempotentBody = {
      request_id: "e2e-channel-idempotent",
      display_name: "E2E Idempotent",
      enabled: true,
      credentials: { bot_token: idempotentSecret },
    };
    const replayFirst = await api.post("/api/channels/telegram/connections", { data: idempotentBody });
    assert.equal(replayFirst.status(), 201);
    const firstIdempotent = await replayFirst.json();
    assert(!JSON.stringify(firstIdempotent).includes(idempotentSecret));
    const replayAgain = await api.post("/api/channels/telegram/connections", { data: idempotentBody });
    assert.equal(replayAgain.status(), 201);
    assert.equal((await replayAgain.json()).connection.connection_id, firstIdempotent.connection.connection_id);
    const replayConflict = await api.post("/api/channels/telegram/connections", {
      data: { ...idempotentBody, display_name: "E2E Idempotent Different" },
    });
    assert.equal(replayConflict.status(), 409);
    assert.equal((await replayConflict.json()).error, "request_id_conflict");
    assert(!JSON.stringify(await readChannels()).includes(idempotentSecret));
    await api.delete(`/api/channels/connections/${firstIdempotent.connection.connection_id}`, {
      data: {
        expected_revision: firstIdempotent.connection.revision,
        request_id: "e2e-channel-idempotent-cleanup",
        confirm: true,
      },
    });
    console.log("Channel request idempotency and write-only credentials passed.");

    // Lifecycle failures and reconnect are rendered from Server responses.
    // A mocked capability represents a Server that has registered the adapter;
    // the mocked POSTs verify the browser error/notice paths.
    const baseOverview = await readChannels();
    const seededTelegram = baseOverview.connections.find(view => view.connection.display_name === "E2E Telegram");
    assert(seededTelegram, "The seeded Telegram connection must remain");
    const capabilityResponse = await api.get("/api/channels/capabilities");
    assert.equal(capabilityResponse.status(), 200);
    const mockedCapabilities = await capabilityResponse.json();
    mockedCapabilities.platforms = mockedCapabilities.platforms.map(row =>
      row.platform === "telegram"
        ? { ...row, adapter_supported: true, supported_actions: ["create", "update", "delete", "start", "stop", "reconnect"] }
        : row,
    );
    const capabilityPattern = /\/api\/channels\/capabilities$/;
    const startPattern = /\/api\/channels\/connections\/[^/]+\/start$/;
    const reconnectPattern = /\/api\/channels\/connections\/[^/]+\/reconnect$/;
    await page.route(capabilityPattern, async route => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(mockedCapabilities) });
    });
    await page.route(startPattern, async route => {
      if (route.request().method() !== "POST") return route.continue();
      await route.fulfill({ status: 501, contentType: "application/json", body: JSON.stringify({ error: "adapter_not_implemented" }) });
    });
    await page.route(reconnectPattern, async route => {
      if (route.request().method() !== "POST") return route.continue();
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          request_id: "e2e-channel-reconnect",
          connection: seededTelegram.connection,
          runtime: { ...seededTelegram.runtime, actual_state: "running", last_connected_at: new Date().toISOString() },
        }),
      });
    });
    await page.goto(new URL(`/channels.html?platform=telegram&connection_id=${seededTelegram.connection.connection_id}`, url).href);
    await page.getByRole("heading", { name: "E2E Telegram", exact: true }).waitFor();
    await page.getByRole("button", { name: "启动", exact: true }).click();
    await page.getByRole("alert").filter({ hasText: "尚未实现该平台的 Adapter" }).waitFor();

    // A poll already in flight when a lifecycle action succeeds must not
    // overwrite the newer action result with its older snapshot.
    let releasePoll;
    let markPollStarted;
    const pollStarted = new Promise(resolve => {
      markPollStarted = resolve;
    });
    await page.route(overviewPattern, async route => {
      if (route.request().method() !== "GET") return route.continue();
      markPollStarted();
      await new Promise(resolve => {
        releasePoll = resolve;
      });
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(baseOverview),
      });
    });
    await pollStarted;
    await page.getByRole("button", { name: "重连", exact: true }).click();
    await page.getByText(/已提交重连/).waitFor();
    await page.locator('.channel-state[data-state="running"]').waitFor({ timeout: 2000 });
    releasePoll();
    await page.waitForTimeout(500);
    assert.equal(
      await page.locator('.channel-state[data-state="running"]').count(),
      1,
      "An older poll response must not overwrite the action result",
    );
    await page.unroute(overviewPattern);
    await page.unroute(capabilityPattern);
    await page.unroute(startPattern);
    await page.unroute(reconnectPattern);
    console.log("Channel lifecycle failure and reconnect states passed.");

    // Polling is single-flight: a slow response makes the next visible tick
    // wait instead of stacking another request. It still keeps polling.
    const pollGate = { active: 0, max: 0, count: 0 };
    let releaseSlowPoll;
    const slowPollGate = new Promise(resolve => {
      releaseSlowPoll = resolve;
    });
    await page.route(overviewPattern, async route => {
      if (route.request().method() !== "GET") return route.continue();
      pollGate.active += 1;
      pollGate.count += 1;
      pollGate.max = Math.max(pollGate.max, pollGate.active);
      try {
        await Promise.race([
          new Promise(resolve => setTimeout(resolve, 6000)),
          slowPollGate,
        ]);
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(baseOverview),
        });
      } finally {
        pollGate.active -= 1;
      }
    });
    await page.goto(new URL("/channels.html", url).href);
    await page.waitForTimeout(8500);
    assert.equal(pollGate.max, 1, "Overview polling must not overlap");
    assert(pollGate.count >= 2, "A visible page must keep polling");
    releaseSlowPoll();
    await page.waitForTimeout(200);
    await page.unroute(overviewPattern);
    console.log("Channel polling is single-flight.");

    // The legacy WeChat projection has no normal create/edit/delete path and
    // keeps pointing at the existing settings login flow.
    const legacyConnection = {
      connection_id: "legacy-wechat",
      platform: "wechat",
      display_name: "个人微信 Bot",
      enabled: false,
      desired_state: "stopped",
      credential_ref: null,
      owner_identity: null,
      allowed_senders: [],
      metadata: { legacy: true, adapter: "wechat_session" },
      revision: 1,
      created_at: "2026-01-01T00:00:00+00:00",
      updated_at: "2026-01-01T00:00:00+00:00",
      legacy: true,
    };
    const legacyOverview = {
      ...baseOverview,
      platforms: baseOverview.platforms.map(row =>
        row.platform === "wechat"
          ? { ...row, display_state: "silent", connections: 1, running: 0 }
          : row,
      ),
      connections: [
        {
          connection: legacyConnection,
          runtime: {
            connection_id: "legacy-wechat",
            actual_state: "stopped",
            last_connected_at: null,
            last_heartbeat_at: null,
            last_error: null,
            next_retry_at: null,
          },
        },
      ],
    };
    await page.route(overviewPattern, async route => {
      if (route.request().method() !== "GET") return route.continue();
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(legacyOverview) });
    });
    await page.goto(new URL("/channels.html?platform=wechat", url).href);
    await page.getByRole("heading", { name: "微信连接", exact: true }).waitFor();
    await page.getByRole("heading", { name: "宿主托管的微信", exact: true }).waitFor();
    const realManagement = await api.get("/api/runtime/management?section=wechat");
    assert.equal(realManagement.status(), 200);
    const managementSnapshot = await realManagement.json();
    assert.equal(typeof managementSnapshot.wechat?.status, "string");
    await page.locator(".channels-legacy-status").waitFor();
    assert((await page.locator(".channels-legacy-status").innerText()).trim().length > 0);
    assert.equal(await page.getByRole("button", { name: "新建连接", exact: true }).count(), 0);
    await page.locator(".channels-connection").filter({ hasText: "个人微信 Bot" }).click();
    await page.getByRole("heading", { name: "个人微信 Bot", exact: true }).waitFor();
    assert.equal(await page.getByRole("button", { name: "编辑", exact: true }).count(), 0);
    assert.equal(await page.getByRole("button", { name: "删除", exact: true }).count(), 0);
    assert.match(
      await page.getByRole("link", { name: /设置 → 微信/ }).getAttribute("href"),
      /settings\.html\?section=wechat/,
    );

    // The real console reads the nested wechat object from the Server's
    // management snapshot; the QR and verification prompt follow that shape.
    const managementPattern = /\/api\/runtime\/management\?section=wechat$/;
    await page.route(managementPattern, async route => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          workspace: url,
          notices: [],
          pending_operations: [],
          wechat: {
            status: "微信登录: 等待扫码\n微信服务: 未运行",
            running: false,
            login_pending: true,
            verification_required: true,
            qr_image: "data:image/gif;base64,R0lGODlhAQABAAAAACw=",
            account_saved: false,
            enabled: true,
            trusted_senders: [],
          },
        }),
      });
    });
    await page.goto(new URL("/channels.html?platform=wechat", url).href);
    await page.getByRole("heading", { name: "宿主托管的微信", exact: true }).waitFor();
    await page.getByRole("img", { name: "微信登录二维码" }).waitFor();
    await page.locator('[data-wechat-notice="verification"]').waitFor();
    assert((await page.locator(".channels-legacy-status").innerText()).trim().length > 0);
    await page.unroute(managementPattern);
    await page.unroute(overviewPattern);
    console.log("Legacy WeChat compatibility panel passed.");

    // Reduced motion skips the GSAP entrance, and rapid history switching
    // leaves no stale cards or inline animation residue.
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto(new URL("/channels.html", url).href);
    await page.waitForFunction(() => document.querySelectorAll(".channels-platform").length === 4);
    const reducedMotion = await page.evaluate(() => ({
      matches: matchMedia("(prefers-reduced-motion: reduce)").matches,
      hidden: [...document.querySelectorAll("[data-channel-animate]")].filter(element => {
        const style = getComputedStyle(element);
        return style.opacity !== "1" || style.visibility !== "visible";
      }).length,
    }));
    assert(reducedMotion.matches);
    assert.equal(reducedMotion.hidden, 0);
    for (const candidate of ["qq", "telegram", "discord", "wechat", "qq", "telegram"]) {
      await page.locator(`.channels-platform[data-platform="${candidate}"]`).click();
      await page.goBack();
    }
    await page.getByRole("heading", { name: "连接", exact: true }).waitFor();
    await page.waitForFunction(() => document.querySelectorAll(".channels-platform").length === 4);
    await page.emulateMedia({ reducedMotion: "no-preference" });
    for (const candidate of ["qq", "telegram", "discord", "wechat", "qq", "telegram"]) {
      await page.locator(`.channels-platform[data-platform="${candidate}"]`).click();
      await page.goBack();
    }
    await page.getByRole("heading", { name: "连接", exact: true }).waitFor();
    await page.waitForFunction(() => document.querySelectorAll(".channels-platform").length === 4);
    await page.waitForTimeout(800);
    const animatedResidue = await page.evaluate(() =>
      [...document.querySelectorAll("[data-channel-animate]")].filter(element => {
        const style = getComputedStyle(element);
        return style.opacity !== "1" || style.visibility !== "visible";
      }).length,
    );
    assert.equal(animatedResidue, 0);
    console.log("Channel reduced motion and rapid route switching passed.");


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
      ["knowledge", knowledgeUrl, "知识"],
      ["channels", new URL("/channels.html", url).href, "连接"],
      ["run", runUrl, "执行时间线"],
      ...(approvalUrl ? [["approval", approvalUrl, "恢复核对"]] : []),
    ]) {
      await page.goto(path);
      await page.getByRole("heading", { name: heading, exact: true }).waitFor();
      await assertNoOverflow();
      await shot("Cairn-workbench-mobile-" + name);
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
    await page.goto(new URL("/knowledge.html?pane=reviews", url).href);
    await page.getByRole("heading", { name: "知识", exact: true }).waitFor();
    await page.locator(".review-card").first().waitFor();
    await assertNoOverflow();
    await shot("Cairn-workbench-mobile-reviews");
    await page.getByRole("link", { name: "设置", exact: true }).click();
    await page.getByRole("heading", { name: "微信连接" }).waitFor();
    await assertNoOverflow();
    await page.getByRole("button", { name: "复盘", exact: true }).click();
    await page.getByRole("heading", { name: "复盘报告", exact: true }).waitFor();
    await page.getByText("启用 · 每天 20:30 · 调度未启动", { exact: true }).waitFor();
    await page.getByText("启用 · 每周 20:40 · 调度未启动", { exact: true }).waitFor();
    await page.getByText(/已授权 · 账号 browser-fixture-bot/).waitFor();
    await page.getByText("修改复盘设置需要编辑 Server 配置并重启 cairn.service 才会生效", { exact: false }).waitFor();
    assert(!(await page.locator(".settings-main").innerText()).includes(token));
    await assertNoOverflow();
    await shot("Cairn-workbench-settings-review");

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
