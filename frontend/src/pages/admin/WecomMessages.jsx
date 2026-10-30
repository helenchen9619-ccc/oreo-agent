import { Download, Search } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { toast } from 'sonner'
import { api, getToken } from '@/api'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'

// M31.1 企微存档消息查看（admin + 授权查看人可见；后端 require_wecom_viewer 双保险）
// 仓库首个分页界面：filters（输入中）/ applied（点「查询」才生效且页码归 1）双 state
const PAGE_SIZE = 20
const EMPTY = { sender: '', room: '', keyword: '', start: '', end: '' }

// msgtime 是毫秒时间戳（OpsAgentDetail fmtTime 同款范式）
const fmtMs = (ms) =>
  new Date(Number(ms)).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false })

const TEXT_MAX = 200

function Content({ text }) {
  if (!text) return <span className="text-muted-foreground">—</span>
  const cut = text.length > TEXT_MAX
  return (
    <span className="whitespace-pre-wrap break-all" title={cut ? text : undefined}>
      {cut ? text.slice(0, TEXT_MAX) + '…' : text}
    </span>
  )
}

export default function WecomMessages() {
  const [filters, setFilters] = useState(EMPTY) // 输入中
  const [applied, setApplied] = useState(EMPTY) // 已生效
  const [page, setPage] = useState(1)
  const [data, setData] = useState(null) // {total, items}
  const [meta, setMeta] = useState({ rooms: [] })
  const [loading, setLoading] = useState(false)
  const [exporting, setExporting] = useState(false) // M31.2 导出

  useEffect(() => {
    api('/admin/wecom/messages/meta').then(setMeta).catch(() => {})
  }, [])

  const load = useCallback(() => {
    setLoading(true)
    const q = new URLSearchParams({ page: String(page), page_size: String(PAGE_SIZE) })
    if (applied.sender.trim()) q.set('sender', applied.sender.trim())
    if (applied.room) q.set('room', applied.room)
    if (applied.keyword.trim()) q.set('keyword', applied.keyword.trim())
    // 时间窗必须 toISOString()——裸传 YYYY-MM-DD 会被解析成 UTC 午夜，偏 8 小时
    if (applied.start) q.set('start', new Date(applied.start + 'T00:00:00').toISOString())
    if (applied.end) q.set('end', new Date(applied.end + 'T23:59:59.999').toISOString())
    api(`/admin/wecom/messages?${q}`)
      .then(setData)
      .catch((e) => toast.error(e.message))
      .finally(() => setLoading(false))
  }, [applied, page])
  useEffect(load, [load])

  const search = () => {
    setApplied({ ...filters })
    setPage(1)
  }
  const reset = () => {
    setFilters(EMPTY)
    setApplied(EMPTY)
    setPage(1)
  }

  // M31.2：导出跟随 applied（已生效筛选，所见即所得）；fetch→blob→a[download] 照 StatsAdmin 范式
  const exportXlsx = async () => {
    setExporting(true)
    try {
      const q = new URLSearchParams()
      if (applied.sender.trim()) q.set('sender', applied.sender.trim())
      if (applied.room) q.set('room', applied.room)
      if (applied.keyword.trim()) q.set('keyword', applied.keyword.trim())
      if (applied.start) q.set('start', new Date(applied.start + 'T00:00:00').toISOString())
      if (applied.end) q.set('end', new Date(applied.end + 'T23:59:59.999').toISOString())
      const resp = await fetch(`/api/admin/wecom/messages/export?${q}`, {
        headers: { Authorization: `Bearer ${getToken()}` },
      })
      if (!resp.ok) {
        const d = await resp.json().catch(() => null)
        throw new Error(d?.detail || `导出失败 (${resp.status})`)
      }
      const blob = await resp.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `企微存档_${applied.start || '起'}_${applied.end || '至今'}.xlsx`
      a.click()
      URL.revokeObjectURL(url)
      toast.success(`已导出 ${data?.total ?? ''} 条消息`)
    } catch (e) {
      toast.error(e.message)
    } finally {
      setExporting(false)
    }
  }

  const pages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1

  const convLabel = (m) => {
    if (m.roomid) return `群：${m.room_name || m.roomid}` // M31.3：群名未解析也明确是群聊
    const others = (m.receivers || []).map((r) => r.name || r.userid).filter(Boolean)
    return `与 ${others.join('、') || '—'}`
  }

  return (
    <div>
      {/* 筛选行 */}
      <div className="flex flex-wrap items-center gap-2">
        <Input
          className="w-40"
          placeholder="发送人（姓名/userid）"
          value={filters.sender}
          onChange={(e) => setFilters({ ...filters, sender: e.target.value })}
        />
        <Select value={filters.room} onValueChange={(v) => setFilters({ ...filters, room: v === 'all' ? '' : v })}>
          <SelectTrigger className="w-44 min-w-0">
            <SelectValue placeholder="会话（全部）" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部会话</SelectItem>
            <SelectItem value="dm">仅私聊</SelectItem>
            <SelectItem value="group">全部群聊</SelectItem>
            {meta.rooms.map((r) => (
              <SelectItem key={r.roomid} value={r.roomid}>
                {r.roomname || r.roomid}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Input
          className="w-36"
          placeholder="内容关键词"
          value={filters.keyword}
          onChange={(e) => setFilters({ ...filters, keyword: e.target.value })}
          onKeyDown={(e) => e.key === 'Enter' && search()}
        />
        <Input type="date" className="w-36" value={filters.start} onChange={(e) => setFilters({ ...filters, start: e.target.value })} />
        <span className="text-xs text-muted-foreground">至</span>
        <Input type="date" className="w-36" value={filters.end} onChange={(e) => setFilters({ ...filters, end: e.target.value })} />
        <Button onClick={search} disabled={loading}>
          <Search className="mr-1.5 h-4 w-4" /> 查询
        </Button>
        <Button variant="ghost" onClick={reset}>重置</Button>
        {/* M31.2：导出当前筛选结果（点查询后才生效的那组条件），超 5 万条后端会提示缩小范围 */}
        <Button variant="outline" onClick={exportXlsx} disabled={exporting || !data || data.total === 0}>
          <Download className="mr-1.5 h-4 w-4" /> {exporting ? '导出中…' : '导出 Excel'}
        </Button>
      </div>

      {/* 列表 */}
      <div className="mt-3 rounded-2xl border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-24">时间</TableHead>
              <TableHead className="w-28">发送人</TableHead>
              <TableHead className="w-44">会话</TableHead>
              <TableHead className="w-16">类型</TableHead>
              <TableHead>内容</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {data?.items.map((m) => (
              <TableRow key={m.id}>
                <TableCell className="text-xs text-muted-foreground">{fmtMs(m.msgtime)}</TableCell>
                <TableCell className="text-sm">{m.sender_name || m.sender_userid || '—'}</TableCell>
                <TableCell className="text-sm text-muted-foreground">{convLabel(m)}</TableCell>
                <TableCell>
                  <Badge variant="outline">{m.msgtype || '—'}</Badge>
                </TableCell>
                <TableCell className="max-w-0 text-sm">
                  <Content text={m.text} />
                </TableCell>
              </TableRow>
            ))}
            {data?.items.length === 0 && (
              <TableRow>
                <TableCell colSpan={5} className="py-10 text-center text-sm text-muted-foreground">
                  没有符合条件的消息
                </TableCell>
              </TableRow>
            )}
            {!data && (
              <TableRow>
                <TableCell colSpan={5} className="py-10 text-center text-sm text-muted-foreground">
                  加载中…
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </div>

      {/* 分页条（仓库首个分页组件，自建） */}
      {data && data.total > 0 && (
        <div className="mt-3 flex items-center justify-between text-sm text-muted-foreground">
          <span>共 {data.total} 条 · 第 {data.page}/{pages} 页</span>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" disabled={page <= 1 || loading} onClick={() => setPage((p) => p - 1)}>
              上一页
            </Button>
            <Button variant="outline" size="sm" disabled={page >= pages || loading} onClick={() => setPage((p) => p + 1)}>
              下一页
            </Button>
          </div>
        </div>
      )}
    </div>
  )
}
