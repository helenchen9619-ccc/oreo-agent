// 飞书内嵌 webview 检测：UA 里带 Lark / Feishu 标识（飞书客户端与 Lark 客户端共用一个内核）。
// 用于登录/授权链接场景——webview 会静默拦截 open.feishu.cn 的整页跳转，需改走「浏览器打开」引导。
export function isFeishuWebview() {
  if (typeof window === 'undefined' || !window.navigator) return false
  return /Lark|Feishu/i.test(window.navigator.userAgent)
}
