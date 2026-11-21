import assert from "node:assert/strict";

export async function checkChatReturn(page, api, url) {
  const nav = page.getByRole("navigation", { name: "主导航" });
  const chat = nav.getByRole("link", { name: "对话", exact: true });
  const settings = nav.getByRole("link", { name: "设置", exact: true });
  const knowledge = nav.getByRole("link", { name: "知识", exact: true });
  const composer = page.getByRole("textbox", { name: "消息" });
  const newChat = page.getByRole("button", { name: "新对话", exact: true });
  const chatUrl = (id) => id ? `/chat.html?conversation=${id}` : "/chat.html";
  const waitForChat = async (id) => {
    await page.waitForURL(new URL(chatUrl(id), url).href);
    await composer.waitFor();
    assert.equal(await chat.getAttribute("aria-current"), "page");
  };
  const create = async (prompt, request_id) => {
    const response = await api.post("/api/runtime/conversations", { data: { prompt, source: "web", request_id } });
    assert(response.ok(), `Could not create chat fixture (${response.status()})`);
    return (await response.json()).conversation_id;
  };
  const remove = async id => {
    const response = await api.post(`/api/runtime/conversations/${id}/delete`, { data: {} });
    assert(response.ok(), `Could not remove chat fixture (${response.status()})`);
  };

  await page.goto(new URL("/", url).href);
  await composer.fill("Unsent on the home route");
  await chat.click();
  assert.equal(new URL(page.url()).pathname, "/");
  assert.equal(await composer.inputValue(), "Unsent on the home route");
  await settings.click();
  await chat.click();
  await waitForChat();

  const first = await create("Chat return first fixture", "chat-return-first");
  const second = await create("Chat return second fixture", "chat-return-second");
  for (const [id, title] of [[second, "Chat return second fixture"], [first, "Chat return first fixture"]]) {
    await page.locator(`.conversation-tree a[href="${chatUrl(id)}"]`).click();
    await page.getByRole("heading", { name: title, exact: true }).waitFor();
  }
  await knowledge.click();
  assert.equal(await chat.getAttribute("href"), chatUrl(first));
  await chat.click();
  await waitForChat(first);
  await page.getByRole("heading", { name: "Chat return first fixture", exact: true }).waitFor();
  await chat.click();
  await waitForChat();
  await composer.fill("Unsent on the new chat route");
  await chat.click();
  assert.equal(await composer.inputValue(), "Unsent on the new chat route");
  await settings.click();
  await page.reload();
  await chat.click();
  await waitForChat(first);

  await settings.click();
  await newChat.click();
  await waitForChat();
  await composer.fill("Chat return promoted fixture");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await page.waitForURL(next => next.searchParams.has("conversation"));
  const promoted = new URL(page.url()).searchParams.get("conversation");
  assert(promoted && promoted !== first && promoted !== second);
  await settings.click();
  assert.equal(await chat.getAttribute("href"), chatUrl(promoted));
  await chat.click();
  await waitForChat(promoted);
  await page.locator('.status-badge[data-status="completed"]').waitFor();
  await settings.click();

  const detailUrl = new URL(`/api/runtime/conversations/${promoted}`, url).href;
  const delayed = Promise.withResolvers();
  await page.route(detailUrl, async route => {
    await delayed.promise;
    await route.fulfill({ status: 404, json: { error: "conversation_not_found" } });
  }, { times: 1 });
  try {
    const checking = page.waitForRequest(detailUrl);
    await chat.click();
    const request = await checking;
    const canceled = page.waitForEvent("requestfailed", incoming => incoming === request);
    await knowledge.click();
    await canceled;
    assert.equal(new URL(page.url()).pathname, "/knowledge.html");
    assert.equal(await chat.getAttribute("href"), chatUrl(promoted));
  } finally {
    delayed.resolve();
  }

  await remove(promoted);
  await chat.click();
  await waitForChat();
  await settings.click();
  assert.equal(await chat.getAttribute("href"), chatUrl());
  await remove(first);
  await remove(second);
  console.log("Chat rail return, blank drafts, first submission, and deleted conversations passed.");
}
