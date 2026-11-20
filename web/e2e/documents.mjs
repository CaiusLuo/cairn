import assert from "node:assert/strict";

const library = "/api/knowledge/documents";

export async function checkDocumentLibrary(page, api) {
  const ids = [];
  for (let index = 0; index <= 50; index++) {
    const response = await api.post(library, {
      headers: {
        "content-type": "text/plain",
        "x-cairn-filename": "doc-browser-" + index + ".txt",
      },
      data: "Fixture document body " + index,
    });
    assert(response.ok(), "Could not seed the document library");
    ids.push((await response.json()).document.document_id);
  }

  await page.getByRole("button", { name: "文档", exact: true }).click();
  await page.getByRole("button", { name: "下一页", exact: true }).click();
  await page.getByRole("button", { name: /doc-browser-0\.txt/ }).waitFor();
  await page.getByRole("button", { name: "上一页", exact: true }).click();

  const first = page.getByRole("button", { name: /doc-browser-50\.txt.*可搜索/ });
  const second = page.getByRole("button", { name: /doc-browser-49\.txt.*可搜索/ });
  await first.waitFor();
  await second.waitFor();

  const delayedText = "**" + library + "/" + ids[50] + "/text?*";
  const release = Promise.withResolvers();
  const finished = Promise.withResolvers();
  await page.route(delayedText, async route => {
    try {
      const response = await route.fetch();
      await release.promise;
      await route.fulfill({ response });
    } catch {
      // Closing the drawer can cancel the intercepted request.
    } finally {
      finished.resolve();
    }
  });
  try {
    const requested = page.waitForRequest(delayedText);
    await first.click();
    await requested;
    await page.getByRole("button", { name: "关闭抽屉" }).click();
    await second.click();
    const preview = page.getByRole("dialog", { name: "doc-browser-49.txt" });
    await preview.getByText("Fixture document body 49", { exact: true }).waitFor();
    release.resolve();
    await finished.promise;
    assert.equal(await preview.locator("pre").textContent(), "Fixture document body 49");
  } finally {
    release.resolve();
    await page.unroute(delayedText);
  }

  await page.getByRole("button", { name: "关闭抽屉" }).click();
  console.log("Document library pagination and preview isolation passed.");
}
