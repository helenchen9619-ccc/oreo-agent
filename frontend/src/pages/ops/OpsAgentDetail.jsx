// 运行中心 · L2 数字人详情（AGENT-OPS）：指标卡 + 近 20 轮记录 + 会话/工作区/记忆钻取。
import { ArrowLeft, ChevronDown, ChevronRight, Download, FileText, Loader2, Sparkles, StickyNote } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { toast } from 'sonner'
import { api } from '@/api'
import { confirmDialog } from '@/components/dialogs'
import PreviewPanel, { downloadRaw } from '@/components/FilePreview'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { cn } from '@/lib/utils'

const HEALTH_LABEL = { error: '🔴 异常', warn: '🟡 关注', ok: '🟢 健康', idle: '⚪ 闲置' }
const STATUS_LABEL = { ok: '成功', error: '失败', cancelled: '已取消' }
const fmtMs = (ms) => (ms == null ? '—' : ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`)
const fmtTokens = (n) => (n >= 1_000_000 ? `${(n / 1_000_000).toFixed(2)}M` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n))
const fmtTime = (iso) => (iso ? new Date(iso).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—')

export default function OpsAgentDetail() {
  const { id } = useParams()
  const navigate = useNavigate()
  const [detail, setDetail] = useState(null)

  useEffect(() => {
    api(`/admin/ops/agents/${id}`).then(setDetail).catch((e) => toast.error(e.message))
  }, [id])

  if (!detail) {
    return <div className="flex h-full items-center justify-center"><Loader2 className="h-5 w-5 animate-spin text-muted-foreground" /></div>
  }
  const { agent, health, metrics, recent_turns } = detail

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <div className="flex-1 overflow-y-auto p-8">
        <div className="mx-auto max-w-6xl space-y-6">
          <div className="flex items-center gap-3">
            <Button variant="ghost" size="icon" className="h-8 w-8" onClick={() => navigate('/admin/ops')}>
              <ArrowLeft className="h-4 w-4" />
            </Button>
            <div>
              <h1 className="text-xl font-semibold tracking-tight">
                {agent.name}
                <span className="ml-2 text-sm font-normal">{HEALTH_LABEL[health]}</span>
              </h1>
              <p className="mt-0.5 text-sm text-muted-foreground">
                属主：{agent.owner_name || '—'} · 状态：{agent.status === 'active' ? '运行中' : '已归档'} · 创建于 {fmtTime(agent.created_at)}
              </p>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
            <MetricCard title="24h 轮次 / 错误率" value={`${metrics['24h'].turns} / ${(metrics['24h'].error_rate * 100).toFixed(1)}%`} />
            <MetricCard title="24h 平均耗时" value={fmtMs(metrics['24h'].avg_ms)} />
            <MetricCard title="7d 轮次 / 错误率" value={`${metrics['7d'].turns} / ${(metrics['7d'].error_rate * 100).toFixed(1)}%`} />
            <MetricCard title="7d Token / 费用" value={`${fmtTokens(metrics['7d'].tokens)} / ¥${Number(metrics['7d'].cost).toFixed(4)}`} />
          </div>

          <Card>
            <CardHeader className="pb-2"><CardTitle className="text-base">最近轮次（{recent_turns.length}）</CardTitle></CardHeader>
            <CardContent>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>时间</TableHead><TableHead>用户</TableHead><TableHead>模型</TableHead><TableHead>状态</TableHead>
                    <TableHead>耗时</TableHead><TableHead>轮数</TableHead><TableHead>工具</TableHead><TableHead>错误</TableHead><TableHead>会话</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {recent_turns.map((t) => (
                    <TableRow key={t.id}>
                      <TableCell className="text-muted-foreground">{fmtTime(t.started_at)}</TableCell>
                      <TableCell>{t.user_name || t.user_id}</TableCell>
                      <TableCell className="text-xs text-muted-foreground">{t.model_name || '—'}</TableCell>
                      <TableCell>
                        <span className={cn(
                          'text-xs',
                          t.status === 'error' && 'font-medium text-red-600 dark:text-red-400',
                          t.status === 'cancelled' && 'text-yellow-600 dark:text-yellow-400',
                        )}>
                          {STATUS_LABEL[t.status] || t.status}
                        </span>
                      </TableCell>
                      <TableCell>{fmtMs(t.duration_ms)}</TableCell>
                      <TableCell>{t.rounds}</TableCell>
                      <TableCell>{t.tool_calls}{t.tool_errors > 0 && <span className="text-red-600">（{t.tool_errors} 错）</span>}</TableCell>
                      <TableCell className="max-w-48 truncate text-xs text-muted-foreground" title={t.error_msg || ''}>{t.error_msg || '—'}</TableCell>
                      <TableCell>
                        <Button variant="link" size="sm" className="h-auto p-0 text-xs" onClick={() => navigate(`/admin/ops/conversations/${t.conversation_id}`)}>
                          #{t.conversation_id}
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))}
                  {recent_turns.length === 0 && (
                    <TableRow><TableCell colSpan={9} className="py-8 text-center text-sm text-muted-foreground">还没有轮次记录</TableCell></TableRow>
                  )}
                </TableBody>
              </Table>
            </CardContent>
          </Card>

          <Tabs defaultValue="conversations">
            <TabsList>
              <TabsTrigger value="conversations">会话</TabsTrigger>
              <TabsTrigger value="workspace">工作区文件</TabsTrigger>
              <TabsTrigger value="memory">记忆</TabsTrigger>
            </TabsList>
            <TabsContent value="conversations"><ConversationsTab agentId={id} /></TabsContent>
            <TabsContent value="workspace"><WorkspaceTab agentId={id} /></TabsContent>
            <TabsContent value="memory"><MemoryTab agentId={id} /></TabsContent>
          </Tabs>
        </div>
      </div>
    </div>
  )
}

function MetricCard({ title, value }) {
  return (
    <Card>
      <CardHeader className="pb-1 pt-4"><CardTitle className="text-xs font-normal text-muted-foreground">{title}</CardTitle></CardHeader>
      <CardContent className="pb-4 text-lg font-semibold">{value}</CardContent>
    </Card>
  )
}

function ConversationsTab({ agentId }) {
  const [rows, setRows] = useState(null)
  const navigate = useNavigate()
  useEffect(() => {
    api(`/admin/ops/agents/${agentId}/conversations`).then(setRows).catch((e) => toast.error(e.message))
  }, [agentId])
  if (!rows) return <div className="flex justify-center py-8"><Loader2 className="h-4 w-4 animate-spin text-muted-foreground" /></div>
  return (
    <div className="rounded-md border">
      <Table>
        <TableHeader>
          <TableRow><TableHead>会话</TableHead><TableHead>用户</TableHead><TableHead>渠道</TableHead><TableHead>消息数</TableHead><TableHead>最后活跃</TableHead><TableHead>状态</TableHead></TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((c) => (
            <TableRow key={c.id} className="cursor-pointer" onClick={() => navigate(`/admin/ops/conversations/${c.id}`)}>
              <TableCell className="max-w-72 truncate font-medium">{c.title}</TableCell>
              <TableCell>{c.user_name}</TableCell>
              <TableCell className="text-muted-foreground">{c.channel}</TableCell>
              <TableCell>{c.message_count}</TableCell>
              <TableCell className="text-muted-foreground">{fmtTime(c.updated_at)}</TableCell>
              <TableCell>{c.active ? <span className="text-xs font-medium text-blue-600 dark:text-blue-400">运行中</span> : <span className="text-xs text-muted-foreground">—</span>}</TableCell>
            </TableRow>
          ))}
          {rows.length === 0 && <TableRow><TableCell colSpan={6} className="py-8 text-center text-sm text-muted-foreground">暂无会话</TableCell></TableRow>}
        </TableBody>
      </Table>
    </div>
  )
}

function WorkspaceTab({ agentId }) {
  const [groups, setGroups] = useState(null)
  const [collapsed, setCollapsed] = useState(new Set())
  const [preview, setPreview] = useState(null) // 文件对象（含 content_text）

  useEffect(() => {
    api(`/admin/ops/agents/${agentId}/workspace`).then(setGroups).catch((e) => toast.error(e.message))
  }, [agentId])

  const openPreview = async (f) => {
    try {
      // 详情接口拉 content_text（★后端写 admin.read_workspace 审计）
      const full = await api(`/admin/ops/workspace/${f.id}`)
      setPreview(full)
    } catch (e) {
      toast.error(e.message)
    }
  }

  if (!groups) return <div className="flex justify-center py-8"><Loader2 className="h-4 w-4 animate-spin text-muted-foreground" /></div>

  return (
    <div className="flex min-h-0 gap-0 rounded-md border">
      <div className="flex-1 overflow-y-auto py-2">
        {groups.map((g) => {
          const isCollapsed = collapsed.has(g.user_id)
          return (
            <div key={g.user_id}>
              <button
                className="flex w-full items-center gap-1.5 px-3 py-1.5 text-sm font-medium hover:bg-accent"
                onClick={() => setCollapsed((s) => { const n = new Set(s); n.has(g.user_id) ? n.delete(g.user_id) : n.add(g.user_id); return n })}
              >
                {isCollapsed ? <ChevronRight className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
                {g.user_name}
                <span className="text-xs font-normal text-muted-foreground">（{g.files.length} 个文件）</span>
              </button>
              {!isCollapsed && g.files.map((f) => (
                <div
                  key={f.id}
                  onClick={() => openPreview(f)}
                  className={cn(
                    'group flex w-full cursor-pointer items-center gap-2 py-1 pl-8 pr-3 text-left text-sm hover:bg-accent',
                    preview?.id === f.id && 'bg-accent',
                  )}
                >
                  <FileText className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                  <span className="min-w-0 flex-1 truncate">{f.filename}</span>
                  <span className="shrink-0 text-xs text-muted-foreground">{f.size >= 1024 ? `${(f.size / 1024).toFixed(1)}KB` : `${f.size}B`}</span>
                  <button
                    title="下载"
                    className="shrink-0 rounded p-0.5 text-muted-foreground hover:bg-background hover:text-foreground"
                    onClick={(e) => {
                      e.stopPropagation()
                      downloadRaw(`/api/admin/ops/workspace/${f.id}/raw`, f.filename).catch((err) => toast.error(err.message))
                    }}
                  >
                    <Download className="h-3.5 w-3.5" />
                  </button>
                </div>
              ))}
            </div>
          )
        })}
        {groups.length === 0 && <div className="py-8 text-center text-sm text-muted-foreground">工作区暂无文件</div>}
      </div>
      {preview && (
        // 右侧全高大滑出（SkillFilesPanel 同款 fixed 模式）：预览区不再挤在页内分栏里
        <div className="fixed inset-y-0 right-0 z-40 flex w-[min(1080px,94vw)] flex-col border-l bg-background shadow-2xl">
          <PreviewPanel
            readOnly
            rawUrl={`/api/admin/ops/workspace/${preview.id}/raw`}
            file={preview}
            onClose={() => setPreview(null)}
            onInsert={() => {}}
          />
        </div>
      )}
    </div>
  )
}

// 记忆 tab（M22c）：属主/各用户的每日笔记 + 手动「整理进长期记忆」（属主笔记 → MEMORY.md 用户偏好与约定区）
function MemoryTab({ agentId }) {
  const [groups, setGroups] = useState(null)
  const [collapsed, setCollapsed] = useState(new Set())
  const [busy, setBusy] = useState(false)

  const load = () => api(`/admin/ops/agents/${agentId}/memory-notes`).then(setGroups).catch((e) => toast.error(e.message))
  useEffect(() => { load() }, [agentId])

  const consolidate = async () => {
    const ok = await confirmDialog({
      title: '整理进长期记忆',
      desc: '用该数字人的默认模型，把属主的笔记提炼合并进 MEMORY.md 的「用户偏好与约定」区。旧版自动留存版本历史可回滚，笔记本身不删除。',
    })
    if (!ok) return
    setBusy(true)
    try {
      const r = await api(`/admin/ops/agents/${agentId}/memory-consolidate`, { method: 'POST' })
      toast.success(`提炼完成：${r.notes_in} 条笔记 → ${r.items_out} 条长期记忆`)
    } catch (e) {
      toast.error(e.message)
    } finally {
      setBusy(false)
    }
  }

  if (!groups) return <div className="flex justify-center py-8"><Loader2 className="h-4 w-4 animate-spin text-muted-foreground" /></div>
  const total = groups.reduce((s, g) => s + g.notes.length, 0)

  return (
    <div className="rounded-md border">
      <div className="flex items-center justify-between gap-3 border-b px-3 py-2">
        <p className="text-xs text-muted-foreground">
          每日笔记（memory_write 记入，仅今/昨两天自动进提示词）· 共 {total} 条
        </p>
        <Button size="sm" variant="outline" disabled={busy || total === 0} onClick={consolidate}>
          {busy ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : <Sparkles className="mr-1.5 h-3.5 w-3.5" />}
          整理进长期记忆
        </Button>
      </div>
      <div className="max-h-[480px] overflow-y-auto py-1">
        {groups.map((g) => {
          const isCollapsed = collapsed.has(g.user_id)
          return (
            <div key={g.user_id}>
              <button
                className="flex w-full items-center gap-1.5 px-3 py-1.5 text-sm font-medium hover:bg-accent"
                onClick={() => setCollapsed((s) => { const n = new Set(s); n.has(g.user_id) ? n.delete(g.user_id) : n.add(g.user_id); return n })}
              >
                {isCollapsed ? <ChevronRight className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
                {g.user_name}
                <span className="text-xs font-normal text-muted-foreground">（{g.notes.length} 条笔记）</span>
              </button>
              {!isCollapsed && g.notes.map((n) => (
                <div key={n.id} className="flex items-start gap-2 py-1 pl-8 pr-3 text-sm">
                  <StickyNote className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                  <span className="shrink-0 rounded bg-muted px-1.5 py-px text-xs text-muted-foreground">{n.note_date}</span>
                  <span className="min-w-0 flex-1 whitespace-pre-wrap break-words">{n.content}</span>
                </div>
              ))}
            </div>
          )
        })}
        {groups.length === 0 && <div className="py-8 text-center text-sm text-muted-foreground">暂无记忆笔记</div>}
      </div>
    </div>
  )
}
