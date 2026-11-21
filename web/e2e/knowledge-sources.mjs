import assert from "node:assert/strict";

async function waitUntil(check, message) {
  const deadline = Date.now() + 15_000;
  while (Date.now() < deadline) {
    if (await check()) return;
    await new Promise(resolve => setTimeout(resolve, 200));
  }
  assert.fail(message);
}

export async function checkKnowledgeSources(page, api) {
  const library = "/api/knowledge/documents";
  const uploaded = await api.post(library, {
    headers: { "content-type": "text/markdown", "x-cairn-filename": "A2UI-backup.md" },
    data: "# Backup\n\n数据库备份必须在停机前完成。A2UICitationFixture",
  });
  assert(uploaded.ok());
  const { document } = await uploaded.json();
  const documentPath = library + "/" + document.document_id;

  await page.goto(new URL("/chat.html", page.url()).href);
  await waitUntil(async () => {
    const response = await api.get(documentPath);
    return response.ok() && (await response.json()).document.status === "ready";
  }, "Fixture document did not become ready");
  await page.getByRole("textbox", { name: "消息" }).fill("A2UI browser knowledge fixture");
  await page.getByRole("button", { name: "发送", exact: true }).click();

  const card = page.locator("a2ui-react-knowledgesources").getByRole("region", { name: "知识库检索来源" });
  const open = card.getByRole("button", { name: "查看片段：A2UI-backup.md" });
  await open.waitFor();
  assert.equal(await card.count(), 1);
  await page.reload();
  await open.waitFor();
  assert.equal(await card.count(), 1);
  await open.focus();
  await open.press("Enter");
  const preview = page.getByRole("dialog", { name: "A2UI-backup.md" });
  await preview.getByText("A2UICitationFixture", { exact: false }).waitFor();
  await page.keyboard.press("Escape");
  await preview.waitFor({ state: "hidden" });
  assert(await open.evaluate(button => document.activeElement === button), "Closing the preview restores focus");
  await open.click();
  await preview.getByRole("button", { name: "用此片段追问" }).click();
  const composer = page.getByRole("textbox", { name: "消息" });
  assert(await composer.evaluate(input => document.activeElement === input));
  assert((await composer.inputValue()).includes("请解释"));
  await page.getByRole("button", { name: "发送", exact: true }).click();

  const id = new URL(page.url()).searchParams.get("conversation");
  const detail = "/api/runtime/conversations/" + id;
  await waitUntil(async () => {
    const response = await api.get(detail);
    if (!response.ok()) return false;
    const { runs } = await response.json();
    return runs.length === 2 && runs[1].status === "completed";
  }, "Follow-up did not complete as one new run");
  const { runs } = await (await api.get(detail)).json();
  assert(runs[1].prompt.includes(document.document_id));
  assert(runs[1].prompt.includes("chunk_id="));

  const renderer = "**/assets/KnowledgeSurface-*.js";
  await page.route(renderer, route => route.abort());
  await page.reload();
  await page.getByRole("region", { name: "知识库检索来源" }).getByRole("button", { name: "查看片段：A2UI-backup.md" }).waitFor();
  assert.equal(await card.count(), 0, "Native source list works without the A2UI bundle");
  await page.unroute(renderer);

  assert((await api.delete(documentPath)).ok());
  await page.reload();
  await card.getByText("来源已不可用").waitFor();
  assert.equal(await open.count(), 0);
  console.log("A2UI knowledge source, reload, preview, follow-up and deletion passed.");
}
