import { ArrowUpCircle, Bot, Check, Download, Eye, FileText, FolderInput, Pencil, Plus, Trash2, Undo2 } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { api } from '@/api'
import { confirmDialog, promptDialog } from '@/components/dialogs'
import SkillFilesPanel from '@/components/SkillFilesPanel'
import { readSkillFolder, sanitizeSkillName } from '@/lib/importSkillFolder'
import { cn } from '@/lib/utils'
import { Avatar, AvatarFallback } from '@/components/ui/avatar'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Textarea } from '@/components/ui/textarea'
import { SkillViewDialog } from '@/pages/admin/AgentDetail'

export default function MarketPage() {
  return (
    <div className="h-full overflow-y-auto p-4 md:p-8">
      <div className="mx-auto max-w-5xl">
        <h1 className="text-2xl font-semibold tracking-tight">市场</h1>
        <p className="mt-1 text-sm text-muted-foreground">领用数字人模板与技能模板；管理你自己的技能</p>
        <Tabs defaultValue="agents" className="mt-6">
          <TabsList>
            <TabsTrigger value="agents">数字人市场</TabsTrigger>
            <TabsTrigger value="skills">技能市场</TabsTrigger>
            <TabsTrigger value="mine">我的技能</TabsTrigger>
          </TabsList>
          <TabsContent value="agents" className="mt-4"><AgentMarket /></TabsContent>
          <TabsContent value="skills" className="mt-4"><SkillMarket /></TabsContent>
          <TabsContent value="mine" className="mt-4"><MySkills /></TabsContent>
        </Tabs>
      </div>
    </div>
  )
}

// ---------- 数字人市场 ----------
function AgentMarket() {
  const [templates, setTemplates] = useState(null)
  const navigate = useNavigate()

  useEffect(() => {
    api('/agents').then((d) => setTemplates(d.claimable_templates)).catch((e) => toast.error(e.message))
  }, [])

  const claim = async (t) => {
    try {
      const inst = await api(`/agents/${t.id}/claim`, { method: 'POST' })
      toast.success(`已领用「${inst.name}」`)
      navigate(`/chat/${inst.id}`)
    } catch (e) {
      toast.error(e.message)
    }
  }

  if (!templates) return null
  return (
    <>
      <p className="mb-4 text-sm text-muted-foreground">领用后得到一份完全属于你的副本，可自由调教，互不影响</p>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {templates.map((t) => (
          <Card key={t.id}>
            <CardHeader className="flex-row items-center gap-3 space-y-0">
              <Avatar className="h-11 w-11">
                <AvatarFallback><Bot className="h-5 w-5" /></AvatarFallback>
              </Avatar>
              <div className="min-w-0 flex-1">
                <CardTitle className="truncate text-base">{t.name}</CardTitle>
                <CardDescription className="truncate">{t.description || '暂无描述'}</CardDescription>
              </div>
              <Button size="sm" onClick={() => claim(t)}>
                <Download className="mr-1 h-3.5 w-3.5" /> 领用
              </Button>
            </CardHeader>
          </Card>
        ))}
        {templates.length === 0 && (
          <div className="col-span-full rounded-lg border border-dashed py-12 text-center text-sm text-muted-foreground">
            暂无可领用的数字人（敏感模板仅管理员可派发）
          </div>
        )}
      </div>
    </>
  )
}

// ---------- 技能市场 ----------
function SkillMarket() {
  const [skills, setSkills] = useState(null)
  const [viewing, setViewing] = useState(null)
  const [cat, setCat] = useState('') // M26：分类筛选（''=全部）

  const load = useCallback(() => {
    api('/skills').then(setSkills).catch((e) => toast.error(e.message))
  }, [])
  useEffect(load, [load])

  // M26：分类清单（去重，按中文排序）+ 排序（按分类分组，无分类排最后，组内保持 id 序）+ 筛选
  const categories = useMemo(() => {
    if (!skills) return []
    return [...new Set(skills.map((s) => s.category).filter(Boolean))].sort((a, b) => a.localeCompare(b, 'zh'))
  }, [skills])

  const shown = useMemo(() => {
    if (!skills) return []
    const sorted = [...skills].sort((a, b) => {
      const ca = a.category || '￿' // 无分类排最后（'￿' 是 Unicode 最大值，排序必沉底）
      const cb = b.category || '￿'
      return ca.localeCompare(cb, 'zh') || a.id - b.id
    })
    return cat ? sorted.filter((s) => s.category === cat) : sorted
  }, [skills, cat])

  const claim = async (s) => {
    try {
      const copy = await api(`/skills/${s.id}/fork`, { method: 'POST' })
      toast.success(`已领用为我的技能「${copy.name}」，可在「我的技能」里修改并绑定到数字人`)
      load()
    } catch (e) {
      toast.error(e.message)
    }
  }

  if (!skills) return null
  return (
    <>
      <p className="mb-4 text-sm text-muted-foreground">平台技能由管理员维护、只读；领用（fork）后变成你的个人版本，可自由修改并绑定到你的数字人</p>
      {categories.length > 0 && (
        <div className="mb-4 flex flex-wrap items-center gap-1.5">
          <button
            onClick={() => setCat('')}
            className={cn(
              'flex items-center gap-1 rounded-full border px-2.5 py-1 text-xs transition-colors',
              !cat ? 'bg-foreground font-medium text-background' : 'text-muted-foreground hover:text-foreground',
            )}
          >
            全部（{skills.length}）
          </button>
          {categories.map((c) => (
            <button
              key={c}
              onClick={() => setCat(c)}
              className={cn(
                'flex items-center gap-1 rounded-full border px-2.5 py-1 text-xs transition-colors',
                cat === c ? 'bg-foreground font-medium text-background' : 'text-muted-foreground hover:text-foreground',
              )}
            >
              {c}（{skills.filter((s) => s.category === c).length}）
            </button>
          ))}
        </div>
      )}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {shown.map((s) => (
          <Card key={s.id} className={!s.enabled ? 'opacity-60' : ''}>
            <CardHeader className="space-y-2">
              <div className="flex items-center gap-2">
                <CardTitle className="min-w-0 flex-1 truncate font-mono text-sm">{s.name}</CardTitle>
                {s.category && <Badge variant="outline" className="shrink-0 text-xs">{s.category}</Badge>}
                {!s.enabled && <Badge variant="secondary" className="shrink-0">停用</Badge>}
              </div>
              <CardDescription className="line-clamp-2 min-h-8">{s.description || '暂无描述'}</CardDescription>
              <div className="flex justify-end gap-1.5 pt-1">
                <Button variant="ghost" size="sm" className="h-7 text-xs" onClick={() => setViewing({ skill: s, files: [{ path: 'SKILL.md', content: s.instructions }] })}>
                  <Eye className="mr-1 h-3 w-3" /> 查看内容
                </Button>
                {s.forked ? (
                  <Button size="sm" variant="outline" className="h-7 text-xs" disabled><Check className="mr-1 h-3 w-3" /> 已领用</Button>
                ) : (
                  <Button size="sm" className="h-7 text-xs" onClick={() => claim(s)} disabled={!s.enabled}>
                    <Download className="mr-1 h-3 w-3" /> 领用
                  </Button>
                )}
              </div>
            </CardHeader>
          </Card>
        ))}
        {skills.length === 0 && (
          <div className="col-span-full rounded-lg border border-dashed py-12 text-center text-sm text-muted-foreground">
            技能市场还是空的，等管理员上架
          </div>
        )}
        {skills.length > 0 && shown.length === 0 && (
          <div className="col-span-full rounded-lg border border-dashed py-12 text-center text-sm text-muted-foreground">
            该分类下暂无技能
          </div>
        )}
      </div>
      <SkillViewDialog viewing={viewing} onClose={() => setViewing(null)} />
    </>
  )
}

// ---------- 我的技能（个人技能完整管理） ----------
const EMPTY_SKILL = { name: '', description: '', category: '', instructions: '' }

function MySkills() {
  const [skills, setSkills] = useState(null)
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState(null)
  const [form, setForm] = useState(EMPTY_SKILL)
  const [filesFor, setFilesFor] = useState(null)
  const importRef = useRef(null)
  const [importing, setImporting] = useState(false)

  // 技能迁移：选本地技能文件夹 → 读文本类文件 → 建个人技能并写入全部文件
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
      const def = sanitizeSkillName(rootName)
      const name = await promptDialog({ title: '技能名称', desc: '小写字母/数字/连字符，全局唯一', defaultValue: def, confirmText: '导入' })
      if (!name) return
      if (!/^[a-z0-9][a-z0-9-]*$/.test(name)) {
        toast.error('名称格式不合法：小写字母/数字/连字符')
        return
      }
      // 确保有 SKILL.md 主文件（没有就生成一份默认的）
      const hasMain = files.some((f) => f.path === 'SKILL.md')
      const instructions = hasMain
        ? files.find((f) => f.path === 'SKILL.md').content
        : `# ${name}\n\n（从本地文件夹导入，请补充技能指令）\n`
      const created = await api('/skills/mine', {
        method: 'POST',
        body: { name, description: '', category: '', instructions },
      })
      await api(`/skills/mine/${created.id}/files`, {
        method: 'PUT',
        body: { files: hasMain ? files : [{ path: 'SKILL.md', content: instructions }, ...files] },
      })
      toast.success(`已导入技能「${name}」：${files.length} 个文件${skipped ? `，跳过 ${skipped} 个二进制/超限文件` : ''}`)
      load()
    } catch (err) {
      toast.error(err.message)
    } finally {
      setImporting(false)
    }
  }

  const load = useCallback(() => {
    api('/skills/mine').then(setSkills).catch((e) => toast.error(e.message))
  }, [])
  useEffect(load, [load])

  const openEdit = (s) => {
    setEditing(s)
    setForm(s ? { name: s.name, description: s.description, category: s.category || '', instructions: s.instructions } : EMPTY_SKILL)
    setOpen(true)
  }

  const save = async (e) => {
    e.preventDefault()
    try {
      if (editing) await api(`/skills/mine/${editing.id}`, { method: 'PATCH', body: form })
      else await api('/skills/mine', { method: 'POST', body: form })
      toast.success('已保存')
      setOpen(false)
      load()
    } catch (err) {
      toast.error(err.message)
    }
  }

  const remove = async (s) => {
    if (!(await confirmDialog({ title: '删除技能', desc: `确定删除我的技能「${s.name}」？绑定它的数字人将不再可用。`, confirmText: '删除', danger: true }))) return
    try {
      await api(`/skills/mine/${s.id}`, { method: 'DELETE' })
      toast.success('已删除')
      load()
    } catch (e) {
      toast.error(e.message)
    }
  }

  // M33：领用副本自助升级到源技能最新版（覆盖前服务端自动备份，可回滚）
  const pullUpdate = async (s) => {
    if (!(await confirmDialog({
      title: '升级到最新版',
      desc: `将用源技能「${s.source_name}」的最新内容覆盖「${s.name}」，你自己的修改会被覆盖（覆盖前自动备份，可随时回滚）。绑定它的数字人立即使用新内容。`,
      confirmText: '升级',
    }))) return
    try {
      await api(`/skills/${s.id}/pull-update`, { method: 'POST' })
      toast.success(`已升级「${s.name}」到最新版（旧版已备份）`)
      load()
    } catch (e) {
      toast.error(e.message)
    }
  }

  const rollback = async (s) => {
    if (!(await confirmDialog({
      title: '回滚到升级前',
      desc: `将「${s.name}」恢复为升级前备份的内容；当前内容会转存为新备份（可再次回滚换回）。`,
      confirmText: '回滚',
    }))) return
    try {
      await api(`/skills/${s.id}/rollback-upgrade`, { method: 'POST' })
      toast.success(`已回滚「${s.name}」`)
      load()
    } catch (e) {
      toast.error(e.message)
    }
  }

  if (!skills) return null
  return (
    <>
      <div className="mb-4 flex items-center justify-between">
        <p className="text-sm text-muted-foreground">你的个人技能（含从市场领用的副本）：可自由修改、绑定到你的数字人</p>
        <div className="flex gap-2">
          <Button size="sm" variant="outline" disabled={importing} onClick={() => importRef.current?.click()}>
            <FolderInput className="mr-1 h-3.5 w-3.5" /> {importing ? '导入中…' : '导入本地文件夹'}
          </Button>
          <Button size="sm" onClick={() => openEdit(null)}><Plus className="mr-1 h-3.5 w-3.5" /> 新建技能</Button>
        </div>
        <input
          ref={importRef}
          type="file"
          className="hidden"
          webkitdirectory=""
          onChange={onImportFolder}
        />
      </div>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {skills.map((s) => (
          <Card key={s.id} className={!s.enabled ? 'opacity-60' : ''}>
            <CardHeader className="space-y-2">
              <div className="flex items-center gap-2">
                <CardTitle className="min-w-0 flex-1 truncate font-mono text-sm">{s.name}</CardTitle>
                {s.category && <Badge variant="outline" className="shrink-0 text-xs">{s.category}</Badge>}
                {!s.enabled && <Badge variant="secondary" className="shrink-0">停用</Badge>}
              </div>
              {/* M3.3 血缘标注：fork 副本显示来源与版本状态 */}
              {s.source_name && (
                <p className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
                  <span>源自 {s.source_deleted ? <s className="text-muted-foreground/70">{s.source_name}</s> : s.source_name}</span>
                  {s.source_deleted && <Badge variant="outline" className="text-xs text-muted-foreground">源已删除</Badge>}
                  {s.source_has_update && !s.source_deleted && (
                    <>
                      <Badge variant="secondary" className="text-xs text-blue-600 dark:text-blue-400">有新版本</Badge>
                      <Button variant="outline" size="sm" className="h-5 px-1.5 text-xs" title="用源技能最新内容覆盖此副本（自动备份，可回滚）" onClick={() => pullUpdate(s)}>
                        <ArrowUpCircle className="mr-0.5 h-3 w-3" /> 升级
                      </Button>
                    </>
                  )}
                </p>
              )}
              <CardDescription className="line-clamp-2 min-h-8">{s.description || '暂无描述'}</CardDescription>
              <div className="flex justify-end gap-1 pt-1">
                {s.has_backup && (
                  <Button variant="ghost" size="sm" className="h-7 text-xs" title="回滚到升级前备份的内容" onClick={() => rollback(s)}>
                    <Undo2 className="mr-1 h-3 w-3" /> 回滚
                  </Button>
                )}
                <Button variant="ghost" size="sm" className="h-7 text-xs" onClick={() => setFilesFor(s)}>
                  <FileText className="mr-1 h-3 w-3" /> 文件
                </Button>
                <Button variant="ghost" size="icon" className="h-7 w-7" title="编辑信息" onClick={() => openEdit(s)}>
                  <Pencil className="h-3.5 w-3.5" />
                </Button>
                <Button variant="ghost" size="icon" className="h-7 w-7" title="删除" onClick={() => remove(s)}>
                  <Trash2 className="h-3.5 w-3.5" />
                </Button>
              </div>
            </CardHeader>
          </Card>
        ))}
        {skills.length === 0 && (
          <div className="col-span-full rounded-lg border border-dashed py-12 text-center text-sm text-muted-foreground">
            还没有个人技能：去「技能市场」领用，或点右上角新建
          </div>
        )}
      </div>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-xl">
          <DialogHeader><DialogTitle>{editing ? `编辑「${editing.name}」` : '新建我的技能'}</DialogTitle></DialogHeader>
          <form onSubmit={save} className="space-y-3">
            <div className="space-y-1.5">
              <Label>名称（小写字母/数字/连字符，全局唯一）</Label>
              <Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required pattern="[a-z0-9][a-z0-9-]*" placeholder="my-weekly-report" disabled={!!editing} />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <Label>分类</Label>
                <Input value={form.category} onChange={(e) => setForm({ ...form, category: e.target.value })} placeholder="例如：office / 财务 / excel" />
              </div>
              <div className="space-y-1.5">
                <Label>描述</Label>
                <Input value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} placeholder="一句话说明" />
              </div>
            </div>
            <div className="space-y-1.5">
              <Label>SKILL.md（主指令，保存后可在「文件」里继续加附件）</Label>
              <Textarea value={form.instructions} onChange={(e) => setForm({ ...form, instructions: e.target.value })} rows={10} className="font-mono text-sm" placeholder="# 技能说明&#10;当用户要求……时，按以下步骤……" />
            </div>
            <div className="flex justify-end"><Button type="submit">保存</Button></div>
          </form>
        </DialogContent>
      </Dialog>

      {filesFor && <SkillFilesPanel skill={filesFor} mineBase onClose={() => { setFilesFor(null); load() }} />}
    </>
  )
}
