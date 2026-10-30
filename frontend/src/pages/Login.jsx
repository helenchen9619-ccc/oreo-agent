import { useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { toast } from 'sonner'
import { api, getToken, saveSession } from '@/api'
import Logo from '@/components/Logo'
import { isFeishuWebview } from '@/lib/feishuWebview'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

export default function Login() {
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const [config, setConfig] = useState(null)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [loading, setLoading] = useState(false)
  // M14b：飞书为主时，账号密码表单默认收起，底部小字切换
  const [showPassword, setShowPassword] = useState(false)
  // M8d：飞书内嵌 webview 会拦截 OAuth 整页跳转，命中时改展示「请在浏览器打开」引导
  const [webviewGuide, setWebviewGuide] = useState(false)

  // 登录成功后统一走这里：存 token → 拉 /me（含 modules）→ 进首页
  const finishLogin = async (token) => {
    saveSession(token, {})
    const me = await api('/auth/me')
    saveSession(token, me)
    navigate('/', { replace: true })
  }

  useEffect(() => {
    // 飞书 OAuth 回调优先：/login?code=xxx（upgrade 增量授权时用户已登录，不能因 getToken() 早退而漏掉 code 交换）
    const code = params.get('code')
    if (code) {
      setLoading(true)
      api('/auth/feishu/callback', { method: 'POST', body: { code } })
        .then((data) => finishLogin(data.token))
        .catch((e) => toast.error(e.message))
        .finally(() => setLoading(false))
      return
    }
    if (getToken()) {
      // 已登录 + ?upgrade=1（M7 飞书数据授权）：直接跳飞书 consent 补 scope，回调后透明重登
      if (params.get('upgrade') === '1') {
        feishuLogin(true)
        return
      }
      navigate('/', { replace: true })
      return
    }
    api('/auth/config').then(setConfig).catch(() => {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [navigate, params])

  const devLogin = async (e) => {
    e.preventDefault()
    setLoading(true)
    try {
      const data = await api('/auth/dev-login', { method: 'POST', body: { username, password } })
      await finishLogin(data.token)
    } catch (err) {
      toast.error(err.message)
    } finally {
      setLoading(false)
    }
  }

  // M14b 账号密码登录（测试账号/非飞书场景；密码由管理员在建号时设置）
  const passwordLogin = async (e) => {
    e.preventDefault()
    setLoading(true)
    try {
      const data = await api('/auth/password-login', { method: 'POST', body: { username, password } })
      await finishLogin(data.token)
    } catch (err) {
      toast.error(err.message)
    } finally {
      setLoading(false)
    }
  }

  const feishuLogin = async (upgrade = false) => {
    // M8d：飞书内嵌 webview 会静默拦截 open.feishu.cn 的**增量授权**整页跳转（点公告 /login?upgrade=1
    // 没反应）。只有 upgrade 走这条拦截改引导；普通飞书登录的跳转在 webview 里能正常工作，直接跳。
    if (upgrade && isFeishuWebview()) {
      setWebviewGuide(true)
      return
    }
    try {
      // 回调落点固定是本站 /login（上方 useEffect 处理 ?code=）；origin 随环境自动适配（本地/云上同一份代码）
      // upgrade=1（M7 增量授权）：显式申请数据 scope，否则飞书只授予最小身份权限
      const redirectUri = encodeURIComponent(`${window.location.origin}/login`)
      const data = await api(`/auth/feishu/authorize-url?redirect_uri=${redirectUri}${upgrade ? '&upgrade=1' : ''}`)
      window.location.href = data.url
    } catch (err) {
      toast.error(err.message)
    }
  }

  // M8d：引导里复制的是本站登录页地址（含 ?upgrade=1 等参数），浏览器里打开即走正常授权流程
  const oreoLoginUrl = () => `${window.location.origin}/login${window.location.search}`
  const copyOreoUrl = async () => {
    try {
      await navigator.clipboard.writeText(oreoLoginUrl())
      toast.success('链接已复制，请粘贴到浏览器打开')
    } catch {
      toast.error('复制失败，请长按下方链接手动复制')
    }
  }

  return (
    <div className="flex min-h-dvh items-center justify-center bg-muted/40 px-4">
      <Card className="w-full max-w-[380px] rounded-2xl">
        <CardHeader className="items-center text-center">
          <Logo />
          <CardDescription className="pt-2">由 IT 信息部统一管理</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          {webviewGuide ? (
            <div className="space-y-3 text-sm">
              <p className="font-medium">无法在飞书内完成授权</p>
              <p className="text-muted-foreground">飞书内打开会拦截授权跳转，请改用浏览器打开 OREO 后再完成授权。</p>
              <p className="text-muted-foreground">飞书 PC 端可点窗口右上角「···」→「在浏览器打开」。</p>
              <div className="break-all rounded-md border bg-muted/40 p-2 text-xs text-muted-foreground">{oreoLoginUrl()}</div>
              <Button className="w-full" onClick={copyOreoUrl}>复制链接</Button>
              <Button variant="outline" className="w-full" onClick={() => navigate('/', { replace: true })}>返回</Button>
            </div>
          ) : (
            <>
          {config?.feishu_enabled && !showPassword && (
            <>
              <Button className="w-full" onClick={() => feishuLogin()} disabled={loading}>
                飞书登录
              </Button>
              {config?.password_login_enabled && (
                <div className="text-center">
                  <button type="button" className="text-xs text-muted-foreground underline-offset-4 hover:underline"
                    onClick={() => setShowPassword(true)}>
                    使用账号密码登录
                  </button>
                </div>
              )}
            </>
          )}
          {config?.password_login_enabled && (showPassword || !config?.feishu_enabled) && (
            <form onSubmit={passwordLogin} className="space-y-3">
              {(config?.feishu_enabled || config?.dev_login_enabled) && (
                <div className="flex items-center gap-2 text-xs text-muted-foreground">
                  <div className="h-px flex-1 bg-border" />
                  账号密码登录
                  <div className="h-px flex-1 bg-border" />
                </div>
              )}
              <div className="space-y-1.5">
                <Label htmlFor="pwd-username">用户名</Label>
                <Input id="pwd-username" value={username} onChange={(e) => setUsername(e.target.value)} autoFocus autoComplete="username" />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="pwd-password">密码</Label>
                <Input id="pwd-password" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" />
              </div>
              <Button type="submit" variant={config?.feishu_enabled ? 'outline' : 'default'} className="w-full" disabled={loading}>
                {loading ? '登录中…' : '登录'}
              </Button>
              {config?.feishu_enabled && (
                <div className="text-center">
                  <button type="button" className="text-xs text-muted-foreground underline-offset-4 hover:underline"
                    onClick={() => setShowPassword(false)}>
                    返回飞书登录
                  </button>
                </div>
              )}
            </form>
          )}
          {config?.dev_login_enabled && (
            <form onSubmit={devLogin} className="space-y-3">
              {(config?.feishu_enabled || config?.password_login_enabled) && (
                <div className="flex items-center gap-2 text-xs text-muted-foreground">
                  <div className="h-px flex-1 bg-border" />
                  开发登录（任意用户名）
                  <div className="h-px flex-1 bg-border" />
                </div>
              )}
              <div className="space-y-1.5">
                <Label htmlFor="username">用户名</Label>
                <Input id="username" value={username} onChange={(e) => setUsername(e.target.value)} autoFocus />
              </div>
              <Button type="submit" variant={config?.feishu_enabled ? 'outline' : 'default'} className="w-full" disabled={loading}>
                {loading ? '登录中…' : '登录'}
              </Button>
            </form>
          )}
            </>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
