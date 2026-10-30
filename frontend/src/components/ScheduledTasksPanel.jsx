// 定时任务面板（M3.8）：对话页中栏的第二个 tab。调度语义全部在服务端（cron_expr 单一事实源），
// 这里只做 预设↔cron 的字符串映射与人话回显。
import { CalendarClock, Loader2, MoreHorizontal, Pencil, Play, Plus, Send, Trash2 } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import { api } from '@/api'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { Textarea } from '@/components/ui/textarea'
import { confirmDialog } from '@/components/dialogs'

const WEEKDAYS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'] // cron 0=周日
const HOUR_STEPS = [1, 2, 3, 4, 6, 8, 12]

// 预设 → cron 字符串（5 段：分 时 日 月 周）
function buildCron(freq, { time, weekday, day, hours, minute, custom }) {
  const [h = '9', m = '0'] = (time || '09:00').split(':')
  if (freq === 'daily') return `${+m} ${+h} * * *`
  if (freq === 'weekly') return `${+m} ${+h} * * ${weekday}`
  if (freq === 'monthly') return `${+m} ${+h} ${day} * *`
  if (freq === 'hourly') return `${+(minute || '0')} */${hours} * * *`
  return custom.trim()
}

// cron → 人话标签；匹配不到预设模式就原样等宽显示（不丢信息）
function cronToLabel(expr) {
  const p = expr.trim().split(/\s+/)
  if (p.length !== 5) return expr
  const [m, h, d, , w] = p
  const mm = String(m).padStart(2, '0')
  if (/^\d+$/.test(m) && /^\d+$/.test(h) && d === '*' && w === '*') return `每天 ${String(h).padStart(2, '0')}:${mm}`
  if (/^\d+$/.test(m) && /^\d+$/.test(h) && d === '*' && /^\d$/.test(w)) return `每${WEEKDAYS[+w]} ${String(h).padStart(2, '0')}:${mm}`
  if (/^\d+$/.test(m) && /^\d+$/.test(h) && /^\d+$/.test(d) && w === '*') return `每月${+d}号 ${String(h).padStart(2, '0')}:${mm}`
  if (/^\d+$/.test(m) && /^\*\/\d+$/.test(h) && d === '*' && w === '*') return `每${h.slice(2)}小时 (第${+m}分)`
  return expr
}

// cron → 预设表单回填（编辑用）；匹配不到返回 null → 落自定义
function parseCron(expr) {
  const p = expr.trim().split(/\s+/)
  if (p.length !== 5) return null
  const [m, h, d, , w] = p
  const time = `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`
  if (/^\d+$/.test(m) && /^\d+$/.test(h) && d === '*' && w === '*') return { freq: 'daily', time }
  if (/^\d+$/.test(m) && /^\d+$/.test(h) && d === '*' && /^\d$/.test(w)) return { freq: 'weekly', time, weekday: w }
  if (/^\d+$/.test(m) && /^\d+$/.test(h) && /^\d+$/.test(d) && w === '*') return { freq: 'monthly', time, day: d }
  if (/^\d+$/.test(m) && /^\*\/\d+$/.test(h) && d === '*' && w === '*') return { freq: 'hourly', hours: h.slice(2), minute: m }
  return null
}

function fmtNext(iso) {
  if (!iso) return '—'
  const d = new Date(iso)
  const today = new Date()
  const sameDay = d.toDateString() === today.toDateString()
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  return sameDay ? `今天 ${hm}` : `${d.getMonth() + 1}-${d.getDate()} ${hm}`
}

const EMPTY_FORM = {
  name: '', prompt: '', freq: 'daily', time: '09:00', weekday: '1', day: '1', hours: '2', minute: '0', custom: '',
  push_target: 'none', // M4b/M8：none=不推 / me=应用机器人私聊推本人 / group=群机器人 webhook
  feishu_webhook: '', feishu_secret: '',
}

export default function ScheduledTasksPanel({ agentId }) {
  const [tasks, setTasks] = useState([])
  const [dialogOpen, setDialogOpen] = useState(false)
  const [editing, setEditing] = useState(null) // null=新建
  const [form, setForm] = useState(EMPTY_FORM)
  const [preview, setPreview] = useState(null) // 自定义 cron 的未来 3 次预览 / 错误
  const [saving, setSaving] = useState(false)
  const previewTimer = useRef(null)

  const load = useCallback(() => {
    api(`/scheduler/tasks?agent_id=${agentId}`).then(setTasks).catch((e) => toast.error(e.message))
  }, [agentId])

  useEffect(load, [load])
  // 30s 轮询：让"下次运行/上次状态"随调度器推进自动刷新
  useEffect(() => {
    const t = setInterval(load, 30000)
    return () => clearInterval(t)
  }, [load])

  const openCreate = () => {
    setEditing(null)
    setForm(EMPTY_FORM)
    setPreview(null)
    setDialogOpen(true)
  }

  const openEdit = (t) => {
    setEditing(t)
    const parsed = parseCron(t.cron_expr)
    setForm({
      name: t.name,
      prompt: t.prompt,
      freq: parsed?.freq || 'custom',
      time: parsed?.time || '09:00',
      weekday: parsed?.weekday || '1',
      day: parsed?.day || '1',
      hours: parsed?.hours || '2',
      minute: parsed?.minute || '0',
      custom: parsed ? '' : t.cron_expr,
      push_target: t.push_to_me ? 'me' : t.feishu_webhook ? 'group' : 'none',
      feishu_webhook: t.feishu_webhook || '',
      feishu_secret: t.feishu_secret || '',
    })
    setPreview(null)
    setDialogOpen(true)
  }

  // 自定义 cron：防抖预览未来 3 次触发
  useEffect(() => {
    if (!dialogOpen || form.freq !== 'custom' || !form.custom.trim()) {
      setPreview(null)
      return
    }
    clearTimeout(previewTimer.current)
    previewTimer.current = setTimeout(async () => {
      try {
        const r = await api(`/scheduler/cron-preview?expr=${encodeURIComponent(form.custom.trim())}`)
        setPreview({ ok: true, next: r.next })
      } catch (e) {
        setPreview({ ok: false, error: e.message })
      }
    }, 400)
    return () => clearTimeout(previewTimer.current)
  }, [dialogOpen, form.freq, form.custom])

  const save = async () => {
    const cron_expr = buildCron(form.freq, form)
    if (!form.name.trim() || !form.prompt.trim() || !cron_expr) {
      toast.error('请填完整：名称、指令、调度规则')
      return
    }
    setSaving(true)
    try {
      const body = {
        name: form.name.trim(),
        prompt: form.prompt,
        cron_expr,
        push_to_me: form.push_target === 'me',
        // 传 "" 后端即清除（区分"不动"与"清空"）；选了"私聊推给我"则清掉 webhook（二选一）
        feishu_webhook: form.push_target === 'group' ? form.feishu_webhook.trim() : '',
        feishu_secret: form.push_target === 'group' ? form.feishu_secret.trim() : '',
      }
      if (editing) await api(`/scheduler/tasks/${editing.id}`, { method: 'PATCH', body })
      else await api('/scheduler/tasks', { method: 'POST', body: { ...body, agent_id: agentId } })
      toast.success(editing ? '已保存' : '已创建定时任务')
      setDialogOpen(false)
      load()
    } catch (e) {
      toast.error(e.message)
    } finally {
      setSaving(false)
    }
  }

  const toggleEnabled = async (t) => {
    try {
      await api(`/scheduler/tasks/${t.id}`, { method: 'PATCH', body: { enabled: !t.enabled } })
      load()
    } catch (e) {
      toast.error(e.message)
    }
  }

  const runNow = async (t) => {
    try {
      await api(`/scheduler/tasks/${t.id}/run`, { method: 'POST' })
      toast.success(`已触发「${t.name}」，结果将出现在会话列表`)
      load()
    } catch (e) {
      toast.error(e.message)
    }
  }

  const testPush = async (t) => {
    try {
      await api(`/scheduler/tasks/${t.id}/test-push`, { method: 'POST' })
      toast.success('测试消息已推送到飞书群')
      load()
    } catch (e) {
      toast.error(e.message)
      load()
    }
  }

  const remove = async (t) => {
    if (!(await confirmDialog({ title: '删除定时任务', desc: `确定删除「${t.name}」？已产生的会话记录会保留。`, confirmText: '删除', danger: true }))) return
    try {
      await api(`/scheduler/tasks/${t.id}`, { method: 'DELETE' })
      toast.success('已删除')
      load()
    } catch (e) {
      toast.error(e.message)
    }
  }

  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v }))

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex shrink-0 items-center justify-between border-b px-3 py-2.5">
        <span className="text-xs font-medium text-muted-foreground">定时任务</span>
        <Button variant="ghost" size="icon" className="h-7 w-7" title="新建定时任务" onClick={openCreate}>
          <Plus className="h-3.5 w-3.5" />
        </Button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="p-2">
          {tasks.map((t) => (
            <div key={t.id} className="group mb-1 rounded-md px-2 py-1.5 hover:bg-accent/60">
              <div className="flex items-center gap-2">
                <CalendarClock className="h-4 w-4 shrink-0 text-muted-foreground" />
                <span className="min-w-0 flex-1 truncate text-sm" title={t.name}>{t.name}</span>
                <Switch checked={t.enabled} onCheckedChange={() => toggleEnabled(t)} size="sm" title={t.enabled ? '停用' : '启用'} />
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    {/* 必须用原生 button：Button 组件没 forwardRef，Radix asChild 挂不上 ref 菜单打不开 */}
                    <button
                      title="任务设置"
                      className="shrink-0 rounded p-1 text-muted-foreground opacity-100 hover:bg-background hover:text-foreground focus:opacity-100 data-[state=open]:opacity-100 md:opacity-0 md:group-hover:opacity-100 md:focus:opacity-100 md:data-[state=open]:opacity-100"
                    >
                      <MoreHorizontal className="h-3 w-3" />
                    </button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="end" sideOffset={4} className="w-32">
                    <DropdownMenuItem onClick={() => runNow(t)}>
                      <Play className="mr-2 h-3.5 w-3.5" /> 立即运行
                    </DropdownMenuItem>
                    {(t.feishu_webhook || t.push_to_me) && (
                      <DropdownMenuItem onClick={() => testPush(t)}>
                        <Send className="mr-2 h-3.5 w-3.5" /> 测试推送
                      </DropdownMenuItem>
                    )}
                    <DropdownMenuItem onClick={() => openEdit(t)}>
                      <Pencil className="mr-2 h-3.5 w-3.5" /> 编辑
                    </DropdownMenuItem>
                    <DropdownMenuSeparator />
                    <DropdownMenuItem className="text-destructive focus:text-destructive" onClick={() => remove(t)}>
                      <Trash2 className="mr-2 h-3.5 w-3.5" /> 删除
                    </DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              </div>
              <div className="mt-0.5 flex items-center gap-1.5 pl-6 text-xs text-muted-foreground">
                <span className="truncate">{cronToLabel(t.cron_expr)}</span>
                <span className="shrink-0">· 下次 {t.enabled ? fmtNext(t.next_run_at) : '已停用'}</span>
              </div>
              {t.last_status && (
                <div className="mt-1 pl-6">
                  {t.last_status === 'running' && (
                    <Badge variant="secondary" className="h-5 gap-1 text-[11px]">
                      <Loader2 className="h-3 w-3 animate-spin" /> 运行中
                    </Badge>
                  )}
                  {t.last_status === 'ok' && <Badge variant="outline" className="h-5 text-[11px]">上次成功 {fmtNext(t.last_run_at)}</Badge>}
                  {t.last_status === 'error' && (
                    <Badge variant="destructive" className="h-5 text-[11px]" title={t.last_error || ''}>
                      上次失败{t.last_error ? `：${t.last_error.slice(0, 20)}` : ''}
                    </Badge>
                  )}
                </div>
              )}
              {t.last_push_error && (
                <div className="mt-1 pl-6 text-[11px] text-destructive" title={t.last_push_error}>
                  飞书推送失败：{t.last_push_error.slice(0, 40)}
                </div>
              )}
            </div>
          ))}
          {tasks.length === 0 && (
            <div className="px-2 py-8 text-center text-xs text-muted-foreground">
              暂无定时任务<br />点右上角 + 新建，到点自动跑数字人
            </div>
          )}
        </div>
      </div>

      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{editing ? '编辑定时任务' : '新建定时任务'}</DialogTitle>
            <DialogDescription>到点自动以你的身份与该数字人开启一个新会话，执行下面的指令。</DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <div className="space-y-1.5">
              <Label>名称</Label>
              <Input value={form.name} onChange={(e) => set('name')(e.target.value)} placeholder="如：每日销售数据巡检" />
            </div>
            <div className="space-y-1.5">
              <Label>指令</Label>
              <Textarea
                value={form.prompt}
                onChange={(e) => set('prompt')(e.target.value)}
                rows={4}
                placeholder="如：读取工作区里的销售数据，检查昨天是否有异常波动，结论写入 reports/ 文件夹"
              />
            </div>
            <div className="space-y-1.5">
              <Label>频率</Label>
              <Select value={form.freq} onValueChange={set('freq')}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="daily">每天</SelectItem>
                  <SelectItem value="weekly">每周</SelectItem>
                  <SelectItem value="monthly">每月</SelectItem>
                  <SelectItem value="hourly">每 N 小时</SelectItem>
                  <SelectItem value="custom">自定义 cron</SelectItem>
                </SelectContent>
              </Select>
            </div>
            {form.freq === 'daily' && (
              <div className="space-y-1.5">
                <Label>时间</Label>
                <Input type="time" value={form.time} onChange={(e) => set('time')(e.target.value)} />
              </div>
            )}
            {form.freq === 'weekly' && (
              <div className="flex gap-2">
                <div className="w-32 space-y-1.5">
                  <Label>星期</Label>
                  <Select value={form.weekday} onValueChange={set('weekday')}>
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      {WEEKDAYS.map((w, i) => <SelectItem key={i} value={String(i)}>{w}</SelectItem>)}
                    </SelectContent>
                  </Select>
                </div>
                <div className="flex-1 space-y-1.5">
                  <Label>时间</Label>
                  <Input type="time" value={form.time} onChange={(e) => set('time')(e.target.value)} />
                </div>
              </div>
            )}
            {form.freq === 'monthly' && (
              <div className="flex gap-2">
                <div className="w-32 space-y-1.5">
                  <Label>日期</Label>
                  <Select value={form.day} onValueChange={set('day')}>
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      {Array.from({ length: 31 }, (_, i) => <SelectItem key={i + 1} value={String(i + 1)}>{i + 1} 号</SelectItem>)}
                    </SelectContent>
                  </Select>
                </div>
                <div className="flex-1 space-y-1.5">
                  <Label>时间</Label>
                  <Input type="time" value={form.time} onChange={(e) => set('time')(e.target.value)} />
                </div>
              </div>
            )}
            {form.freq === 'monthly' && +form.day > 28 && (
              <p className="text-xs text-muted-foreground">没有该日的月份会跳过（如 2 月没有 31 号）</p>
            )}
            {form.freq === 'hourly' && (
              <div className="flex gap-2">
                <div className="w-32 space-y-1.5">
                  <Label>间隔</Label>
                  <Select value={form.hours} onValueChange={set('hours')}>
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      {HOUR_STEPS.map((h) => <SelectItem key={h} value={String(h)}>每 {h} 小时</SelectItem>)}
                    </SelectContent>
                  </Select>
                </div>
                <div className="flex-1 space-y-1.5">
                  <Label>第几分钟</Label>
                  <Input type="number" min={0} max={59} value={form.minute} onChange={(e) => set('minute')(e.target.value)} />
                </div>
              </div>
            )}
            {form.freq === 'custom' && (
              <div className="space-y-1.5">
                <Label>cron 表达式（分 时 日 月 周）</Label>
                <Input value={form.custom} onChange={(e) => set('custom')(e.target.value)} placeholder="如 30 9 * * 1（每周一 09:30）" className="font-mono" />
                {preview?.ok && (
                  <p className="text-xs text-muted-foreground">
                    接下来：{preview.next.map((n) => fmtNext(n)).join('、')}
                  </p>
                )}
                {preview && !preview.ok && <p className="text-xs text-destructive">{preview.error}</p>}
              </div>
            )}
            <div className="space-y-1.5 border-t pt-3">
              <Label>飞书推送（选填）</Label>
              <Select value={form.push_target} onValueChange={set('push_target')}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="none">不推送</SelectItem>
                  <SelectItem value="me">私聊推给我（OREO 机器人 1 对 1 消息，仅自己可见）</SelectItem>
                  <SelectItem value="group">推到飞书群（群机器人 Webhook）</SelectItem>
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground">
                {form.push_target === 'me'
                  ? '每次跑完结果以私聊卡片发给你的飞书（需你的账号已用飞书登录过平台）；个人摘要选这个，推群会泄露'
                  : form.push_target === 'group'
                    ? '每次跑完把结果推送到该飞书群（群 → 设置 → 群机器人 → 自定义机器人）'
                    : '跑完只在平台里留结果，不发飞书'}
              </p>
            </div>
            {form.push_target === 'group' && (
              <>
                <div className="space-y-1.5">
                  <Label>群机器人 Webhook</Label>
                  <Input
                    value={form.feishu_webhook}
                    onChange={(e) => set('feishu_webhook')(e.target.value)}
                    placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/…"
                    className="font-mono text-xs"
                  />
                </div>
                {form.feishu_webhook.trim() && (
                  <div className="space-y-1.5">
                    <Label>签名密钥（机器人安全设置选了「签名校验」才填）</Label>
                    <Input
                      value={form.feishu_secret}
                      onChange={(e) => set('feishu_secret')(e.target.value)}
                      placeholder="留空 = 机器人未开启签名校验"
                      className="font-mono text-xs"
                    />
                  </div>
                )}
              </>
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDialogOpen(false)}>取消</Button>
            <Button onClick={save} disabled={saving}>
              {saving && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />}
              {editing ? '保存' : '创建'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
