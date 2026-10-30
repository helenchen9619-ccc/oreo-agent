import { Download } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { toast } from 'sonner'
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api, getToken } from '@/api'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'

const fmtTokens = (n) => (n >= 1_000_000 ? `${(n / 1_000_000).toFixed(2)}M` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n))
const fmtCost = (n) => `¥${Number(n || 0).toFixed(4)}`

const isoDate = (d) => d.toISOString().slice(0, 10)
const todayStr = () => isoDate(new Date())
const daysAgoStr = (n) => isoDate(new Date(Date.now() - n * 86400000))

export default function StatsAdmin() {
  const [start, setStart] = useState(daysAgoStr(30))
  const [end, setEnd] = useState(todayStr())
  const [overview, setOverview] = useState(null)
  const [dim, setDim] = useState('by-user')
  const [rows, setRows] = useState([])
  const [exporting, setExporting] = useState(false)

  const rangeQuery = `start=${start}&end=${end}`

  useEffect(() => {
    if (!start || !end) return
    api(`/admin/stats/overview?${rangeQuery}`).then(setOverview).catch((e) => toast.error(e.message))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [start, end])

  const loadDim = useCallback(() => {
    if (!start || !end) return
    api(`/admin/stats/${dim}?${rangeQuery}`).then(setRows).catch((e) => toast.error(e.message))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dim, start, end])
  useEffect(loadDim, [loadDim])

  const exportExcel = async () => {
    setExporting(true)
    try {
      const resp = await fetch(`/api/admin/stats/export?${rangeQuery}`, {
        headers: { Authorization: `Bearer ${getToken()}` },
      })
      if (!resp.ok) throw new Error(`导出失败 (${resp.status})`)
      const blob = await resp.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `usage_${start}_${end}.xlsx`
      a.click()
      URL.revokeObjectURL(url)
      toast.success('已导出 Excel')
    } catch (e) {
      toast.error(e.message)
    } finally {
      setExporting(false)
    }
  }

  const t = overview?.totals

  return (
    <div className="h-full overflow-y-auto p-8">
      <div className="mx-auto max-w-5xl">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">用量监控</h1>
            <p className="mt-1 text-sm text-muted-foreground">每次模型调用的 token 与费用（费用依赖模型价目表，未填价格的模型计 0）</p>
          </div>
          <div className="flex items-center gap-2">
            <input
              type="date"
              value={start}
              max={end}
              onChange={(e) => setStart(e.target.value)}
              className="h-9 rounded-md border bg-background px-2 text-sm"
            />
            <span className="text-sm text-muted-foreground">至</span>
            <input
              type="date"
              value={end}
              min={start}
              max={todayStr()}
              onChange={(e) => setEnd(e.target.value)}
              className="h-9 rounded-md border bg-background px-2 text-sm"
            />
            <Button variant="outline" onClick={exportExcel} disabled={exporting || !start || !end}>
              <Download className="mr-1.5 h-4 w-4" /> {exporting ? '导出中…' : '导出 Excel'}
            </Button>
          </div>
        </div>

        <div className="mt-6 grid grid-cols-2 gap-4 lg:grid-cols-4">
          <StatCard title="调用次数" value={t ? t.calls.toLocaleString() : '—'} />
          <StatCard title="Token 总量" value={t ? fmtTokens(t.input_tokens + t.output_tokens) : '—'} sub={t ? `入 ${fmtTokens(t.input_tokens)} / 出 ${fmtTokens(t.output_tokens)}` : ''} />
          <StatCard title="费用合计" value={t ? fmtCost(t.cost) : '—'} />
          <StatCard title="活跃用户" value={t ? String(t.active_users) : '—'} />
        </div>

        <Card className="mt-4">
          <CardHeader className="pb-2"><CardTitle className="text-sm font-medium">按日趋势</CardTitle></CardHeader>
          <CardContent>
            {overview && overview.daily.length > 0 ? (
              <ResponsiveContainer width="100%" height={240}>
                <AreaChart data={overview.daily} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                  <defs>
                    <linearGradient id="gTokens" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor="#2563eb" stopOpacity={0.3} />
                      <stop offset="100%" stopColor="#2563eb" stopOpacity={0.02} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                  <XAxis dataKey="date" tick={{ fontSize: 11 }} tickFormatter={(d) => d.slice(5)} />
                  <YAxis yAxisId="t" tick={{ fontSize: 11 }} tickFormatter={fmtTokens} width={48} />
                  <YAxis yAxisId="c" orientation="right" tick={{ fontSize: 11 }} tickFormatter={(v) => `¥${v}`} width={56} />
                  <Tooltip formatter={(v, name) => (name === '费用' ? fmtCost(v) : Number(v).toLocaleString())} />
                  <Area yAxisId="t" type="monotone" dataKey="tokens" name="Token" stroke="#2563eb" fill="url(#gTokens)" strokeWidth={1.5} />
                  <Area yAxisId="c" type="monotone" dataKey="cost" name="费用" stroke="#d97706" fill="none" strokeWidth={1.5} />
                </AreaChart>
              </ResponsiveContainer>
            ) : (
              <div className="py-16 text-center text-sm text-muted-foreground">该时间段内没有调用记录</div>
            )}
          </CardContent>
        </Card>

        <Tabs value={dim} onValueChange={setDim} className="mt-4">
          <TabsList>
            <TabsTrigger value="by-user">按人</TabsTrigger>
            <TabsTrigger value="by-department">按部门</TabsTrigger>
            <TabsTrigger value="by-model">按模型</TabsTrigger>
            <TabsTrigger value="by-agent">按数字人</TabsTrigger>
          </TabsList>
          <TabsContent value={dim} className="mt-3">
            <div className="rounded-2xl border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>{dim === 'by-model' ? '模型' : dim === 'by-agent' ? '数字人' : dim === 'by-department' ? '部门' : '用户'}</TableHead>
                    <TableHead className="text-right">调用次数</TableHead>
                    <TableHead className="text-right">输入 tokens</TableHead>
                    <TableHead className="text-right">输出 tokens</TableHead>
                    <TableHead className="text-right">费用</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rows.map((r, i) => (
                    <TableRow key={r.id ?? i}>
                      <TableCell className="text-sm font-medium">
                        {r.label}
                        {r.sub && <span className="ml-2 text-xs text-muted-foreground">{r.sub}</span>}
                      </TableCell>
                      <TableCell className="text-right text-sm">{r.calls.toLocaleString()}</TableCell>
                      <TableCell className="text-right text-sm">{r.input_tokens.toLocaleString()}</TableCell>
                      <TableCell className="text-right text-sm">{r.output_tokens.toLocaleString()}</TableCell>
                      <TableCell className="text-right text-sm">{fmtCost(r.cost)}</TableCell>
                    </TableRow>
                  ))}
                  {rows.length === 0 && (
                    <TableRow><TableCell colSpan={5} className="py-10 text-center text-sm text-muted-foreground">暂无数据</TableCell></TableRow>
                  )}
                </TableBody>
              </Table>
            </div>
          </TabsContent>
        </Tabs>
      </div>
    </div>
  )
}

function StatCard({ title, value, sub }) {
  return (
    <Card>
      <CardHeader className="pb-1"><CardTitle className="text-xs font-medium text-muted-foreground">{title}</CardTitle></CardHeader>
      <CardContent>
        <div className="text-2xl font-semibold tracking-tight">{value}</div>
        {sub && <div className="mt-0.5 text-xs text-muted-foreground">{sub}</div>}
      </CardContent>
    </Card>
  )
}
