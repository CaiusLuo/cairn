import assert from "node:assert/strict";
import test from "node:test";

import { parseCurl } from "../src/curl-import.ts";

test("parses a Bailian OpenAI-compatible curl", () => {
  const parsed = parseCurl(`curl --location --request POST 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions' \\
--header 'Authorization: Bearer sk-test-key' \\
--header 'Content-Type: application/json' \\
--data-raw '{
  "model": "kimi-k3.1-pro",
  "messages": [{"role": "user", "content": "hi"}]
}'`);

  assert.equal(parsed.error, null);
  assert.equal(parsed.baseUrl, "https://dashscope.aliyuncs.com/compatible-mode/v1");
  assert.equal(parsed.endpoint, "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions");
  assert.deepEqual(parsed.modelIds, ["kimi-k3.1-pro"]);
  assert.equal(parsed.apiKey, "sk-test-key");
  assert.equal(parsed.providerName, "dashscope");
});

test("parses double-quoted JSON with escaped quotes", () => {
  const parsed = parseCurl(
    'curl https://api.deepseek.com/chat/completions -H "Authorization: Bearer sk-deep" -H "Content-Type: application/json" -d "{\\"model\\": \\"deepseek-chat\\", \\"messages\\": []}"',
  );

  assert.equal(parsed.baseUrl, "https://api.deepseek.com");
  assert.deepEqual(parsed.modelIds, ["deepseek-chat"]);
  assert.equal(parsed.apiKey, "sk-deep");
});

test("accepts = separated flags and reports a placeholder key", () => {
  const parsed = parseCurl(
    `curl --url=https://open.bigmodel.cn/api/paas/v4/chat/completions --header='Authorization: Bearer $ZHIPU_API_KEY' --header='Content-Type: application/json' --data-raw='{"model":"glm-4-plus"}'`,
  );

  assert.equal(parsed.baseUrl, "https://open.bigmodel.cn/api/paas/v4");
  assert.deepEqual(parsed.modelIds, ["glm-4-plus"]);
  assert.equal(parsed.apiKey, null);
  assert(parsed.warnings.some(item => item.includes("占位符")));
});

test("reads the model from a query parameter and keeps a base URL", () => {
  const parsed = parseCurl(
    "curl 'https://api.example.com/v1/chat/completions?model=custom-model' -H 'api-key: dummy-query-key' -d '{}'",
  );

  assert.equal(parsed.baseUrl, "https://api.example.com/v1");
  assert.deepEqual(parsed.modelIds, ["custom-model"]);
  assert.equal(parsed.apiKey, "dummy-query-key");
});

test("reads an already stripped base URL", () => {
  const parsed = parseCurl(
    "curl https://api.moonshot.cn/v1 -H 'Authorization: Bearer sk-moon'",
  );

  assert.equal(parsed.baseUrl, "https://api.moonshot.cn/v1");
  assert.deepEqual(parsed.modelIds, []);
  assert.equal(parsed.apiKey, "sk-moon");
});

test("rejects an Azure-only endpoint but still extracts its deployment", () => {
  const parsed = parseCurl(
    "curl 'https://resource.openai.azure.com/openai/deployments/my-deployment/chat/completions?api-version=2024-10-21' -H 'api-key: fake-azure-key' -d '{}'",
  );

  assert.equal(parsed.baseUrl, null);
  assert.deepEqual(parsed.modelIds, ["my-deployment"]);
  assert(parsed.warnings.some(item => item.includes("Azure")));
});

test("maps the native Bailian endpoint to its OpenAI-compatible base", () => {
  const parsed = parseCurl(`curl 'https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation' \\
-H 'Authorization: Bearer sk-bailian' \\
-d '{"model":"kimi-k3.1-pro","input":{"messages":[]}}'`);

  assert.equal(
    parsed.baseUrl,
    "https://dashscope.aliyuncs.com/compatible-mode/v1",
  );
  assert.deepEqual(parsed.modelIds, ["kimi-k3.1-pro"]);
  assert(parsed.warnings.some(item => item.includes("百炼原生接口")));
});

test("reports a missing request URL", () => {
  const parsed = parseCurl("curl -H 'Authorization: Bearer sk-test'");

  assert(parsed.error?.includes("URL"));
});

test("reports a malformed request URL", () => {
  const parsed = parseCurl("curl https:// example.com");

  assert(parsed.error?.includes("URL"));
});
