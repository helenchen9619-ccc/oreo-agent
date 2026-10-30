import { Archive, Eye, History, RefreshCcw, Send, ShieldCheck, Sparkles, X } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { toast } from 'sonner'
import { api, getUser } from '@/api'
import DepartmentSelect from '@/components/DepartmentSelect'
import { confirmDialog } from '@/components/dialogs'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Textarea } from '@/components/ui/textarea'
import { cn } from '@/lib/utils'

const FILE_KEYS = [
  { key: 'SOUL', hint: '这个数字人是谁、什么性格、什么口吻' },
  { key: 'AGENTS', hint: '工作方式、约束、流程等通用指令' },
  { key: 'MEMORY', hint: '长期策展记忆（数字人对话中也会自动维护）' },
]

/** mode: 'admin'（管理端全量）| 'owner'（员工管自己的实例） */
export default function AgentDetail({ mode }) {
  const { id } = useParams()
  const navigate = useNavigate()
  const [agent, setAgent] = useState(null)
  const base = mode === 'admin' ? `/admin/agents/${id}` : `/my-agents/${id}`

  const load = useCallback(() => {
    const req = mode === 'admin'
      ? api('/admin/agents').then((rows) => rows.find((x) => x.id === Number(id)))
      : api(`/my-agents/${id}`)
    req.then((a) => {
      if (!a) {
        toast.error('数字人不存在')
        navigate(mode === 'admin' ? '/admin/agents' : '/')
        return
      }
      setAgent(a)
    }).catch((e) => toast.error(e.message))
  }, [id, mode, navigate])

  useEffect(load, [load])

  if (!agent) return null
  const isTpl = agent.is_template

  const action = async (fn, okMsg) => {
    try {
      await fn()
      if (okMsg) toast.success(okMsg)
      load()
    } catch (e) {
      toast.error(e.message)
    }
  }

  return (
    <div className="h-full overflow-y-auto p-8">
      <div className="mx-auto max-w-4xl">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-2xl font-semibold tracking-tight">{agent.name}</h1>
          <Badge variant={agent.status === 'active' ? 'default' : 'secondary'}>{agent.status === 'active' ? '启用中' : '已归档'}</Badge>
          <Badge variant="outline">{isTpl ? '模板' : `实例${agent.owner_name ? ` · ${agent.owner_name}` : ''}`}</Badge>
          {agent.is_master && <Badge variant="outline" className="text-amber-600"><Sparkles className="mr-0.5 h-3 w-3" />主 agent</Badge>}
          <div className="ml-auto flex items-center gap-2">
            {!isTpl && (
              <Button
                variant="outline" size="sm"
                onClick={async () => {
                  if (!(await confirmDialog({ title: '从模板同步', desc: '从模板重新同步会用模板当前的 .md 和技能覆盖本数字人，你自己的调教将丢失。确定？', confirmText: '同步', danger: true }))) return
                  action(() => api(`/my-agents/${id}/sync-from-template`, { method: 'POST' }), '已从模板同步')
                }}
              >
                <RefreshCcw className="mr-1.5 h-3.5 w-3.5" /> 从模板同步
              </Button>
            )}
            {mode === 'admin' && (
              <>
                {isTpl && <DispatchDialog templateId={id} />}
                {isTpl && <ClaimGrantsDialog templateId={id} />}
                <Button variant="outline" size="sm" onClick={() => action(() => api(`/admin/agents/${id}/set-master`, { method: 'POST' }), agent.is_master ? '已取消主 agent' : '已设为主 agent')}>
                  <Sparkles className="mr-1.5 h-3.5 w-3.5" /> {agent.is_master ? '取消主 agent' : '设为主 agent'}
                </Button>
                <Button variant="outline" size="sm" onClick={() => action(() => api(`/admin/agents/${id}/archive`, { method: 'POST' }), agent.status === 'active' ? '已归档' : '已启用')}>
                  <Archive className="mr-1.5 h-3.5 w-3.5" /> {agent.status === 'active' ? '归档' : '取消归档'}
                </Button>
              </>
            )}
          </div>
        </div>

        <Tabs defaultValue="files" className="mt-6">
          <TabsList>
            <TabsTrigger value="files">人格文件 (.md)</TabsTrigger>
            <TabsTrigger value="info">基本信息</TabsTrigger>
            <TabsTrigger value="skills">技能</TabsTrigger>
          </TabsList>
          <TabsContent value="files" className="mt-4"><FilesEditor base={base} /></TabsContent>
          <TabsContent value="info" className="mt-4">
            {mode === 'admin' ? <AdminInfoTab agent={agent} onSaved={load} /> : <OwnerInfoTab agent={agent} base={base} onSaved={load} />}
          </TabsContent>
          <TabsContent value="skills" className="mt-4">
            {mode === 'admin' ? <AdminSkillsTab agentId={id} /> : <OwnerSkillsTab agentId={id} />}
          </TabsContent>
        </Tabs>
      </div>
    </div>
  )
}

// ---------- .md 文件编辑器（admin/owner 共用，base 不同） ----------
export function FilesEditor({ base }) {
  const [files, setFiles] = useState({})
  const [tab, setTab] = useState('SOUL')
  const [draft, setDraft] = useState('')
  const [versions, setVersions] = useState([])
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    api(`${base}/files`)
      .then((rows) => {
        const map = {}
        rows.forEach((f) => { map[f.file_key] = f })
        setFiles(map)
        setDraft(map[tab]?.content ?? '')
      })
      .catch((e) => toast.error(e.message))
  }, [base]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (files[tab]) setDraft(files[tab].content)
    api(`${base}/files/${tab}/versions`).then(setVersions).catch(() => setVersions([]))
  }, [tab]) // eslint-disable-line react-hooks/exhaustive-deps

  const dirty = files[tab] && draft !== files[tab].content

  const reloadFiles = async () => {
    const rows = await api(`${base}/files`)
    const map = {}
    rows.forEach((f) => { map[f.file_key] = f })
    setFiles(map)
    setDraft(map[tab]?.content ?? '')
    api(`${base}/files/${tab}/versions`).then(setVersions).catch(() => {})
  }

  const save = async () => {
    setSaving(true)
    try {
      const r = await api(`${base}/files/${tab}`, { method: 'PUT', body: { content: draft } })
      setFiles({ ...files, [tab]: { ...files[tab], content: draft, version: r.version } })
      toast.success(`${tab}.md 已保存为 v${r.version}`)
      api(`${base}/files/${tab}/versions`).then(setVersions).catch(() => {})
    } catch (e) {
      toast.error(e.message)
    } finally {
      setSaving(false)
    }
  }

  const rollback = async (v) => {
    try {
      const r = await api(`${base}/files/${tab}/rollback/${v}`, { method: 'POST' })
      toast.success(`已回滚到 v${v}（当前 v${r.version}）`)
      await reloadFiles()
    } catch (e) {
      toast.error(e.message)
    }
  }

  const meta = FILE_KEYS.find((f) => f.key === tab)

  return (
    <Tabs value={tab} onValueChange={setTab}>
      <div className="flex items-center justify-between">
        <TabsList>
          {FILE_KEYS.map((f) => <TabsTrigger key={f.key} value={f.key}>{f.key}.md</TabsTrigger>)}
        </TabsList>
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          {files[tab] && <span>当前 v{files[tab].version}</span>}
          {dirty && <Badge variant="outline" className="text-amber-600">未保存</Badge>}
          <Button size="sm" onClick={save} disabled={!dirty || saving}>{saving ? '保存中…' : '保存'}</Button>
        </div>
      </div>
      {FILE_KEYS.map((f) => (
        <TabsContent key={f.key} value={f.key} className="mt-3">
          <p className="mb-2 text-xs text-muted-foreground">{f.hint}</p>
          <Textarea value={draft} onChange={(e) => setDraft(e.target.value)} className="min-h-[420px] font-mono text-sm" placeholder={`# 在这里编写 ${f.key}.md`} />
          {versions.length > 0 && (
            <div className="mt-3 rounded-2xl border">
              <div className="flex items-center gap-1.5 border-b px-3 py-2 text-xs font-medium text-muted-foreground"><History className="h-3.5 w-3.5" /> 历史版本</div>
              <div className="max-h-40 overflow-y-auto">
                {versions.map((v) => (
                  <div key={v.version} className="flex items-center justify-between px-3 py-1.5 text-xs">
                    <span>v{v.version} · {new Date(v.created_at).toLocaleString()}</span>
                    <Button size="sm" variant="ghost" className="h-6 text-xs" onClick={() => rollback(v.version)}>回滚到此版本</Button>
                  </div>
                ))}
              </div>
            </div>
          )}
        </TabsContent>
      ))}
    </Tabs>
  )
}

// ---------- 基本信息：admin 全量 ----------
function AdminInfoTab({ agent, onSaved }) {
  const [form, setForm] = useState(agent)
  const [models, setModels] = useState([])
  useEffect(() => setForm(agent), [agent])
  useEffect(() => {
    api('/admin/gateway/models').then((rows) => setModels(rows.filter((m) => m.model_type === 'chat'))).catch(() => {})
  }, [])

  const toggleAllowlist = (mid, checked) => {
    const cur = form.model_allowlist ?? models.map((m) => m.id)
    const next = checked ? [...new Set([...cur, mid])] : cur.filter((x) => x !== mid)
    setForm({ ...form, model_allowlist: next.length === models.length ? null : next })
  }

  const save = async () => {
    try {
      await api(`/admin/agents/${agent.id}`, { method: 'PATCH', body: form })
      toast.success('已保存')
      onSaved()
    } catch (e) {
      toast.error(e.message)
    }
  }

  return (
    <div className="max-w-xl space-y-4">
      <div className="space-y-1.5">
        <Label>名称</Label>
        <Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
      </div>
      <div className="space-y-1.5">
        <Label>描述</Label>
        <Textarea value={form.description || ''} onChange={(e) => setForm({ ...form, description: e.target.value })} rows={2} />
      </div>
      <div className="space-y-1.5">
        <Label>默认模型</Label>
        <Select value={form.default_model_id ? String(form.default_model_id) : 'none'} onValueChange={(v) => setForm({ ...form, default_model_id: v === 'none' ? null : Number(v) })}>
          <SelectTrigger><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="none">（未设置）</SelectItem>
            {models.map((m) => <SelectItem key={m.id} value={String(m.id)}>{m.display_name}</SelectItem>)}
          </SelectContent>
        </Select>
      </div>
      <div className="space-y-2">
        <Label>可用模型（全勾 = 不限制）</Label>
        <div className="grid grid-cols-2 gap-2 rounded-md border p-3">
          {models.map((m) => {
            const checked = form.model_allowlist === null || form.model_allowlist.includes(m.id)
            return (
              <label key={m.id} className="flex items-center gap-2 text-sm">
                <Checkbox checked={checked} onCheckedChange={(c) => toggleAllowlist(m.id, !!c)} />
                {m.display_name}
              </label>
            )
          })}
        </div>
      </div>
      <div className="flex items-center justify-between rounded-md border p-3">
        <div>
          <div className="text-sm font-medium">允许代码执行</div>
          <div className="text-xs text-muted-foreground">在隔离沙箱中执行 Python（数据分析/绘图），产物自动存回工作区</div>
        </div>
        <Switch checked={form.allow_code_exec} onCheckedChange={(v) => setForm({ ...form, allow_code_exec: v })} />
      </div>
      <div className="flex items-center justify-between rounded-md border p-3">
        <div>
          <div className="text-sm font-medium">允许联网搜索</div>
          <div className="text-xs text-muted-foreground">可实时搜索网页并读取网页正文（行业调研/新闻/政策等）</div>
        </div>
        <Switch checked={form.allow_web_search} onCheckedChange={(v) => setForm({ ...form, allow_web_search: v })} />
      </div>
      <div className="flex items-center justify-between rounded-md border p-3">
        <div>
          <div className="text-sm font-medium">允许调用外部 API（白名单内）</div>
          <div className="text-xs text-muted-foreground">可调用「网关管理 → API 白名单」中预置的内部接口，认证凭据由平台托管、模型不可见</div>
        </div>
        <Switch checked={form.allow_api_request} onCheckedChange={(v) => setForm({ ...form, allow_api_request: v })} />
      </div>
      <div className="flex items-center justify-between rounded-md border p-3">
        <div>
          <div className="text-sm font-medium">允许读取飞书内容</div>
          <div className="text-xs text-muted-foreground">可读用户本人飞书视野的文档/知识库、总结群聊与单聊（用户需先完成一次飞书数据授权）</div>
        </div>
        <Switch checked={!!form.allow_feishu} onCheckedChange={(v) => setForm({ ...form, allow_feishu: v })} />
      </div>
      <div className="flex items-center justify-between rounded-md border p-3">
        <div>
          <div className="text-sm font-medium">允许读取智书合同</div>
          <div className="text-xs text-muted-foreground">可读用户本人可见/申请的智书合同（需管理员先在「组织与权限 → 人员」里为该用户填智书 user_id）</div>
        </div>
        <Switch checked={!!form.allow_zhishu} onCheckedChange={(v) => setForm({ ...form, allow_zhishu: v })} />
      </div>
      <div className="flex items-center justify-between rounded-md border p-3">
        <div>
          <div className="text-sm font-medium">允许文生图</div>
          <div className="text-xs text-muted-foreground">用文字描述生成图片（火山方舟 Seedream 5.0），图片直接展示在对话里并存入工作区 gen_imgs/</div>
        </div>
        <Switch checked={!!form.allow_image_generate} onCheckedChange={(v) => setForm({ ...form, allow_image_generate: v })} />
      </div>
      <div className="space-y-1.5">
        <Label>最大工具调用轮数（1-100）</Label>
        <Input type="number" min={1} max={100} value={form.max_loop_rounds} onChange={(e) => setForm({ ...form, max_loop_rounds: Number(e.target.value) })} className="w-32" />
      </div>
      <Button onClick={save}>保存基本信息</Button>
    </div>
  )
}

// ---------- 基本信息：owner（仅名称/头像/描述） ----------
function OwnerInfoTab({ agent, base, onSaved }) {
  const [form, setForm] = useState({ name: agent.name, avatar: agent.avatar || '', description: agent.description || '' })
  useEffect(() => setForm({ name: agent.name, avatar: agent.avatar || '', description: agent.description || '' }), [agent])

  const save = async () => {
    try {
      await api(base, { method: 'PATCH', body: { name: form.name, avatar: form.avatar || null, description: form.description } })
      toast.success('已保存')
      onSaved()
    } catch (e) {
      toast.error(e.message)
    }
  }

  return (
    <div className="max-w-xl space-y-4">
      <div className="space-y-1.5">
        <Label>名称</Label>
        <Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
      </div>
      <div className="space-y-1.5">
        <Label>头像（emoji 或图片 URL）</Label>
        <Input value={form.avatar} onChange={(e) => setForm({ ...form, avatar: e.target.value })} placeholder="例如：🎯" />
      </div>
      <div className="space-y-1.5">
        <Label>描述</Label>
        <Textarea value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} rows={2} />
      </div>
      <p className="text-xs text-muted-foreground">模型与代码执行开关由管理员统一管理。</p>
      <Button onClick={save}>保存</Button>
    </div>
  )
}

// ---------- 技能：admin（返回 {bound_ids, skills}，含全部平台+个人技能） ----------
function AdminSkillsTab({ agentId }) {
  const [data, setData] = useState(null)

  const load = useCallback(() => {
    api(`/admin/agents/${agentId}/skills`).then(setData).catch((e) => toast.error(e.message))
  }, [agentId])
  useEffect(load, [load])

  if (!data) return <div className="py-8 text-center text-sm text-muted-foreground">加载中…</div>
  const bound = new Set(data.bound_ids)

  const toggle = async (sid, checked) => {
    const next = new Set(bound)
    if (checked) next.add(sid)
    else next.delete(sid)
    setData({ ...data, bound_ids: [...next] })
    try {
      await api(`/admin/agents/${agentId}/skills`, { method: 'PUT', body: { skill_ids: [...next] } })
      toast.success('技能绑定已更新')
    } catch (e) {
      toast.error(e.message)
      load()
    }
  }

  return (
    <div className="max-w-xl space-y-2">
      <p className="text-sm text-muted-foreground">勾选技能绑定到该数字人（含员工的个人技能，便于技能迁移）。技能在「技能库管理」中维护。</p>
      {data.skills.map((s) => (
        <label key={s.id} className="flex items-start gap-2.5 rounded-md border p-3">
          <Checkbox checked={bound.has(s.id)} onCheckedChange={(c) => toggle(s.id, !!c)} className="mt-0.5" disabled={!s.enabled} />
          <span className="min-w-0">
            <span className="text-sm font-medium">{s.name}</span>
            {s.owner_user_id != null
              ? <Badge variant="secondary" className="ml-2 text-xs">个人 · {s.owner_name || '?'}</Badge>
              : <Badge variant="outline" className="ml-2 text-xs">平台</Badge>}
            {s.category && <Badge variant="outline" className="ml-2 text-xs">{s.category}</Badge>}
            {!s.enabled && <Badge variant="secondary" className="ml-2">已停用</Badge>}
            {s.description && <div className="mt-0.5 text-xs text-muted-foreground">{s.description}</div>}
          </span>
        </label>
      ))}
      {data.skills.length === 0 && <div className="py-8 text-center text-sm text-muted-foreground">暂无技能</div>}
    </div>
  )
}

// ---------- 技能：owner（只列"已拥有"的：我的个人技能 + 已绑定的平台技能；未领用的平台技能去市场） ----------
function OwnerSkillsTab({ agentId }) {
  const [data, setData] = useState(null)
  const [viewing, setViewing] = useState(null) // {skill, files}
  const me = getUser()

  const load = useCallback(() => {
    api(`/my-agents/${agentId}/skills`).then(setData).catch((e) => toast.error(e.message))
  }, [agentId])
  useEffect(load, [load])

  const toggle = async (sid, checked) => {
    const next = new Set(data.bound_ids)
    if (checked) next.add(sid)
    else next.delete(sid)
    try {
      await api(`/my-agents/${agentId}/skills`, { method: 'PUT', body: { skill_ids: [...next] } })
      load()
    } catch (e) {
      toast.error(e.message)
    }
  }

  const view = async (s) => {
    try {
      const files = s.owner_user_id === null
        ? [{ path: 'SKILL.md', content: s.instructions }] // 平台技能：员工只读主文件
        : await api(`/skills/mine/${s.id}/files`)
      // mine=自己的个人技能才允许在预览弹窗删文件（别人的个人技能只是绑定可读）
      setViewing({ skill: s, files, mine: s.owner_user_id !== null && s.owner_user_id === me?.id })
    } catch (e) {
      toast.error(e.message)
    }
  }

  if (!data) return null
  const bound = new Set(data.bound_ids)
  // 已拥有 = 我的个人技能 + 当前已绑定到本数字人的技能
  const owned = data.skills.filter((s) => s.owner_user_id === me?.id || bound.has(s.id))

  return (
    <div className="max-w-2xl space-y-2">
      <p className="text-sm text-muted-foreground">
        勾选即绑定到你的数字人；技能的编辑管理统一在左侧「市场 → 我的技能」进行。
      </p>
      {owned.map((s) => {
        const isMine = s.owner_user_id !== null && s.owner_user_id === me?.id
        return (
          <div key={s.id} className="flex items-start gap-2.5 rounded-md border p-3">
            <Checkbox checked={bound.has(s.id)} onCheckedChange={(c) => toggle(s.id, !!c)} className="mt-0.5" disabled={!s.enabled} />
            <div className="min-w-0 flex-1">
              <span className="text-sm font-medium">{s.name}</span>
              {s.category && <Badge variant="outline" className="ml-2 text-xs">{s.category}</Badge>}
              {isMine
                ? <Badge variant="secondary" className="ml-2 text-xs">我的</Badge>
                : s.owner_user_id !== null
                  ? <Badge variant="outline" className="ml-2 text-xs">个人 · 只读</Badge>
                  : <Badge variant="outline" className="ml-2 text-xs">平台 · 只读</Badge>}
              {!s.enabled && <Badge variant="secondary" className="ml-2">已停用</Badge>}
              {s.description && <div className="mt-0.5 text-xs text-muted-foreground">{s.description}</div>}
            </div>
            <Button variant="ghost" size="icon" className="h-7 w-7" title="查看内容" onClick={() => view(s)}><Eye className="h-3.5 w-3.5" /></Button>
          </div>
        )
      })}
      {owned.length === 0 && (
        <div className="py-8 text-center text-sm text-muted-foreground">暂无技能，去左侧「市场 → 技能市场」领用</div>
      )}

      <SkillViewDialog
        viewing={viewing}
        onClose={() => setViewing(null)}
        canDelete={!!viewing?.mine}
        onFilesChange={(rows) => setViewing((v) => (v ? { ...v, files: rows } : v))}
      />
    </div>
  )
}

export function SkillViewDialog({ viewing, onClose, canDelete = false, onFilesChange }) {
  const [path, setPath] = useState('SKILL.md')
  const [deleting, setDeleting] = useState(false)
  useEffect(() => setPath('SKILL.md'), [viewing?.skill?.id])
  if (!viewing) return null
  const { skill, files } = viewing
  // .keep 是空文件夹占位文件，预览不展示
  const visible = files.filter((f) => !f.path.endsWith('/.keep') && f.path !== '.keep')
  const file = visible.find((f) => f.path === path) || visible[0]

  // 删除单个文件（仅自己的技能）：全量替换语义，PUT 剩余文件列表
  const removeFile = async (f) => {
    if (f.path === 'SKILL.md') {
      toast.error('SKILL.md 是主文件，不能删除')
      return
    }
    if (!(await confirmDialog({ title: '删除文件', desc: `确定从技能「${skill.name}」中删除 ${f.path}？`, confirmText: '删除', danger: true }))) return
    setDeleting(true)
    try {
      const rows = await api(`/skills/mine/${skill.id}/files`, {
        method: 'PUT',
        body: { files: files.filter((x) => x.path !== f.path) },
      })
      onFilesChange?.(rows)
      if (path === f.path) setPath('SKILL.md')
      toast.success(`已删除 ${f.path}`)
    } catch (e) {
      toast.error(e.message)
    } finally {
      setDeleting(false)
    }
  }

  return (
    <Dialog open onOpenChange={onClose}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle className="font-mono text-base">{skill.name}</DialogTitle>
          <DialogDescription>{skill.description || '技能内容'}</DialogDescription>
        </DialogHeader>
        {visible.length > 1 && (
          <div className="flex flex-wrap gap-1.5">
            {visible.map((f) => (
              <span key={f.path} className="group inline-flex max-w-full items-center">
                <Button
                  size="sm"
                  variant={f.path === file?.path ? 'default' : 'outline'}
                  className={cn('h-7 max-w-56 font-mono text-xs', canDelete && f.path !== 'SKILL.md' && 'rounded-r-none')}
                  title={f.path}  /* 悬停显示完整路径 */
                  onClick={() => setPath(f.path)}
                >
                  <span className="truncate">{f.path}</span>
                </Button>
                {canDelete && f.path !== 'SKILL.md' && (
                  <button
                    className="flex h-7 w-6 items-center justify-center rounded-r-md border border-l-0 border-input bg-background text-muted-foreground opacity-0 transition-opacity hover:bg-destructive/10 hover:text-destructive group-hover:opacity-100 disabled:opacity-40"
                    title={`删除 ${f.path}`}
                    disabled={deleting}
                    onClick={() => removeFile(f)}
                  >
                    <X className="h-3 w-3" />
                  </button>
                )}
              </span>
            ))}
          </div>
        )}
        <pre className="max-h-[50vh] overflow-auto whitespace-pre-wrap break-all rounded-md border bg-muted/40 p-3 font-mono text-xs">{file?.content || '（空）'}</pre>
      </DialogContent>
    </Dialog>
  )
}

// ---------- 派发（admin，模板） ----------
function DispatchDialog({ templateId }) {
  const [open, setOpen] = useState(false)
  const [users, setUsers] = useState([])
  const [uid, setUid] = useState(null)

  useEffect(() => {
    if (open) api('/auth/users').then(setUsers).catch((e) => toast.error(e.message))
  }, [open])

  const dispatch = async () => {
    try {
      const inst = await api(`/admin/agents/${templateId}/dispatch`, { method: 'POST', body: { user_id: uid } })
      toast.success(`已派发：${inst.name}`)
      setOpen(false)
    } catch (e) {
      toast.error(e.message)
    }
  }

  return (
    <>
      <Button variant="outline" size="sm" onClick={() => setOpen(true)}><Send className="mr-1.5 h-3.5 w-3.5" /> 派发</Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>派发模板</DialogTitle>
            <DialogDescription>为选中的员工创建一份独立实例（已领用过的员工不会重复创建）</DialogDescription>
          </DialogHeader>
          <Select value={uid ? String(uid) : undefined} onValueChange={(v) => setUid(Number(v))}>
            <SelectTrigger><SelectValue placeholder="选择员工" /></SelectTrigger>
            <SelectContent>
              {users.map((u) => <SelectItem key={u.id} value={String(u.id)}>{u.name}（{u.username}）</SelectItem>)}
            </SelectContent>
          </Select>
          <div className="flex justify-end"><Button onClick={dispatch} disabled={!uid}>派发</Button></div>
        </DialogContent>
      </Dialog>
    </>
  )
}

// ---------- 可领用范围（admin，模板） ----------
function ClaimGrantsDialog({ templateId }) {
  const [open, setOpen] = useState(false)
  const [grants, setGrants] = useState([])
  const [users, setUsers] = useState([])
  const [depts, setDepts] = useState([])
  const [form, setForm] = useState({ grantee_type: 'all', grantee_id: '' })

  const load = useCallback(() => {
    api(`/admin/agents/${templateId}/claim-grants`).then(setGrants).catch((e) => toast.error(e.message))
  }, [templateId])
  useEffect(() => {
    if (open) {
      load()
      api('/auth/users').then(setUsers).catch(() => {})
      api('/admin/departments').then(setDepts).catch(() => {})
    }
  }, [open, load])

  const add = async () => {
    try {
      await api(`/admin/agents/${templateId}/claim-grants`, {
        method: 'POST',
        body: { grantee_type: form.grantee_type, grantee_id: form.grantee_type === 'user' ? Number(form.grantee_id) : form.grantee_type === 'department' ? Number(form.grantee_id) : null },
      })
      toast.success('已添加')
      load()
    } catch (e) {
      toast.error(e.message)
    }
  }

  const remove = async (gid) => {
    try {
      await api(`/admin/agents/claim-grants/${gid}`, { method: 'DELETE' })
      load()
    } catch (e) {
      toast.error(e.message)
    }
  }

  const label = (g) =>
    g.grantee_type === 'all' ? '全员可领用' : g.grantee_type === 'user' ? `用户：${users.find((u) => u.id === g.grantee_id)?.name || '#' + g.grantee_id}` : `部门：${depts.find((d) => d.id === g.grantee_id)?.name || '#' + g.grantee_id}`

  return (
    <>
      <Button variant="outline" size="sm" onClick={() => setOpen(true)}><ShieldCheck className="mr-1.5 h-3.5 w-3.5" /> 可领用范围</Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>可领用范围</DialogTitle>
            <DialogDescription>不在范围内的员工在首页看不到这个模板。管理员派发不受此限制。</DialogDescription>
          </DialogHeader>
          <div className="space-y-1.5">
            {grants.map((g) => (
              <div key={g.id} className="flex items-center justify-between rounded-md border px-3 py-2 text-sm">
                <span>{label(g)}</span>
                <Button variant="ghost" size="sm" className="h-7 text-xs" onClick={() => remove(g.id)}>移除</Button>
              </div>
            ))}
            {grants.length === 0 && <div className="py-4 text-center text-xs text-muted-foreground">未设置——员工暂不可自助领用</div>}
          </div>
          <div className="flex items-end gap-2 border-t pt-3">
            <div className="space-y-1.5">
              <Label className="text-xs">范围</Label>
              <Select value={form.grantee_type} onValueChange={(v) => setForm({ grantee_type: v, grantee_id: '' })}>
                <SelectTrigger className="w-32"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">全员</SelectItem>
                  <SelectItem value="user">指定用户</SelectItem>
                  <SelectItem value="department">指定部门</SelectItem>
                </SelectContent>
              </Select>
            </div>
            {form.grantee_type === 'user' && (
              <Select value={form.grantee_id} onValueChange={(v) => setForm({ ...form, grantee_id: v })}>
                <SelectTrigger className="w-40"><SelectValue placeholder="选择用户" /></SelectTrigger>
                <SelectContent>
                  {users.map((u) => <SelectItem key={u.id} value={String(u.id)}>{u.name}</SelectItem>)}
                </SelectContent>
              </Select>
            )}
            {form.grantee_type === 'department' && (
              <DepartmentSelect className="w-48" value={form.grantee_id} onValueChange={(v) => setForm({ ...form, grantee_id: v })} departments={depts} />
            )}
            <Button onClick={add} disabled={form.grantee_type !== 'all' && !form.grantee_id}>添加</Button>
          </div>
        </DialogContent>
      </Dialog>
    </>
  )
}
