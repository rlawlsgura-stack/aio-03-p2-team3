import { apiFetch } from "../api.js"

// 서버가 판정한 현재 입력값만 확인하며, 취소하면 저장과 화면 전환을 하지 않는다.
export async function saveTradeDocument(path, options) {
  try {
    return await apiFetch(path, options)
  } catch (error) {
    if (error.code !== "SETTINGS_CONFIRMATION_REQUIRED" || !error.details?.confirmed_settings || options.signal?.aborted) throw error
    if (!window.confirm(error.message)) return null
    const original = options.body.document ?? options.body
    const document = { ...original, confirmed_settings: error.details.confirmed_settings }
    return apiFetch(path, { ...options,
      body: options.body.document ? { ...options.body, document } : document,
    })
  }
}
