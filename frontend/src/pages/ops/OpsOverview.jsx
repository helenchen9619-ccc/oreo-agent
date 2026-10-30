// 运行中心 · L1 总览（AGENT-OPS）：全平台数字人实例健康度一览，钻取进详情。
// 健康灯恒按 24h 窗口（后端算好）；指标列随 24h/7d 切换；运行中徽标 15s 轮询。
import { Activity, ArrowDown, ArrowUp, Search } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { api } from '@/api'
import { Input } from '@/components/ui/input'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { cn } from '@/lib/utils'

const HEALTH = {
  error: { dot: 'bg-red-500', label: '异常' },
  warn: { dot: 'bg-yellow-500', label: '关注' },
  ok: { dot: 'bg-green-500', label: '健康' },
  idle: { dot: 'bg-gray-300 dark:bg-gray-600', label: '闲置' },
}

const fmtMs = (ms) => (ms == null ? '—' : ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`)
const fmtTokens = (n) => (n >= 1_000_000 ? `${(n / 1_000_000).toFixed(2)}M` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n))
const fmtCost = (n) => `¥${Number(n || 0).toFixed(4)}`
const fmtActive = (iso) => {
  if (!iso) return '从未'
  const mins = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000))
  if (mins < 1) return '刚刚'
  if (mins < 60) return `${mins} 分钟前`
  if (mins < 1440) return `${Math.floor(mins / 60)} 小时前`
  return `${Math.floor(mins / 1440)} 天前`
}

const COLUMNS = [
  { key: 'health', label: '健康', sortable: false },
  { key: 'name', label: '数字人', sortable: false },
  { key: 'turns', label: '轮次', sortable: true },
  { key: 'error_rate', label: '错误率', sortable: true },
  { key: 'avg_duration_ms', label: '均耗时', sortable: true },
  { key: 'tokens', label: 'Token', sortable: true },
  { key: 'cost', label: '费用', sortable: true },
  { key: 'last_active', label: '最近活跃', sortable: true },
]

export default function OpsOverview() {
  const [window_, setWindow] = useState('24h')
  const [rows, setRows] = useState([])
  const [active, setActive] = useState({}) // agent_id → 运行中数（15s 轮询）
  const [sort, setSort] = useState({ key: 'error_rate', dir: -1 })
  const [query, setQuery] = useState('')        // M24.2：属主/数字人名搜索
  const [onlyRunning, setOnlyRunning] = useState(false) // M24.2：只看运行中
  const navigate = useNavigate()

  useEffect(() => {
    api(`/admin/ops/agents?window=${window_}`).then(setRows).catch((e) => toast.error(e.message))
  }, [window_])

  const pollActive = useCallback(() => {
    api('/admin/ops/runtime/active')
      .then((list) => {
        const m = {}
        for (const r of list) m[r.agent_id] = (m[r.agent_id] || 0) + 1
        setActive(m)
      })
      .catch(() => {})
  }, [])
  useEffect(() => {
    pollActive()
    const t = setInterval(pollActive, 15000)
    return () => clearInterval(t)
  }, [pollActive])

  const runningTotal = useMemo(() => Object.values(active).reduce((s, n) => s + n, 0), [active])

  const sorted = useMemo(() => {
    // M24.2：先过滤（搜索词匹配属主/数字人名；只看运行中按轮询 active map，与徽标同源），再排序
    const q = query.trim().toLowerCase()
    let out = rows
    if (q) out = out.filter((r) => (r.owner_name || '').toLowerCase().includes(q) || (r.name || '').toLowerCase().includes(q))
    if (onlyRunning) out = out.filter((r) => (active[r.id] || 0) > 0)
    if (!sort.key) return out
    return [...out].sort((a, b) => {
      const va = a[sort.key] ?? -1
      const vb = b[sort.key] ?? -1
      return (va > vb ? 1 : va < vb ? -1 : 0) * sort.dir
    })
  }, [rows, sort, query, onlyRunning, active])

  const toggleSort = (key) =>
    setSort((s) => (s.key === key ? { key, dir: -s.dir } : { key, dir: -1 }))

  return (
    <div className="h-full overflow-y-auto p-8">
      <div className="mx-auto max-w-6xl">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">运行中心</h1>
            <p className="mt-1 text-sm text-muted-foreground">
              全平台数字人运行健康度（健康灯按近 24h：🔴错误≥3或错误率&gt;20% · 🟡错误率5–20%或均耗时&gt;60s · ⚪7天无活动）
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-3">
            <div className="relative">
              <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="搜索属主 / 数字人名"
                className="h-8 w-48 pl-8 text-sm"
              />
            </div>
            <button
              onClick={() => setOnlyRunning((v) => !v)}
              className={cn(
                'inline-flex h-8 items-center gap-1.5 rounded-md border px-3 text-sm',
                onlyRunning ? 'bg-foreground font-medium text-background' : 'text-muted-foreground hover:text-foreground',
              )}
            >
              <Activity className="h-3.5 w-3.5" />
              只看运行中{runningTotal > 0 && `（${runningTotal}）`}
            </button>
            <div className="flex rounded-md border p-0.5 text-sm">
              {['24h', '7d'].map((w) => (
                <button
                  key={w}
                  onClick={() => setWindow(w)}
                  className={cn('rounded px-3 py-1', window_ === w ? 'bg-foreground font-medium text-background' : 'text-muted-foreground hover:text-foreground')}
                >
                  {w === '24h' ? '近 24 小时' : '近 7 天'}
                </button>
              ))}
            </div>
          </div>
        </div>

        <div className="mt-6 rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                {COLUMNS.map((c) => (
                  <TableHead
                    key={c.key}
                    className={cn(c.sortable && 'cursor-pointer select-none hover:text-foreground')}
                    onClick={c.sortable ? () => toggleSort(c.key) : undefined}
                  >
                    <span className="inline-flex items-center gap-1">
                      {c.label}
                      {sort.key === c.key && (sort.dir === -1 ? <ArrowDown className="h-3 w-3" /> : <ArrowUp className="h-3 w-3" />)}
                    </span>
                  </TableHead>
                ))}
                <TableHead>运行中</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {sorted.map((r) => {
                const h = HEALTH[r.health] || HEALTH.ok
                const running = active[r.id] || 0
                return (
                  <TableRow key={r.id} className="cursor-pointer" onClick={() => navigate(`/admin/ops/agents/${r.id}`)}>
                    <TableCell>
                      <span className="inline-flex items-center gap-1.5">
                        <span className={cn('inline-block h-2.5 w-2.5 rounded-full', h.dot)} />
                        <span className="text-xs text-muted-foreground">{h.label}</span>
                      </span>
                    </TableCell>
                    <TableCell>
                      <div className="font-medium">{r.name}</div>
                      <div className="text-xs text-muted-foreground">属主：{r.owner_name || '—'}</div>
                    </TableCell>
                    <TableCell>{r.turns}</TableCell>
                    <TableCell className={cn(r.error_rate > 0 && 'text-red-600 dark:text-red-400')}>{(r.error_rate * 100).toFixed(1)}%</TableCell>
                    <TableCell>{fmtMs(r.avg_duration_ms)}</TableCell>
                    <TableCell>{fmtTokens(r.tokens)}</TableCell>
                    <TableCell>{fmtCost(r.cost)}</TableCell>
                    <TableCell className="text-muted-foreground">{fmtActive(r.last_active)}</TableCell>
                    <TableCell>
                      {running > 0 ? (
                        <span className="inline-flex items-center gap-1 rounded-full bg-blue-100 px-2 py-0.5 text-xs font-medium text-blue-700 dark:bg-blue-950 dark:text-blue-300">
                          <Activity className="h-3 w-3 animate-pulse" />{running}
                        </span>
                      ) : (
                        <span className="text-xs text-muted-foreground">—</span>
                      )}
                    </TableCell>
                  </TableRow>
                )
              })}
              {sorted.length === 0 && (
                <TableRow><TableCell colSpan={9} className="py-10 text-center text-sm text-muted-foreground">
                  {rows.length === 0 ? '暂无数字人实例' : '没有符合条件的数字人'}
                </TableCell></TableRow>
              )}
            </TableBody>
          </Table>
        </div>
      </div>
    </div>
  )
}
