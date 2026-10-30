const TOKEN_KEY = 'ea_token'
const USER_KEY = 'ea_user'

export function getToken() { return localStorage.getItem(TOKEN_KEY) }
export function getUser() { return JSON.parse(localStorage.getItem(USER_KEY) || 'null') }
export function saveSession(token, user) {
  localStorage.setItem(TOKEN_KEY, token)
  localStorage.setItem(USER_KEY, JSON.stringify(user))
}
export function clearSession() {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
}

/** 统一 fetch 封装：自动带 token，错误抛 Error(detail)。 */
export async function api(path, { method = 'GET', body, formData } = {}) {
  const headers = {}
  const token = getToken()
  if (token) headers.Authorization = `Bearer ${token}`
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  const resp = await fetch(`/api${path}`, {
    method,
    headers,
    body: formData ?? (body !== undefined ? JSON.stringify(body) : undefined),
  })
  if (resp.status === 401) {
    clearSession()
    // 已在登录页就别再跳：后端异常/被抢端口返回 401 时，重复 href 会造成整页无限重载（闪屏）
    if (!window.location.pathname.startsWith('/login')) window.location.href = '/login'
    throw new Error('登录已失效')
  }
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}))
    // M15 维护模式：后端 503 + detail.maintenance → 通知 Layout 整屏显示升级页（detail 是对象，message 取 msg）
    if (resp.status === 503 && err.detail?.maintenance) {
      window.dispatchEvent(new Event('ea-maintenance'))
    }
    const detailText = typeof err.detail === 'object' && err.detail !== null ? err.detail.msg : err.detail
    const e = new Error(detailText || `请求失败 (${resp.status})`)
    e.status = resp.status
    throw e
  }
  if (resp.status === 204) return null
  return resp.json()
}

/** SSE 流式对话。事件：onDelta / onTool / onDone / onError / onAborted / onRetrying（M35 模型过载自动重试中）；传 signal 可中止（停止按钮） */
export async function streamChat(conversationId, payload, { onDelta, onTool, onDone, onError, onAborted, onRetrying, signal }) {
  try {
    const resp = await fetch(`/api/chat/conversations/${conversationId}/messages`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${getToken()}` },
      body: JSON.stringify(payload),
      signal,
    })
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}))
      if (resp.status === 503 && err.detail?.maintenance) window.dispatchEvent(new Event('ea-maintenance'))
      const detailText = typeof err.detail === 'object' && err.detail !== null ? err.detail.msg : err.detail
      const e = new Error(detailText || `请求失败 (${resp.status})`)
      e.status = resp.status
      onError(e)
      return
    }
    const reader = resp.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const events = buffer.split('\n\n')
      buffer = events.pop() || ''
      for (const ev of events) {
        // 一个事件块可能有多行（注释/心跳/data）；逐行取 data:，其余忽略，坏行跳过不杀流
        for (const line of ev.split('\n')) {
          const t = line.trim()
          if (!t.startsWith('data: ')) continue
          const payload = t.slice(6)
          if (payload === '[DONE]') continue
          let data
          try {
            data = JSON.parse(payload)
          } catch {
            continue
          }
          if (data.delta) onDelta(data.delta)
          else if (data.retrying) onRetrying?.(data.retrying)
          else if (data.tool) onTool?.(data.tool)
          else if (data.done) onDone(data.done)
          else if (data.error) onError(new Error(data.error))
        }
      }
    }
  } catch (e) {
    if (e?.name === 'AbortError') {
      onAborted?.()
      return
    }
    onError(e)
  }
}
