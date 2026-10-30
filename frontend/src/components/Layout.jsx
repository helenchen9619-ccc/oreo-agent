import { Activity, Archive, BarChart3, Bot, KeyRound, LogOut, Megaphone, Menu, MessageSquare, PanelLeftClose, PanelLeftOpen, Puzzle, ShieldCheck, Store, Wrench, X } from 'lucide-react'
import { useEffect, useState } from 'react'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { api, clearSession, getUser } from '@/api'
import Logo from '@/components/Logo'
import { Avatar, AvatarFallback } from '@/components/ui/avatar'
import { Separator } from '@/components/ui/separator'
import { Switch } from '@/components/ui/switch'
import { useIsMobile } from '@/lib/useIsMobile'
import { cn } from '@/lib/utils'

// 模块 → 导航项（module '*' = 全员可见；module null = 仅 admin 角色可见；其余按用户模块授权）
const NAV = [
  { to: '/', label: '对话', icon: MessageSquare, module: 'chat', end: true },
  { to: '/market', label: '市场', icon: Store, module: 'chat' },
  { to: '/changelog', label: '更新公告', icon: Megaphone, module: '*' },
  { to: '/admin/agents', label: '数字人管理', icon: Bot, module: 'agents_admin' },
  { to: '/admin/skills', label: '技能库管理', icon: Puzzle, module: 'skills_admin' },
  { to: '/admin/gateway', label: '模型网关', icon: KeyRound, module: 'gateway_admin' },
  { to: '/admin/stats', label: '用量监控', icon: BarChart3, module: 'stats' },
  { to: '/admin/ops', label: '运行中心', icon: Activity, module: 'ops' },
  { to: '/admin/roles', label: '组织与权限', icon: ShieldCheck, module: null },
  { to: '/admin/wecom', label: '企微存档', icon: Archive, module: null },  // M31：admin；M31.1：授权查看人也可见（只见消息 tab）
]

// 侧栏内容：桌面 aside 与移动端抽屉共用（collapsed 仅桌面生效；toggle 图标由调用方给）
function SidebarBody({ user, items, collapsed, toggleIcon, onToggle, onNavClick, onLogout, maintenance, onToggleMaintenance, annUnread }) {
  return (
    <>
      <div className={cn('flex items-center', collapsed ? 'flex-col gap-2 px-2 py-4' : 'justify-between px-4 py-5')}>
        <Logo collapsed={collapsed} />
        <button
          onClick={onToggle}
          title={collapsed ? '展开菜单' : '收起菜单'}
          className="rounded-md p-1.5 text-muted-foreground hover:bg-accent hover:text-foreground"
        >
          {toggleIcon}
        </button>
      </div>
      <Separator />
      <nav className={cn('flex-1 space-y-1 overflow-y-auto', collapsed ? 'p-2' : 'p-3')}>
        {items.map(({ to, label, icon: Icon, end }) => (
          <NavLink
            key={to}
            to={to}
            end={end}
            onClick={onNavClick}
            title={collapsed ? label : undefined}
            className={({ isActive }) =>
              cn(
                'flex items-center rounded-xl text-sm transition-colors',
                collapsed ? 'justify-center px-2 py-2' : 'gap-2.5 px-3 py-2',
                isActive
                  ? 'bg-black font-medium text-white dark:bg-white dark:text-black'
                  : 'text-muted-foreground hover:bg-gray-100 hover:text-foreground dark:hover:bg-gray-800',
              )
            }
          >
            {/* M27：更新公告未读 NEW 标签——展开态红色徽章，折叠态图标右上角红点 */}
            {to === '/changelog' && annUnread ? (
              <span className="relative shrink-0">
                <Icon className="h-4 w-4" />
                {collapsed && <span className="absolute -right-1 -top-1 h-2 w-2 rounded-full bg-red-500" />}
              </span>
            ) : (
              <Icon className="h-4 w-4 shrink-0" />
            )}
            {!collapsed && label}
            {!collapsed && to === '/changelog' && annUnread && (
              <span className="ml-auto rounded bg-red-500 px-1.5 py-px text-[10px] font-medium leading-4 text-white">NEW</span>
            )}
          </NavLink>
        ))}
      </nav>
      {/* M15 维护模式开关（仅管理员）：开启后员工端整屏「系统更新升级中」，管理员照常可用 */}
      {user.role === 'admin' && (
        <>
          <Separator />
          <div className={cn('flex items-center', collapsed ? 'justify-center p-2' : 'gap-2.5 px-3 py-2')} title={collapsed ? `维护模式：${maintenance ? '开启中' : '关闭'}` : undefined}>
            {collapsed ? (
              <button
                onClick={() => onToggleMaintenance(!maintenance)}
                className="rounded-md p-1.5 hover:bg-accent"
              >
                <Wrench className={cn('h-4 w-4', maintenance ? 'text-amber-600' : 'text-muted-foreground')} />
              </button>
            ) : (
              <>
                <Wrench className={cn('h-4 w-4 shrink-0', maintenance ? 'text-amber-600' : 'text-muted-foreground')} />
                <span className={cn('flex-1 text-xs', maintenance ? 'font-medium text-amber-700 dark:text-amber-400' : 'text-muted-foreground')}>
                  维护模式{maintenance ? '开启中' : ''}
                </span>
                <Switch checked={maintenance} onCheckedChange={onToggleMaintenance} className="scale-90" />
              </>
            )}
          </div>
        </>
      )}
      <Separator />
      <div className={cn('flex items-center', collapsed ? 'flex-col gap-1 p-2' : 'gap-2.5 p-3')}>
        <Avatar className="h-8 w-8">
          <AvatarFallback>{(user.name || 'U')[0].toUpperCase()}</AvatarFallback>
        </Avatar>
        {!collapsed && (
          <div className="min-w-0 flex-1">
            <div className="truncate text-sm font-medium">{user.name}</div>
            <div className="text-xs text-muted-foreground">{user.role === 'admin' ? '管理员' : '员工'}</div>
          </div>
        )}
        <button onClick={onLogout} title="退出登录" className="rounded-md p-1.5 text-muted-foreground hover:bg-accent hover:text-foreground">
          <LogOut className="h-4 w-4" />
        </button>
      </div>
    </>
  )
}

export default function Layout() {
  const user = getUser() || {}
  const navigate = useNavigate()
  const location = useLocation()
  const isMobile = useIsMobile()
  const modules = user.modules || []
  const isAdmin = user.role === 'admin'
  // hooks 必须先于一切渲染期计算声明（2026-09-23 白屏事故：wecomViewer 声明在 NAV.filter 之后 → 非 admin TDZ 必崩）
  const [collapsed, setCollapsed] = useState(false) // 侧栏可折叠（屏幕留给对话/预览）
  const [drawerOpen, setDrawerOpen] = useState(false) // 移动端侧栏抽屉
  const [maintenance, setMaintenance] = useState(false) // M15 维护模式
  const [annUnread, setAnnUnread] = useState(false) // M27 公告未读 NEW 标签
  const [wecomViewer, setWecomViewer] = useState(false) // M31.1 企微存档授权查看人（非 admin 也能看消息 tab）
  const items = NAV.filter((n) =>
    n.module === '*' ? true : n.module ? modules.includes(n.module) : isAdmin || (n.to === '/admin/wecom' && wecomViewer),
  )

  // M31.1：非 admin 挂载时查一次企微存档查看授权，命中则侧栏显示「企微存档」入口
  useEffect(() => {
    if (isAdmin) return
    api('/admin/wecom/access').then((d) => setWecomViewer(!!d.viewer)).catch(() => {})
  }, [isAdmin])

  // M27：挂载即查 + 60s 轮询公告未读状态；ChangelogPage 阅读后派事件即时摘除
  useEffect(() => {
    let alive = true
    const check = () => api('/announcements/status').then((d) => { if (alive) setAnnUnread(!!d.unread) }).catch(() => {})
    check()
    const t = setInterval(check, 60000)
    const onRead = () => setAnnUnread(false)
    window.addEventListener('ea-announcement-read', onRead)
    return () => { alive = false; clearInterval(t); window.removeEventListener('ea-announcement-read', onRead) }
  }, [])

  // M15：挂载即查 + 轮询（维护期间加密到 10s 让员工端快速恢复）+ api.js 的 503 事件即时感知
  useEffect(() => {
    let alive = true
    const check = () => api('/system/status').then((d) => { if (alive) setMaintenance(!!d.maintenance) }).catch(() => {})
    check()
    const t = setInterval(check, maintenance ? 10000 : 30000)
    const onEvt = () => setMaintenance(true)
    window.addEventListener('ea-maintenance', onEvt)
    return () => { alive = false; clearInterval(t); window.removeEventListener('ea-maintenance', onEvt) }
  }, [maintenance])

  // 路由变化即收起移动端抽屉
  useEffect(() => {
    setDrawerOpen(false)
  }, [location.pathname])

  const logout = () => {
    clearSession()
    navigate('/login')
  }

  const toggleMaintenance = async (v) => {
    try {
      await api('/admin/system/maintenance', { method: 'PUT', body: { enabled: v } })
      setMaintenance(v)
      toast.success(v ? '维护模式已开启：员工端显示「系统更新升级中」' : '维护模式已关闭：员工端已恢复访问')
    } catch (e) {
      toast.error(e.message)
    }
  }

  // M15：维护期间员工整屏升级页（管理员不受影响，可照常测试）
  if (maintenance && !isAdmin) {
    return (
      <div className="flex h-dvh flex-col items-center justify-center bg-background px-6 text-center">
        <Logo />
        <h1 className="mt-8 text-xl font-semibold tracking-tight">系统更新升级中</h1>
        <p className="mt-3 max-w-sm text-sm leading-relaxed text-muted-foreground">
          IT 信息部正在更新平台，通常几分钟内完成。本页会自动检测恢复，无需手动刷新。
        </p>
      </div>
    )
  }

  return (
    <div className="flex h-dvh bg-background">
      {/* 桌面侧栏（移动端隐藏，改抽屉） */}
      <aside className={cn('hidden flex-col border-r bg-card transition-all md:flex', collapsed ? 'w-14' : 'w-56')}>
        <SidebarBody
          user={user}
          items={items}
          collapsed={collapsed}
          toggleIcon={collapsed ? <PanelLeftOpen className="h-4 w-4" /> : <PanelLeftClose className="h-4 w-4" />}
          onToggle={() => setCollapsed((v) => !v)}
          onLogout={logout}
          maintenance={maintenance}
          onToggleMaintenance={toggleMaintenance}
          annUnread={annUnread}
        />
      </aside>
      {/* 移动端抽屉 */}
      {isMobile && drawerOpen && (
        <div className="fixed inset-0 z-50 md:hidden">
          <div className="absolute inset-0 bg-black/40" onClick={() => setDrawerOpen(false)} />
          <aside className="absolute left-0 top-0 flex h-full w-64 flex-col border-r bg-card shadow-xl">
            <SidebarBody
              user={user}
              items={items}
              collapsed={false}
              toggleIcon={<X className="h-4 w-4" />}
              onToggle={() => setDrawerOpen(false)}
              onNavClick={() => setDrawerOpen(false)}
              onLogout={logout}
              maintenance={maintenance}
              onToggleMaintenance={toggleMaintenance}
              annUnread={annUnread}
            />
          </aside>
        </div>
      )}
      <div className="flex min-w-0 flex-1 flex-col">
        {/* M15 管理员提醒横幅：维护模式开启时常驻，防忘记关 */}
        {isAdmin && maintenance && (
          <div className="flex items-center justify-center gap-1.5 bg-amber-100 px-3 py-1.5 text-center text-xs text-amber-900 dark:bg-amber-950/60 dark:text-amber-200">
            <Wrench className="h-3.5 w-3.5 shrink-0" />
            维护模式开启中：员工端显示「系统更新升级中」，你的访问不受影响。测试完请在侧栏关闭。
          </div>
        )}
        {/* 移动端顶栏（汉堡开抽屉） */}
        <div className="flex items-center gap-2 border-b bg-card px-3 py-2 md:hidden">
          <button
            onClick={() => setDrawerOpen(true)}
            title="打开菜单"
            className="rounded-md p-1.5 text-muted-foreground hover:bg-accent hover:text-foreground"
          >
            <Menu className="h-5 w-5" />
          </button>
          <Logo collapsed={false} />
        </div>
        <main className="min-h-0 min-w-0 flex-1 overflow-hidden">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
