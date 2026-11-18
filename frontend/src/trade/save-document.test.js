import assert from "node:assert/strict"
import test from "node:test"
import { saveTradeDocument } from "./save-document.js"

test("설정값 확인은 원래 요청을 한 번만 재제출하고 취소·다른 오류는 저장하지 않는다", async (t) => {
  const previousWindow = globalThis.window
  t.after(() => {
    if (previousWindow === undefined) delete globalThis.window
    else globalThis.window = previousWindow
  })
  const requests = [], prompts = [], replies = []
  let confirmed = true
  globalThis.window = { confirm: (message) => { prompts.push(message); return confirmed } }
  t.mock.method(globalThis, "fetch", async (url, options) => {
    requests.push({ url, ...options, body: JSON.parse(options.body) })
    return replies.shift()
  })
  const values = { "seller.legal_name": "Different Trading Co" }
  const confirmation = () => new Response(JSON.stringify({ error: {
    code: "SETTINGS_CONFIRMATION_REQUIRED", message: "입력한 회사명을 이 문서에 사용할까요?",
    details: { confirmed_settings: values },
  } }), { status: 409 })
  const document = { seller: { legal_name: "Different Trading Co" },
    confirmed_settings: { "buyer.address": "old confirmation" } }
  const options = { token: "test-token", signal: new AbortController().signal, method: "POST" }

  // 신규 문서, 일반 수정, 검토 중 수정은 같은 확인 경로를 사용한다.
  for (const body of [document, { revision: 3, document },
    { action: "modify", revision: 3, content_hash: "same-review-hash", document }]) {
    replies.push(confirmation(), new Response(JSON.stringify({ document_id: 7 })))
    assert.deepEqual(await saveTradeDocument("/trade/pi", { ...options, body }), { document_id: 7 })
    const [first, retry] = requests.slice(-2)
    const expected = { ...document, confirmed_settings: values }
    assert.deepEqual(first.body, body)
    assert.deepEqual(retry.body, body.document ? { ...body, document: expected } : expected)
    assert.equal(retry.signal, options.signal)
    assert.equal(retry.method, first.method)
    assert.deepEqual(retry.headers, first.headers)
  }
  assert.deepEqual(document.confirmed_settings, { "buyer.address": "old confirmation" })
  assert.equal(prompts.length, 3)

  confirmed = false
  replies.push(confirmation())
  let count = requests.length
  assert.equal(await saveTradeDocument("/trade/pi", { ...options, body: document }), null)
  assert.equal(requests.length, count + 1)

  confirmed = true
  replies.push(new Response(JSON.stringify({ error: { code: "REVISION_CONFLICT", message: "stale" } }), { status: 409 }))
  count = requests.length
  const promptCount = prompts.length
  await assert.rejects(saveTradeDocument("/trade/pi", { ...options, body: document }), { code: "REVISION_CONFLICT" })
  assert.equal(requests.length, count + 1)
  assert.equal(prompts.length, promptCount)

  // 확인 중 설정이 다시 바뀌어도 무한 재시도하거나 자동 승인하지 않는다.
  replies.push(confirmation(), confirmation())
  count = requests.length
  await assert.rejects(saveTradeDocument("/trade/pi", { ...options, body: document }), { code: "SETTINGS_CONFIRMATION_REQUIRED" })
  assert.equal(requests.length, count + 2)
  assert.equal(prompts.length, promptCount + 1)
})
