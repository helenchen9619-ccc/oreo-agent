import { ArrowUpToLine, FolderInput, Pencil, Plus, Send, Trash2 } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { toast } from 'sonner'
import { api } from '@/api'
import { confirmDialog, promptDialog } from '@/components/dialogs'
import SkillFilesPanel from '@/components/SkillFilesPanel'
import { readSkillFolder, sanitizeSkillName } from '@/lib/importSkillFolder'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Textarea } from '@/components/ui/textarea'
import { cn } from '@/lib/utils'

const EMPTY = { name: '', description: '', category: '', instructions: '', enabled: true }

export default function SkillsAdmin() {
  return (
    <div className="h-full overflow-y-auto p-8">
      <div className="mx-auto max-w-5xl">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">技能库管理</h1>
          <p className="mt-1 text-sm text-muted-foreground">技能是一个文件夹：SKILL.md 主指令 + 可选脚本附件；平台技能对员工只读，员工可 fork 成自己的版本</p>
        </div>
        <Tabs defaultValue="skills" className="mt-6">
          <TabsList>
            <TabsTrigger value="skills">技能总览</TabsTrigger>
            <TabsTrigger value="overview">派发总览</TabsTrigger>
          </TabsList>
          <TabsContent value="skills" className="mt-4"><SkillList /></TabsContent>
          <TabsContent value="overview" className="mt-4"><Overview /></TabsContent>
        </Tabs>
      </div>
    </div>
  )
}

// ---------- 技能总览：平台技能 + 全部员工的个人技能（可筛选归属；个人技能也可派发 = 技能迁移） ----------
function SkillList() {
  const [skills, setSkills] = useState([])
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState(null)
  const [form, setForm] = useState(EMPTY)
  const [filesFor, setFilesFor] = useState(null)
  const [dispatchFor, setDispatchFor] = useState(null)
  const [categoryFilter, setCategoryFilter] = useState('all')
  const [ownerFilter, setOwnerFilter] = useState('all') // all | platform | personal
  const importRef = useRef(null)
  const [importing, setImporting] = useState(false)

  // 技能迁移：从本地文件夹批量导入为平台技能
  const onImportFolder = async (e) => {
    // 必须先复制成数组再清空 input——FileList 是活引用，清 value 后 length 变 0
    const list = Array.from(e.target.files || [])
    e.target.value = ''
    if (!list.length) return
    const rootName = (list[0].webkitRelativePath || '').split('/')[0] || ''
    setImporting(true)
    toast.info(`已选择「${rootName}」，共 ${list.length} 个文件，正在读取…`)
    try {
      const { files, skipped } = await readSkillFolder(list)
      if (!files.length) {
        toast.error('文件夹里没有可导入的文本文件（.md/.py/.json 等）')
        return
      }
      const name = await promptDialog({ title: '技能名称', desc: '小写字母/数字/连字符，全局唯一', defaultValue: sanitizeSkillName(rootName), confirmText: '导入' })
      if (!name) return
      if (!/^[a-z0-9][a-z0-9-]*$/.test(name)) {
        toast.error('名称格式不合法：小写字母/数字/连字符')
        return
      }
      const hasMain = files.some((f) => f.path === 'SKILL.md')
      const instructions = hasMain
        ? files.find((f) => f.path === 'SKILL.md').content
        : `# ${name}\n\n（从本地文件夹导入，请补充技能指令）\n`
      const created = await api('/admin/skills', {
        method: 'POST',
        body: { name, description: '', category: '', instructions, enabled: true },
      })
      await api(`/admin/skills/${created.id}/files`, {
        method: 'PUT',
        body: { files: hasMain ? files : [{ path: 'SKILL.md', content: instructions }, ...files] },
      })
      toast.success(`已导入平台技能「${name}」：${files.length} 个文件${skipped ? `，跳过 ${skipped} 个二进制/超限文件` : ''}`)
      load()
    } catch (err) {
      toast.error(err.message)
    } finally {
      setImporting(false)
    }
  }

  const load = useCallback(() => {
    api('/admin/skills').then(setSkills).catch((e) => toast.error(e.message))
  }, [])
  useEffect(load, [load])

  const categories = useMemo(() => [...new Set(skills.map((s) => s.category).filter(Boolean))], [skills])
  const shown = skills
    .filter((s) => categoryFilter === 'all' || s.category === categoryFilter)
    .filter((s) => ownerFilter === 'all' || (ownerFilter === 'personal' ? s.owner_user_id != null : s.owner_user_id == null))

  const openEdit = (s) => {
    setEditing(s)
    setForm(s ? { name: s.name, description: s.description, category: s.category || '', instructions: s.instructions, enabled: s.enabled } : EMPTY)
    setOpen(true)
  }

  const save = async (e) => {
    e.preventDefault()
    try {
      if (editing) await api(`/admin/skills/${editing.id}`, { method: 'PATCH', body: form })
      else await api('/admin/skills', { method: 'POST', body: form })
      toast.success('已保存')
      setOpen(false)
      load()
    } catch (err) {
      toast.error(err.message)
    }
  }

  const remove = async (s) => {
    if (!(await confirmDialog({ title: '删除技能', desc: `确定删除技能「${s.name}」？已绑定它的数字人将不再可用。`, confirmText: '删除', danger: true }))) return
    try {
      await api(`/admin/skills/${s.id}`, { method: 'DELETE' })
      toast.success('已删除')
      load()
    } catch (e) {
      toast.error(e.message)
    }
  }

  // 收录（蒸馏）：个人技能复制为平台技能，原稿仍归作者、互不影响
  const promote = async (s) => {
    const name = await promptDialog({
      title: `收录「${s.name}」为平台技能`,
      desc: '复制为平台技能后归组织管理、全员可派发；原稿仍归作者，两者互不影响。',
      defaultValue: `${s.name}-lib`,
      placeholder: '平台版名称（小写字母/数字/连字符）',
      confirmText: '收录',
    })
    if (!name) return
    try {
      const r = await api(`/admin/skills/${s.id}/promote`, { method: 'POST', body: { name } })
      toast.success(`已收录为平台技能「${r.name}」，可派发了`)
      load()
    } catch (e) {
      toast.error(e.message)
    }
  }

  return (
    <>
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          {/* 归属快速筛选：全部 / 平台 / 个人 */}
          <div className="flex rounded-md border p-0.5">
            {[['all', '全部'], ['platform', '平台'], ['personal', '个人']].map(([v, label]) => (
              <button
                key={v}
                onClick={() => setOwnerFilter(v)}
                className={cn(
                  'rounded px-2.5 py-1 text-xs transition-colors',
                  ownerFilter === v ? 'bg-foreground font-medium text-background' : 'text-muted-foreground hover:text-foreground',
                )}
              >
                {label}
              </button>
            ))}
          </div>
          <Select value={categoryFilter} onValueChange={setCategoryFilter}>
            <SelectTrigger className="h-8 w-40 text-xs"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部分类</SelectItem>
              {categories.map((c) => <SelectItem key={c} value={c}>{c}</SelectItem>)}
            </SelectContent>
          </Select>
          <span className="text-xs text-muted-foreground">{shown.length} 个技能</span>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" disabled={importing} onClick={() => importRef.current?.click()}>
            <FolderInput className="mr-1.5 h-4 w-4" /> {importing ? '导入中…' : '导入本地文件夹'}
          </Button>
          <Button onClick={() => openEdit(null)}><Plus className="mr-1.5 h-4 w-4" /> 新建技能</Button>
        </div>
        <input ref={importRef} type="file" className="hidden" webkitdirectory="" onChange={onImportFolder} />
      </div>

      <div className="mt-4 rounded-2xl border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>名称</TableHead>
              <TableHead className="w-24">归属</TableHead>
              <TableHead className="w-28">分类</TableHead>
              <TableHead>描述</TableHead>
              <TableHead className="w-20">状态</TableHead>
              <TableHead className="w-28 text-right">操作</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {shown.map((s) => (
              <TableRow key={s.id}>
                <TableCell className="font-mono text-sm">{s.name}</TableCell>
                <TableCell>
                  {s.owner_user_id != null
                    ? <Badge variant="secondary" className="text-xs">个人 · {s.owner_name || '?'}</Badge>
                    : (
                      <div className="flex items-center gap-1.5">
                        <Badge variant="outline" className="text-xs">平台</Badge>
                        {s.created_by_name && <span className="text-xs text-muted-foreground" title="原作者署名">原作 {s.created_by_name}</span>}
                      </div>
                    )}
                </TableCell>
                <TableCell>{s.category ? <Badge variant="outline" className="text-xs">{s.category}</Badge> : <span className="text-xs text-muted-foreground">—</span>}</TableCell>
                <TableCell className="max-w-md truncate text-sm text-muted-foreground">{s.description}</TableCell>
                <TableCell>{s.enabled ? <Badge>启用</Badge> : <Badge variant="secondary">停用</Badge>}</TableCell>
                <TableCell className="text-right">
                  {s.owner_user_id != null ? (
                    // M3.3：个人技能不能直接派发，先收录为平台技能
                    <Button variant="ghost" size="sm" className="h-8 text-xs" title="复制为平台技能（原稿仍归作者），之后可派发" onClick={() => promote(s)}>
                      <ArrowUpToLine className="mr-1 h-3 w-3" />收录
                    </Button>
                  ) : (
                    <Button variant="ghost" size="sm" className="h-8 text-xs" onClick={() => setDispatchFor(s)}><Send className="mr-1 h-3 w-3" />派发</Button>
                  )}
                  <Button variant="ghost" size="sm" className="h-8 text-xs" onClick={() => setFilesFor(s)}>文件</Button>
                  <Button variant="ghost" size="icon" className="h-8 w-8" onClick={() => openEdit(s)}><Pencil className="h-3.5 w-3.5" /></Button>
                  <Button variant="ghost" size="icon" className="h-8 w-8" onClick={() => remove(s)}><Trash2 className="h-3.5 w-3.5" /></Button>
                </TableCell>
              </TableRow>
            ))}
            {shown.length === 0 && (
              <TableRow><TableCell colSpan={6} className="py-10 text-center text-sm text-muted-foreground">暂无技能</TableCell></TableRow>
            )}
          </TableBody>
        </Table>
      </div>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-xl">
          <DialogHeader><DialogTitle>{editing ? '编辑技能' : '新建技能'}</DialogTitle></DialogHeader>
          <form onSubmit={save} className="space-y-3">
            <div className="space-y-1.5">
              <Label>名称（小写字母/数字/连字符）</Label>
              <Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required pattern="[a-z0-9][a-z0-9-]*" placeholder="weekly-report" />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <Label>分类</Label>
                <Input value={form.category} onChange={(e) => setForm({ ...form, category: e.target.value })} placeholder="例如：office / 财务 / excel" list="skill-categories" />
                <datalist id="skill-categories">
                  {categories.map((c) => <option key={c} value={c} />)}
                </datalist>
              </div>
              <div className="space-y-1.5">
                <Label>描述</Label>
                <Input value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} placeholder="一句话说明" />
              </div>
            </div>
            <div className="space-y-1.5">
              <Label>SKILL.md（主指令，保存后可在「文件」里继续加脚本附件）</Label>
              <Textarea value={form.instructions} onChange={(e) => setForm({ ...form, instructions: e.target.value })} rows={10} className="font-mono text-sm" placeholder="# 技能说明&#10;当用户要求……时，按以下步骤……" />
            </div>
            <div className="flex items-center gap-2">
              <Switch checked={form.enabled} onCheckedChange={(v) => setForm({ ...form, enabled: v })} />
              <Label>启用</Label>
            </div>
            <div className="flex justify-end"><Button type="submit">保存</Button></div>
          </form>
        </DialogContent>
      </Dialog>

      {filesFor && <SkillFilesPanel skill={filesFor} onClose={() => setFilesFor(null)} />}
      {dispatchFor && <DispatchSkillDialog skill={dispatchFor} onClose={() => setDispatchFor(null)} />}
    </>
  )
}

// ---------- 技能派发：技能 → 一批数字人（实例/模板 两个 tab，各带全选） ----------
function DispatchSkillDialog({ skill, onClose }) {
  const [templates, setTemplates] = useState(null)
  const [instances, setInstances] = useState(null)
  const [selected, setSelected] = useState(new Set())
  const [boundNow, setBoundNow] = useState(new Set())
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    Promise.all([
      api('/admin/agents?view=templates'),
      api('/admin/agents?view=instances'),
      api('/admin/skills/overview'),
    ])
      .then(([tpls, insts, overview]) => {
        setTemplates(tpls)
        setInstances(insts)
        const mine = overview.find((o) => o.id === skill.id)
        setBoundNow(new Set((mine?.bindings || []).map((b) => b.agent_id)))
      })
      .catch((e) => toast.error(e.message))
  }, [skill.id])

  const toggle = (aid, checked) => {
    const next = new Set(selected)
    if (checked) next.add(aid)
    else next.delete(aid)
    setSelected(next)
  }

  const selectAll = (list) => {
    const next = new Set(selected)
    list.filter((a) => !boundNow.has(a.id)).forEach((a) => next.add(a.id))
    setSelected(next)
  }

  const dispatch = async () => {
    setBusy(true)
    try {
      const r = await api(`/admin/skills/${skill.id}/dispatch`, { method: 'POST', body: { agent_ids: [...selected] } })
      toast.success(`已派发，技能现绑定 ${r.bound_agent_ids.length} 个数字人`)
      onClose()
    } catch (e) {
      toast.error(e.message)
    } finally {
      setBusy(false)
    }
  }

  const renderList = (list) => (
    <div className="mt-2 max-h-[42vh] space-y-1.5 overflow-y-auto">
      {!list && <div className="py-6 text-center text-sm text-muted-foreground">加载中…</div>}
      {list?.map((a) => {
        const already = boundNow.has(a.id)
        return (
          <label key={a.id} className="flex items-center gap-2.5 rounded-md border px-3 py-2 text-sm">
            <Checkbox
              checked={selected.has(a.id)}
              onCheckedChange={(c) => toggle(a.id, !!c)}
              disabled={already}
            />
            <span className="min-w-0 flex-1 truncate">{a.name}</span>
            {a.is_template
              ? <Badge variant="outline" className="text-xs">模板</Badge>
              : <Badge variant="secondary" className="text-xs">实例 · {a.owner_name || '?'}</Badge>}
            {already && <Badge variant="outline" className="text-xs text-green-600">已绑定</Badge>}
          </label>
        )
      })}
      {list?.length === 0 && <div className="py-6 text-center text-sm text-muted-foreground">暂无</div>}
    </div>
  )

  return (
    <Dialog open onOpenChange={onClose}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>派发「{skill.name}」</DialogTitle>
          <DialogDescription>把技能直接绑定到选中的数字人；已绑定的会标注，重复派发无副作用</DialogDescription>
        </DialogHeader>
        <Tabs defaultValue="instances">
          <TabsList>
            <TabsTrigger value="instances">实例（{instances?.length ?? '…'}）</TabsTrigger>
            <TabsTrigger value="templates">模板（{templates?.length ?? '…'}）</TabsTrigger>
          </TabsList>
          <TabsContent value="instances">
            <Button variant="outline" size="sm" className="mt-2 h-7 text-xs" onClick={() => selectAll(instances || [])}>全选所有实例</Button>
            {renderList(instances)}
          </TabsContent>
          <TabsContent value="templates">
            <Button variant="outline" size="sm" className="mt-2 h-7 text-xs" onClick={() => selectAll(templates || [])}>全选所有模板</Button>
            {renderList(templates)}
          </TabsContent>
        </Tabs>
        <div className="flex justify-end gap-2">
          <Button variant="outline" onClick={onClose}>取消</Button>
          <Button onClick={dispatch} disabled={busy || selected.size === 0}>{busy ? '派发中…' : `派发（${selected.size}）`}</Button>
        </div>
      </DialogContent>
    </Dialog>
  )
}

// ---------- 派发总览 ----------
function Overview() {
  const [rows, setRows] = useState(null)

  useEffect(() => {
    api('/admin/skills/overview').then(setRows).catch((e) => toast.error(e.message))
  }, [])

  if (!rows) return <div className="py-10 text-center text-sm text-muted-foreground">加载中…</div>

  return (
    <div className="rounded-2xl border">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>技能</TableHead>
            <TableHead className="w-24">分类</TableHead>
            <TableHead className="w-28">归属</TableHead>
            <TableHead>绑定的数字人</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((r) => (
            <TableRow key={r.id}>
              <TableCell className="font-mono text-sm">{r.name}</TableCell>
              <TableCell>{r.category ? <Badge variant="outline" className="text-xs">{r.category}</Badge> : '—'}</TableCell>
              <TableCell className="text-xs">{r.owner_name ? `个人 · ${r.owner_name}` : '平台'}</TableCell>
              <TableCell>
                {r.bindings.length === 0 ? (
                  <span className="text-xs text-muted-foreground">未被绑定</span>
                ) : (
                  <div className="flex flex-wrap gap-1.5">
                    {r.bindings.map((b) => (
                      <Badge key={b.agent_id} variant={b.is_template ? 'outline' : 'secondary'} className="text-xs">
                        {b.agent_name}{b.is_template ? '（模板）' : b.owner_name ? ` · ${b.owner_name}` : ''}
                      </Badge>
                    ))}
                  </div>
                )}
              </TableCell>
            </TableRow>
          ))}
          {rows.length === 0 && (
            <TableRow><TableCell colSpan={4} className="py-10 text-center text-sm text-muted-foreground">暂无技能</TableCell></TableRow>
          )}
        </TableBody>
      </Table>
    </div>
  )
}
