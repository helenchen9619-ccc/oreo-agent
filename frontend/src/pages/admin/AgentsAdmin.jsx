import { Bot, Plus, Sparkles } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { api } from '@/api'
import { deptLabel, deptTree, useDepartments } from '@/components/DepartmentSelect'
import { Avatar, AvatarFallback } from '@/components/ui/avatar'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Textarea } from '@/components/ui/textarea'

export default function AgentsAdmin() {
  const [tab, setTab] = useState('templates')
  const [agents, setAgents] = useState([])
  const [open, setOpen] = useState(false)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const navigate = useNavigate()

  // 实例视图的筛选/排序/分组（M3.16）
  const [depts] = useDepartments()
  const [deptFilter, setDeptFilter] = useState('all')
  const [ownerFilter, setOwnerFilter] = useState('all')
  const [tplFilter, setTplFilter] = useState('all')
  const [groupBy, setGroupBy] = useState('tpl') // tpl / dept / owner / time

  const load = useCallback(() => {
    api(`/admin/agents?view=${tab}`).then(setAgents).catch((e) => toast.error(e.message))
  }, [tab])
  useEffect(load, [load])

  const create = async (e) => {
    e.preventDefault()
    try {
      const a = await api('/admin/agents', { method: 'POST', body: { name, description } })
      toast.success(`已创建模板「${a.name}」，已自动生成 SOUL/AGENTS/MEMORY 文件骨架`)
      setOpen(false)
      setName('')
      setDescription('')
      navigate(`/admin/agents/${a.id}`)
    } catch (err) {
      toast.error(err.message)
    }
  }

  const isTpl = tab === 'templates'

  // —— 实例视图派生数据 ——
  // 选项一律从全量 agents 派生，不随筛选塌缩
  const ownerOptions = useMemo(() => {
    const m = new Map()
    for (const a of agents) if (a.owner_user_id && !m.has(a.owner_user_id)) m.set(a.owner_user_id, a.owner_name || `#${a.owner_user_id}`)
    return [...m.entries()]
  }, [agents])
  const tplOptions = useMemo(() => {
    const m = new Map()
    let hasNoTpl = false
    for (const a of agents) {
      if (a.template_id) m.set(a.template_id, a.template_name || `模板 #${a.template_id}`)
      else hasNoTpl = true
    }
    return { options: [...m.entries()], hasNoTpl }
  }, [agents])

  const shown = useMemo(() => {
    if (isTpl) return agents
    const filtered = agents.filter((a) => {
      if (deptFilter !== 'all' && String(a.owner_department_id) !== deptFilter) return false
      if (ownerFilter !== 'all' && String(a.owner_user_id) !== ownerFilter) return false
      if (tplFilter === 'none') return !a.template_id
      if (tplFilter !== 'all' && String(a.template_id) !== tplFilter) return false
      return true
    })
    // 固定按领用时间新→旧（ISO 字符串可字典序比较；时间分组因此也自然倒序）
    return [...filtered].sort((x, y) => (y.created_at || '').localeCompare(x.created_at || ''))
  }, [agents, isTpl, deptFilter, ownerFilter, tplFilter])

  // 分组：按所选维度（保持 shown 的顺序，组按首次出现排列）
  const GROUP_KEYS = {
    tpl: (a) => (a.template_id ? (a.template_name || `模板 #${a.template_id}`) : '无来源模板'),
    dept: (a) => a.owner_department_name || '未分配部门',
    owner: (a) => a.owner_name || '未知属主',
    time: (a) => (a.created_at ? a.created_at.slice(0, 10) : '未知时间'),
  }
  const groups = useMemo(() => {
    if (isTpl) return [{ key: null, list: shown }]
    const keyOf = GROUP_KEYS[groupBy]
    const m = new Map()
    for (const a of shown) {
      const key = keyOf(a)
      if (!m.has(key)) m.set(key, [])
      m.get(key).push(a)
    }
    return [...m.entries()].map(([key, list]) => ({ key, list }))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shown, isTpl, groupBy])

  const hasFilter = deptFilter !== 'all' || ownerFilter !== 'all' || tplFilter !== 'all'
  const clearFilters = () => {
    setDeptFilter('all')
    setOwnerFilter('all')
    setTplFilter('all')
  }

  const renderCard = (a) => {
    // 副标题：分组维度已在组标题体现，不再重复
    const parts = []
    if (groupBy !== 'owner') parts.push(`属主：${a.owner_name || '未知'}`)
    if (groupBy !== 'dept') parts.push(a.owner_department_name || '未分配部门')
    if (groupBy !== 'tpl') parts.push(a.template_name || '无来源模板')
    return (
      <Card key={a.id} className="cursor-pointer transition-shadow hover:shadow-md" onClick={() => navigate(`/admin/agents/${a.id}`)}>
        <CardHeader className="flex-row items-center gap-3 space-y-0">
          <Avatar className="h-11 w-11">
            <AvatarFallback><Bot className="h-5 w-5" /></AvatarFallback>
          </Avatar>
          <div className="min-w-0 flex-1">
            <CardTitle className="flex items-center gap-1.5 truncate text-base">
              {a.name}
              {a.is_master && <Badge variant="outline" className="shrink-0 text-amber-600"><Sparkles className="mr-0.5 h-3 w-3" />主</Badge>}
            </CardTitle>
            <CardDescription className="truncate">
              {isTpl ? (a.description || '暂无描述') : parts.join(' · ')}
            </CardDescription>
          </div>
          <Badge variant={a.status === 'active' ? 'default' : 'secondary'}>
            {a.status === 'active' ? '启用' : '归档'}
          </Badge>
        </CardHeader>
      </Card>
    )
  }

  return (
    <div className="h-full overflow-y-auto p-8">
      <div className="mx-auto max-w-5xl">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">数字人管理</h1>
            <p className="mt-1 text-sm text-muted-foreground">模板是你配置的基础数字人；员工领用后各自得到独立实例，互不影响</p>
          </div>
          {isTpl && <Button onClick={() => setOpen(true)}><Plus className="mr-1.5 h-4 w-4" /> 新建模板</Button>}
        </div>

        <Tabs value={tab} onValueChange={setTab} className="mt-6">
          <TabsList>
            <TabsTrigger value="templates">模板</TabsTrigger>
            <TabsTrigger value="instances">已领用实例</TabsTrigger>
          </TabsList>
          <TabsContent value={tab} className="mt-4">
            {!isTpl && (
              <div className="mb-4 flex flex-wrap items-center gap-2">
                <Select value={deptFilter} onValueChange={setDeptFilter}>
                  <SelectTrigger className="h-8 w-40 text-xs"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="all">全部部门</SelectItem>
                    {deptTree(depts).map((d) => <SelectItem key={d.id} value={String(d.id)}>{deptLabel(d)}</SelectItem>)}
                  </SelectContent>
                </Select>
                <Select value={ownerFilter} onValueChange={setOwnerFilter}>
                  <SelectTrigger className="h-8 w-36 text-xs"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="all">全部领用人</SelectItem>
                    {ownerOptions.map(([id, n]) => <SelectItem key={id} value={String(id)}>{n}</SelectItem>)}
                  </SelectContent>
                </Select>
                <Select value={tplFilter} onValueChange={setTplFilter}>
                  <SelectTrigger className="h-8 w-44 text-xs"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="all">全部来源模板</SelectItem>
                    {tplOptions.options.map(([id, n]) => <SelectItem key={id} value={String(id)}>{n}</SelectItem>)}
                    {tplOptions.hasNoTpl && <SelectItem value="none">无来源模板</SelectItem>}
                  </SelectContent>
                </Select>
                <Tabs value={groupBy} onValueChange={setGroupBy}>
                  <TabsList className="h-8">
                    <TabsTrigger value="tpl" className="h-7 px-2.5 text-xs">来源模板</TabsTrigger>
                    <TabsTrigger value="dept" className="h-7 px-2.5 text-xs">部门</TabsTrigger>
                    <TabsTrigger value="owner" className="h-7 px-2.5 text-xs">领用人</TabsTrigger>
                    <TabsTrigger value="time" className="h-7 px-2.5 text-xs">领用时间</TabsTrigger>
                  </TabsList>
                </Tabs>
                {hasFilter && (
                  <button onClick={clearFilters} className="text-xs text-muted-foreground underline-offset-2 hover:text-foreground hover:underline">
                    清除筛选
                  </button>
                )}
                <span className="ml-auto text-xs text-muted-foreground">{shown.length} / {agents.length} 个实例</span>
              </div>
            )}

            {groups.map((g) => (
              <div key={g.key ?? 'all'} className="mt-9 first:mt-0">
                {g.key && (
                  <div className="mb-3 flex items-center gap-2 border-b pb-2">
                    <span className="text-sm font-medium">{g.key}</span>
                    <Badge variant="secondary" className="text-xs">{g.list.length}</Badge>
                  </div>
                )}
                <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
                  {g.list.map(renderCard)}
                </div>
              </div>
            ))}
            {shown.length === 0 && (
              <div className="rounded-lg border border-dashed py-16 text-center text-sm text-muted-foreground">
                {isTpl
                  ? '还没有模板，点右上角新建'
                  : hasFilter
                    ? (<>没有符合筛选条件的实例，<button onClick={clearFilters} className="underline underline-offset-2 hover:text-foreground">清除筛选</button></>)
                    : '还没有员工领用实例'}
              </div>
            )}
          </TabsContent>
        </Tabs>
      </div>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>新建数字人模板</DialogTitle>
            <DialogDescription>模板本身不能被对话；员工领用或你派发后才会生成可对话的实例</DialogDescription>
          </DialogHeader>
          <form onSubmit={create} className="space-y-3">
            <div className="space-y-1.5">
              <Label>名称</Label>
              <Input value={name} onChange={(e) => setName(e.target.value)} required placeholder="例如：营销文案助手" />
            </div>
            <div className="space-y-1.5">
              <Label>描述</Label>
              <Textarea value={description} onChange={(e) => setDescription(e.target.value)} rows={3} placeholder="这个数字人是做什么的" />
            </div>
            <div className="flex justify-end"><Button type="submit">创建</Button></div>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  )
}
