import { Download, Pencil, Plus, Trash2 } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { toast } from 'sonner'
import { api } from '@/api'
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
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Textarea } from '@/components/ui/textarea'

export default function GatewayAdmin() {
  const [providers, setProviders] = useState([])
  const [models, setModels] = useState([])
  const [grants, setGrants] = useState([])
  const [apiHosts, setApiHosts] = useState([])

  const load = useCallback(() => {
    api('/admin/gateway/providers').then(setProviders).catch((e) => toast.error(e.message))
    api('/admin/gateway/models').then(setModels).catch((e) => toast.error(e.message))
    api('/admin/gateway/grants').then(setGrants).catch((e) => toast.error(e.message))
    api('/admin/api-hosts').then(setApiHosts).catch((e) => toast.error(e.message))
  }, [])
  useEffect(load, [load])

  return (
    <div className="h-full overflow-y-auto p-8">
      <div className="mx-auto max-w-5xl">
        <h1 className="text-2xl font-semibold tracking-tight">模型网关</h1>
        <p className="mt-1 text-sm text-muted-foreground">统一管理供应商与模型 Key，按人/部门授权模型。Key 加密存储，永不下发。</p>
        <Tabs defaultValue="providers" className="mt-6">
          <TabsList>
            <TabsTrigger value="providers">供应商</TabsTrigger>
            <TabsTrigger value="models">模型</TabsTrigger>
            <TabsTrigger value="grants">模型授权</TabsTrigger>
            <TabsTrigger value="apihosts">API 白名单</TabsTrigger>
          </TabsList>
          <TabsContent value="providers" className="mt-4"><ProvidersTab providers={providers} onChanged={load} /></TabsContent>
          <TabsContent value="models" className="mt-4"><ModelsTab models={models} providers={providers} onChanged={load} /></TabsContent>
          <TabsContent value="grants" className="mt-4"><GrantsTab grants={grants} models={models} onChanged={load} /></TabsContent>
          <TabsContent value="apihosts" className="mt-4"><ApiHostsTab hosts={apiHosts} onChanged={load} /></TabsContent>
        </Tabs>
      </div>
    </div>
  )
}

// ---------- 供应商 ----------
const EMPTY_PROVIDER = { name: '', slug: '', base_url: '', api_key: '', enabled: true }

function ProvidersTab({ providers, onChanged }) {
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState(null)
  const [form, setForm] = useState(EMPTY_PROVIDER)
  const [remote, setRemote] = useState(null)
  const [remoteLoading, setRemoteLoading] = useState(false)

  const openEdit = (p) => {
    setEditing(p)
    setForm(p ? { name: p.name, slug: p.slug, base_url: p.base_url, api_key: '', enabled: p.enabled } : EMPTY_PROVIDER)
    setOpen(true)
  }

  const save = async (e) => {
    e.preventDefault()
    try {
      if (editing) {
        const body = { name: form.name, base_url: form.base_url, enabled: form.enabled }
        if (form.api_key) body.api_key = form.api_key
        await api(`/admin/gateway/providers/${editing.id}`, { method: 'PATCH', body })
      } else {
        await api('/admin/gateway/providers', { method: 'POST', body: form })
      }
      toast.success('已保存')
      setOpen(false)
      onChanged()
    } catch (err) {
      toast.error(err.message)
    }
  }

  const remove = async (p) => {
    if (!(await confirmDialog({ title: '删除供应商', desc: `确定删除供应商「${p.name}」？`, confirmText: '删除', danger: true }))) return
    try {
      await api(`/admin/gateway/providers/${p.id}`, { method: 'DELETE' })
      toast.success('已删除')
      onChanged()
    } catch (e) {
      toast.error(e.message)
    }
  }

  const fetchRemote = async (p) => {
    setRemoteLoading(true)
    try {
      const rows = await api(`/admin/gateway/providers/${p.id}/remote-models`)
      setRemote({ provider: p, models: rows })
    } catch (e) {
      toast.error(e.message)
    } finally {
      setRemoteLoading(false)
    }
  }

  return (
    <div>
      <div className="mb-3 flex justify-end">
        <Button onClick={() => openEdit(null)}><Plus className="mr-1.5 h-4 w-4" /> 添加供应商</Button>
      </div>
      <div className="rounded-2xl border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>名称</TableHead>
              <TableHead>Base URL</TableHead>
              <TableHead className="w-20">状态</TableHead>
              <TableHead className="w-40 text-right">操作</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {providers.map((p) => (
              <TableRow key={p.id}>
                <TableCell className="font-medium">{p.name}</TableCell>
                <TableCell className="max-w-xs truncate font-mono text-xs text-muted-foreground">{p.base_url}</TableCell>
                <TableCell>{p.enabled ? <Badge>启用</Badge> : <Badge variant="secondary">停用</Badge>}</TableCell>
                <TableCell className="text-right">
                  <Button variant="ghost" size="sm" className="h-8 text-xs" disabled={remoteLoading} onClick={() => fetchRemote(p)}>
                    <Download className="mr-1 h-3.5 w-3.5" /> 拉取模型
                  </Button>
                  <Button variant="ghost" size="icon" className="h-8 w-8" onClick={() => openEdit(p)}><Pencil className="h-3.5 w-3.5" /></Button>
                  <Button variant="ghost" size="icon" className="h-8 w-8" onClick={() => remove(p)}><Trash2 className="h-3.5 w-3.5" /></Button>
                </TableCell>
              </TableRow>
            ))}
            {providers.length === 0 && (
              <TableRow><TableCell colSpan={4} className="py-10 text-center text-sm text-muted-foreground">暂无供应商</TableCell></TableRow>
            )}
          </TableBody>
        </Table>
      </div>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogHeader><DialogTitle>{editing ? `编辑供应商：${editing.name}` : '添加供应商'}</DialogTitle></DialogHeader>
          <form onSubmit={save} className="space-y-3">
            <div className="space-y-1.5">
              <Label>名称</Label>
              <Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required placeholder="例如：月之暗面 Kimi" />
            </div>
            {!editing && (
              <div className="space-y-1.5">
                <Label>标识 slug</Label>
                <Input value={form.slug} onChange={(e) => setForm({ ...form, slug: e.target.value })} required placeholder="kimi" />
              </div>
            )}
            <div className="space-y-1.5">
              <Label>Base URL（OpenAI 兼容端点）</Label>
              <Input value={form.base_url} onChange={(e) => setForm({ ...form, base_url: e.target.value })} required placeholder="https://api.moonshot.cn/v1" />
            </div>
            <div className="space-y-1.5">
              <Label>API Key{editing && '（留空则不更换）'}</Label>
              <Input type="password" value={form.api_key} onChange={(e) => setForm({ ...form, api_key: e.target.value })} required={!editing} placeholder="sk-..." />
            </div>
            <div className="flex items-center gap-2">
              <Switch checked={form.enabled} onCheckedChange={(v) => setForm({ ...form, enabled: v })} />
              <Label>启用</Label>
            </div>
            <div className="flex justify-end"><Button type="submit">保存</Button></div>
          </form>
        </DialogContent>
      </Dialog>

      <Dialog open={!!remote} onOpenChange={() => setRemote(null)}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>{remote?.provider.name} · 可用模型</DialogTitle>
            <DialogDescription>该 Key 实际可调用的模型，把 model_id 原样填到「模型」页新建即可</DialogDescription>
          </DialogHeader>
          <div className="max-h-96 space-y-1 overflow-y-auto">
            {remote?.models.map((m) => (
              <div key={m.model_id} className="rounded bg-muted px-3 py-1.5 font-mono text-xs">{m.model_id}</div>
            ))}
          </div>
        </DialogContent>
      </Dialog>
    </div>
  )
}

// ---------- 模型 ----------
function ModelsTab({ models, providers, onChanged }) {
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState(null)
  const [form, setForm] = useState(null)
  const providerName = (pid) => providers.find((p) => p.id === pid)?.name || `#${pid}`

  const openEdit = (m) => {
    setEditing(m)
    setForm(
      m
        ? { provider_id: m.provider_id, model_id: m.model_id, display_name: m.display_name, model_type: m.model_type, price_input_per_1k: m.price_input_per_1k, price_output_per_1k: m.price_output_per_1k, context_window: m.context_window, supports_vision: !!m.supports_vision, enabled: m.enabled }
        : { provider_id: providers[0]?.id, model_id: '', display_name: '', model_type: 'chat', price_input_per_1k: 0, price_output_per_1k: 0, context_window: null, supports_vision: false, enabled: true },
    )
    setOpen(true)
  }

  const save = async (e) => {
    e.preventDefault()
    try {
      const body = { ...form, context_window: form.context_window ? Number(form.context_window) : null }
      if (editing) await api(`/admin/gateway/models/${editing.id}`, { method: 'PATCH', body })
      else await api('/admin/gateway/models', { method: 'POST', body })
      toast.success('已保存')
      setOpen(false)
      onChanged()
    } catch (err) {
      toast.error(err.message)
    }
  }

  const remove = async (m) => {
    if (!(await confirmDialog({ title: '删除模型', desc: `确定删除模型「${m.display_name}」？其授权将一并失效。`, confirmText: '删除', danger: true }))) return
    try {
      await api(`/admin/gateway/models/${m.id}`, { method: 'DELETE' })
      toast.success('已删除')
      onChanged()
    } catch (e) {
      toast.error(e.message)
    }
  }

  return (
    <div>
      <div className="mb-3 flex justify-end">
        <Button onClick={() => openEdit(null)} disabled={providers.length === 0}><Plus className="mr-1.5 h-4 w-4" /> 添加模型</Button>
      </div>
      <div className="rounded-2xl border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>显示名</TableHead>
              <TableHead>model_id</TableHead>
              <TableHead>供应商</TableHead>
              <TableHead>类型</TableHead>
              <TableHead className="w-16">能力</TableHead>
              <TableHead className="w-28">上下文窗口</TableHead>
              <TableHead className="w-20">状态</TableHead>
              <TableHead className="w-24 text-right">操作</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {models.map((m) => (
              <TableRow key={m.id}>
                <TableCell className="font-medium">{m.display_name}</TableCell>
                <TableCell className="font-mono text-xs text-muted-foreground">{m.model_id}</TableCell>
                <TableCell className="text-sm">{providerName(m.provider_id)}</TableCell>
                <TableCell><Badge variant="outline">{m.model_type === 'chat' ? '对话' : '嵌入'}</Badge></TableCell>
                <TableCell>{m.supports_vision ? <Badge variant="outline" className="text-xs">视觉</Badge> : <span className="text-xs text-muted-foreground">—</span>}</TableCell>
                <TableCell className="text-sm">{m.context_window?.toLocaleString() || '—'}</TableCell>
                <TableCell>{m.enabled ? <Badge>启用</Badge> : <Badge variant="secondary">停用</Badge>}</TableCell>
                <TableCell className="text-right">
                  <Button variant="ghost" size="icon" className="h-8 w-8" onClick={() => openEdit(m)}><Pencil className="h-3.5 w-3.5" /></Button>
                  <Button variant="ghost" size="icon" className="h-8 w-8" onClick={() => remove(m)}><Trash2 className="h-3.5 w-3.5" /></Button>
                </TableCell>
              </TableRow>
            ))}
            {models.length === 0 && (
              <TableRow><TableCell colSpan={8} className="py-10 text-center text-sm text-muted-foreground">暂无模型</TableCell></TableRow>
            )}
          </TableBody>
        </Table>
      </div>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogHeader><DialogTitle>{editing ? '编辑模型' : '添加模型'}</DialogTitle></DialogHeader>
          {form && (
            <form onSubmit={save} className="space-y-3">
              <div className="space-y-1.5">
                <Label>供应商</Label>
                <Select value={String(form.provider_id)} onValueChange={(v) => setForm({ ...form, provider_id: Number(v) })}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    {providers.map((p) => <SelectItem key={p.id} value={String(p.id)}>{p.name}</SelectItem>)}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1.5">
                <Label>model_id（必须与供应商 API 完全一致）</Label>
                <Input value={form.model_id} onChange={(e) => setForm({ ...form, model_id: e.target.value })} required placeholder="kimi-k2-0905-preview" className="font-mono" />
              </div>
              <div className="space-y-1.5">
                <Label>显示名</Label>
                <Input value={form.display_name} onChange={(e) => setForm({ ...form, display_name: e.target.value })} required placeholder="Kimi K2" />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1.5">
                  <Label>类型</Label>
                  <Select value={form.model_type} onValueChange={(v) => setForm({ ...form, model_type: v })}>
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="chat">对话</SelectItem>
                      <SelectItem value="embedding">嵌入</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-1.5">
                  <Label>上下文窗口 tokens</Label>
                  <Input type="number" value={form.context_window ?? ''} onChange={(e) => setForm({ ...form, context_window: e.target.value })} placeholder="131072" />
                </div>
                <div className="space-y-1.5">
                  <Label>输入价（元/千 token）</Label>
                  <Input type="number" step="0.0001" value={form.price_input_per_1k} onChange={(e) => setForm({ ...form, price_input_per_1k: Number(e.target.value) })} />
                </div>
                <div className="space-y-1.5">
                  <Label>输出价（元/千 token）</Label>
                  <Input type="number" step="0.0001" value={form.price_output_per_1k} onChange={(e) => setForm({ ...form, price_output_per_1k: Number(e.target.value) })} />
                </div>
              </div>
              <div className="flex items-center gap-6">
                <div className="flex items-center gap-2">
                  <Switch checked={form.enabled} onCheckedChange={(v) => setForm({ ...form, enabled: v })} />
                  <Label>启用</Label>
                </div>
                <div className="flex items-center gap-2">
                  <Switch checked={form.supports_vision} onCheckedChange={(v) => setForm({ ...form, supports_vision: v })} />
                  <Label>支持图片输入（视觉）</Label>
                </div>
              </div>
              <div className="flex justify-end"><Button type="submit">保存</Button></div>
            </form>
          )}
        </DialogContent>
      </Dialog>
    </div>
  )
}

// ---------- 授权 ----------
function GrantsTab({ grants, models, onChanged }) {
  const [open, setOpen] = useState(false)
  const [form, setForm] = useState({ model_id: null, grantee_type: 'all', grantee_id: '' })
  const [depts, setDepts] = useState([])
  const [users, setUsers] = useState([])
  const modelName = (mid) => models.find((m) => m.id === mid)?.display_name || `#${mid}`

  useEffect(() => {
    api('/admin/departments').then(setDepts).catch(() => {})
    api('/auth/users').then(setUsers).catch(() => {})
  }, [])

  const save = async (e) => {
    e.preventDefault()
    try {
      await api('/admin/gateway/grants', {
        method: 'POST',
        body: { model_id: form.model_id, grantee_type: form.grantee_type, grantee_id: form.grantee_type === 'all' ? null : Number(form.grantee_id) },
      })
      toast.success('已授权')
      setOpen(false)
      onChanged()
    } catch (err) {
      toast.error(err.message)
    }
  }

  const remove = async (g) => {
    try {
      await api(`/admin/gateway/grants/${g.id}`, { method: 'DELETE' })
      toast.success('已移除')
      onChanged()
    } catch (e) {
      toast.error(e.message)
    }
  }

  const granteeLabel = (g) =>
    g.grantee_type === 'all'
      ? '全员'
      : g.grantee_type === 'department'
        ? `部门：${depts.find((d) => d.id === g.grantee_id)?.name || '#' + g.grantee_id}`
        : `用户：${users.find((u) => u.id === g.grantee_id)?.name || '#' + g.grantee_id}`

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <p className="text-xs text-muted-foreground">未授权的模型员工不可见。可按部门或用户精确授权。</p>
        <Button onClick={() => { setForm({ model_id: models[0]?.id ?? null, grantee_type: 'all', grantee_id: '' }); setOpen(true) }} disabled={models.length === 0}>
          <Plus className="mr-1.5 h-4 w-4" /> 添加授权
        </Button>
      </div>
      <div className="rounded-2xl border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>模型</TableHead>
              <TableHead>授权对象</TableHead>
              <TableHead className="w-20 text-right">操作</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {grants.map((g) => (
              <TableRow key={g.id}>
                <TableCell className="font-medium">{modelName(g.model_id)}</TableCell>
                <TableCell><Badge variant={g.grantee_type === 'all' ? 'default' : 'secondary'}>{granteeLabel(g)}</Badge></TableCell>
                <TableCell className="text-right">
                  <Button variant="ghost" size="icon" className="h-8 w-8" onClick={() => remove(g)}><Trash2 className="h-3.5 w-3.5" /></Button>
                </TableCell>
              </TableRow>
            ))}
            {grants.length === 0 && (
              <TableRow><TableCell colSpan={3} className="py-10 text-center text-sm text-muted-foreground">暂无授权</TableCell></TableRow>
            )}
          </TableBody>
        </Table>
      </div>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogHeader><DialogTitle>添加模型授权</DialogTitle></DialogHeader>
          <form onSubmit={save} className="space-y-3">
            <div className="space-y-1.5">
              <Label>模型</Label>
              <Select value={form.model_id ? String(form.model_id) : undefined} onValueChange={(v) => setForm({ ...form, model_id: Number(v) })}>
                <SelectTrigger><SelectValue placeholder="选择模型" /></SelectTrigger>
                <SelectContent>
                  {models.map((m) => <SelectItem key={m.id} value={String(m.id)}>{m.display_name}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label>授权对象</Label>
              {[
                { v: 'all', label: '全员' },
                { v: 'department', label: '指定部门' },
                { v: 'user', label: '指定用户' },
              ].map((o) => (
                <label key={o.v} className="flex items-center gap-2 text-sm">
                  <Checkbox checked={form.grantee_type === o.v} onCheckedChange={() => setForm({ ...form, grantee_type: o.v, grantee_id: '' })} />
                  {o.label}
                </label>
              ))}
            </div>
            {form.grantee_type === 'department' && (
              <DepartmentSelect value={form.grantee_id} onValueChange={(v) => setForm({ ...form, grantee_id: v })} departments={depts} />
            )}
            {form.grantee_type === 'user' && (
              <Select value={form.grantee_id} onValueChange={(v) => setForm({ ...form, grantee_id: v })}>
                <SelectTrigger><SelectValue placeholder="选择用户" /></SelectTrigger>
                <SelectContent>
                  {users.map((u) => <SelectItem key={u.id} value={String(u.id)}>{u.name}（{u.username}）</SelectItem>)}
                </SelectContent>
              </Select>
            )}
            <div className="flex justify-end"><Button type="submit" disabled={!form.model_id}>保存</Button></div>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  )
}

// ---------- API 白名单 ----------
// 红线：认证配置（密钥/账号密码）只进不出——后端永不回传，编辑态留空 = 不修改。
const EMPTY_HOST = { host: '', label: '', auth_type: 'none', headersJson: '', login_path: '', login_bodyJson: '', allowed_methods: 'GET,POST', min_interval_ms: 0, enabled: true }
const AUTH_TYPE_LABEL = { none: '无认证', headers: '固定请求头', login_session: '登录换 Cookie' }

function ApiHostsTab({ hosts, onChanged }) {
  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState(null)
  const [form, setForm] = useState(EMPTY_HOST)

  const openEdit = (h) => {
    setEditing(h)
    setForm(
      h
        ? { host: h.host, label: h.label, auth_type: h.auth_type, headersJson: '', login_path: '', login_bodyJson: '', allowed_methods: h.allowed_methods, min_interval_ms: h.min_interval_ms, enabled: h.enabled }
        : EMPTY_HOST,
    )
    setOpen(true)
  }

  const parseJsonField = (raw, name, { required = false } = {}) => {
    const text = (raw || '').trim()
    if (!text) {
      if (required) throw new Error(`${name} 不能为空`)
      return undefined
    }
    try {
      const v = JSON.parse(text)
      if (!v || typeof v !== 'object' || Array.isArray(v)) throw new Error()
      return v
    } catch {
      throw new Error(`${name} 不是合法的 JSON 对象`)
    }
  }

  const save = async (e) => {
    e.preventDefault()
    try {
      const creating = !editing
      const authTypeChanged = editing && form.auth_type !== editing.auth_type
      // headersJson / login_bodyJson 任一填了就视为要更新认证配置
      const authFilled = !!(form.headersJson.trim() || form.login_bodyJson.trim() || form.login_path.trim())
      let authConfig
      if (form.auth_type !== 'none' && (creating || authTypeChanged || authFilled)) {
        const headers = parseJsonField(form.headersJson, '固定请求头')
        if (form.auth_type === 'headers') {
          if (!headers || Object.keys(headers).length === 0) throw new Error('固定请求头认证至少要有一个请求头')
          authConfig = { headers }
        } else {
          if (!form.login_path.trim()) throw new Error('登录路径不能为空')
          const loginBody = parseJsonField(form.login_bodyJson, '登录参数', { required: true })
          authConfig = { ...(headers ? { headers } : {}), login_path: form.login_path.trim(), login_body: loginBody }
        }
      }

      const body = {
        label: form.label,
        allowed_methods: form.allowed_methods,
        min_interval_ms: Number(form.min_interval_ms) || 0,
        enabled: form.enabled,
      }
      if (creating) {
        body.host = form.host
        body.auth_type = form.auth_type
        if (authConfig) body.auth_config = authConfig
        await api('/admin/api-hosts', { method: 'POST', body })
      } else {
        if (form.host !== editing.host) body.host = form.host
        if (authTypeChanged) {
          body.auth_type = form.auth_type
          // 换认证类型必须同时给新配置（none 除外）
          if (form.auth_type !== 'none' && !authConfig) throw new Error('更换认证方式时必须填写新的认证配置')
          if (authConfig) body.auth_config = authConfig
        } else if (authFilled && authConfig) {
          body.auth_config = authConfig
        }
        await api(`/admin/api-hosts/${editing.id}`, { method: 'PATCH', body })
      }
      toast.success('已保存')
      setOpen(false)
      onChanged()
    } catch (err) {
      toast.error(err.message)
    }
  }

  const remove = async (h) => {
    if (!(await confirmDialog({ title: '删除白名单条目', desc: `确定把「${h.host}」移出白名单？数字人将立即无法再调用它。`, confirmText: '删除', danger: true }))) return
    try {
      await api(`/admin/api-hosts/${h.id}`, { method: 'DELETE' })
      toast.success('已删除')
      onChanged()
    } catch (e) {
      toast.error(e.message)
    }
  }

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <p className="text-xs text-muted-foreground">数字人的 api_request 工具只能调用这里的域名；认证凭据由平台加密托管，模型与前端都拿不到。</p>
        <Button onClick={() => openEdit(null)}><Plus className="mr-1.5 h-4 w-4" /> 添加域名</Button>
      </div>
      <div className="rounded-2xl border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>域名</TableHead>
              <TableHead>备注</TableHead>
              <TableHead>认证方式</TableHead>
              <TableHead>允许方法</TableHead>
              <TableHead className="w-24">最小间隔</TableHead>
              <TableHead className="w-20">状态</TableHead>
              <TableHead className="w-24 text-right">操作</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {hosts.map((h) => (
              <TableRow key={h.id}>
                <TableCell className="font-mono text-xs font-medium">{h.host}</TableCell>
                <TableCell className="text-sm text-muted-foreground">{h.label || '—'}</TableCell>
                <TableCell>
                  <Badge variant="outline">{AUTH_TYPE_LABEL[h.auth_type] || h.auth_type}</Badge>
                  {h.has_auth_config && <span className="ml-1.5 text-xs text-emerald-600">已配置</span>}
                </TableCell>
                <TableCell className="font-mono text-xs">{h.allowed_methods}</TableCell>
                <TableCell className="text-sm">{h.min_interval_ms ? `${h.min_interval_ms}ms` : '—'}</TableCell>
                <TableCell>{h.enabled ? <Badge>启用</Badge> : <Badge variant="secondary">停用</Badge>}</TableCell>
                <TableCell className="text-right">
                  <Button variant="ghost" size="icon" className="h-8 w-8" onClick={() => openEdit(h)}><Pencil className="h-3.5 w-3.5" /></Button>
                  <Button variant="ghost" size="icon" className="h-8 w-8" onClick={() => remove(h)}><Trash2 className="h-3.5 w-3.5" /></Button>
                </TableCell>
              </TableRow>
            ))}
            {hosts.length === 0 && (
              <TableRow><TableCell colSpan={7} className="py-10 text-center text-sm text-muted-foreground">暂无白名单条目</TableCell></TableRow>
            )}
          </TableBody>
        </Table>
      </div>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogHeader><DialogTitle>{editing ? `编辑白名单：${editing.host}` : '添加白名单域名'}</DialogTitle></DialogHeader>
          <form onSubmit={save} className="space-y-3">
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <Label>域名</Label>
                <Input value={form.host} onChange={(e) => setForm({ ...form, host: e.target.value })} required placeholder="api.example.com" className="font-mono" />
                <p className="text-xs text-muted-foreground">粘贴完整 URL 会自动提取域名</p>
              </div>
              <div className="space-y-1.5">
                <Label>备注</Label>
                <Input value={form.label} onChange={(e) => setForm({ ...form, label: e.target.value })} placeholder="BI 数据看板" />
              </div>
            </div>
            <div className="space-y-1.5">
              <Label>认证方式</Label>
              <Select value={form.auth_type} onValueChange={(v) => setForm({ ...form, auth_type: v })}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="none">无认证</SelectItem>
                  <SelectItem value="headers">固定请求头（如 Basic 门禁 / API Key）</SelectItem>
                  <SelectItem value="login_session">登录换 Cookie（账号密码登录，平台自动维持会话）</SelectItem>
                </SelectContent>
              </Select>
            </div>
            {form.auth_type !== 'none' && (
              <div className="space-y-3 rounded-xl border border-dashed p-3">
                {editing && (
                  <p className="text-xs font-medium text-amber-600">认证配置保存后不可回读；以下全部留空 = 不修改现有配置</p>
                )}
                <div className="space-y-1.5">
                  <Label>固定请求头（JSON{form.auth_type === 'login_session' && '，可空'}）</Label>
                  <Textarea
                    value={form.headersJson}
                    onChange={(e) => setForm({ ...form, headersJson: e.target.value })}
                    placeholder='{"Authorization": "Basic ..."}'
                    className="h-20 font-mono text-xs"
                  />
                </div>
                {form.auth_type === 'login_session' && (
                  <>
                    <div className="space-y-1.5">
                      <Label>登录路径</Label>
                      <Input value={form.login_path} onChange={(e) => setForm({ ...form, login_path: e.target.value })} placeholder="/api/auth/login" className="font-mono" />
                    </div>
                    <div className="space-y-1.5">
                      <Label>登录参数（JSON，POST body）</Label>
                      <Textarea
                        value={form.login_bodyJson}
                        onChange={(e) => setForm({ ...form, login_bodyJson: e.target.value })}
                        placeholder='{"phone_number": "...", "password": "..."}'
                        className="h-20 font-mono text-xs"
                      />
                    </div>
                  </>
                )}
              </div>
            )}
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <Label>允许方法（逗号分隔）</Label>
                <Input value={form.allowed_methods} onChange={(e) => setForm({ ...form, allowed_methods: e.target.value })} placeholder="GET" className="font-mono" />
                <p className="text-xs text-muted-foreground">只读接口务必只填 GET</p>
              </div>
              <div className="space-y-1.5">
                <Label>最小调用间隔（毫秒）</Label>
                <Input type="number" min="0" max="60000" value={form.min_interval_ms} onChange={(e) => setForm({ ...form, min_interval_ms: e.target.value })} placeholder="1000" />
                <p className="text-xs text-muted-foreground">同域名强制串行 + 间隔，保护对端</p>
              </div>
            </div>
            <div className="flex items-center gap-2">
              <Switch checked={form.enabled} onCheckedChange={(v) => setForm({ ...form, enabled: v })} />
              <Label>启用</Label>
            </div>
            <div className="flex justify-end"><Button type="submit">保存</Button></div>
          </form>
        </DialogContent>
      </Dialog>
    </div>
  )
}
