// 组织与权限（M3.12）：部门 / 人员 / 权限矩阵 三个 tab。
// 部门：树形 CRUD（删除被引用时后端 400，toast 显示原因）。
// 人员：建号 + 编辑（部门/角色/状态），禁用代替删除；防锁死由后端守卫。
import { useCallback, useEffect, useState } from 'react'
import { Pencil, Plus, UserPlus } from 'lucide-react'
import { toast } from 'sonner'
import { api } from '@/api'
import DepartmentSelect, { deptLabel, deptTree, useDepartments } from '@/components/DepartmentSelect'
import { confirmDialog, promptDialog } from '@/components/dialogs'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'

export default function RolesAdmin() {
  return (
    <div className="h-full overflow-y-auto p-8">
      <div className="mx-auto max-w-4xl">
        <h1 className="text-2xl font-semibold tracking-tight">组织与权限</h1>
        <p className="mt-1 text-sm text-muted-foreground">维护部门与人员账号，并配置角色可见的功能模块。飞书通讯录自动同步将随 M4 上线。</p>
        <Tabs defaultValue="departments" className="mt-6">
          <TabsList>
            <TabsTrigger value="departments">部门</TabsTrigger>
            <TabsTrigger value="users">人员</TabsTrigger>
            <TabsTrigger value="matrix">权限矩阵</TabsTrigger>
          </TabsList>
          <TabsContent value="departments"><DepartmentsTab /></TabsContent>
          <TabsContent value="users"><UsersTab /></TabsContent>
          <TabsContent value="matrix"><MatrixTab /></TabsContent>
        </Tabs>
      </div>
    </div>
  )
}

// ---------- 部门 ----------

function DepartmentsTab() {
  const [depts, reload] = useDepartments()
  const [moving, setMoving] = useState(null) // 正在换父级的部门
  const tree = deptTree(depts)

  const create = async (parent) => {
    const name = await promptDialog({
      title: parent ? `在「${parent.name}」下新建子部门` : '新建一级部门',
      placeholder: '部门名称，如：供应链部',
    })
    if (!name) return
    try {
      await api('/admin/departments', { method: 'POST', body: { name, parent_id: parent?.id ?? null } })
      toast.success('已创建')
      reload()
    } catch (e) {
      toast.error(e.message)
    }
  }

  const rename = async (d) => {
    const name = await promptDialog({ title: '重命名部门', defaultValue: d.name })
    if (!name || name === d.name) return
    try {
      await api(`/admin/departments/${d.id}`, { method: 'PATCH', body: { name, parent_id: d.parent_id } })
      toast.success('已重命名')
      reload()
    } catch (e) {
      toast.error(e.message)
    }
  }

  const remove = async (d) => {
    if (!(await confirmDialog({ title: '删除部门', desc: `确定删除「${d.name}」？有子部门、人员或被授权引用时会被拒绝。`, danger: true }))) return
    try {
      await api(`/admin/departments/${d.id}`, { method: 'DELETE' })
      toast.success('已删除')
      reload()
    } catch (e) {
      toast.error(e.message) // 后端会说明被谁引用
    }
  }

  return (
    <div className="mt-4">
      <div className="mb-3 flex items-center justify-between">
        <p className="text-xs text-muted-foreground">部门用于模型授权、配额与数字人领用范围的「按部门」选项，保存即生效。</p>
        <Button size="sm" onClick={() => create(null)}><Plus className="mr-1.5 h-4 w-4" /> 新建一级部门</Button>
      </div>
      <div className="rounded-2xl border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>部门</TableHead>
              <TableHead className="w-20 text-center">人数</TableHead>
              <TableHead className="w-64 text-right">操作</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {tree.map((d) => (
              <TableRow key={d.id}>
                <TableCell className="text-sm font-medium">{deptLabel(d)}</TableCell>
                <TableCell className="text-center text-sm text-muted-foreground">{d.user_count}</TableCell>
                <TableCell className="text-right">
                  <Button variant="ghost" size="sm" className="h-7 text-xs" onClick={() => create(d)}>加子部门</Button>
                  <Button variant="ghost" size="sm" className="h-7 text-xs" onClick={() => rename(d)}>重命名</Button>
                  <Button variant="ghost" size="sm" className="h-7 text-xs" onClick={() => setMoving(d)}>移动</Button>
                  <Button variant="ghost" size="sm" className="h-7 text-xs text-destructive" onClick={() => remove(d)}>删除</Button>
                </TableCell>
              </TableRow>
            ))}
            {tree.length === 0 && (
              <TableRow><TableCell colSpan={3} className="py-10 text-center text-sm text-muted-foreground">还没有部门，点击右上角新建</TableCell></TableRow>
            )}
          </TableBody>
        </Table>
      </div>
      <MoveDeptDialog dept={moving} departments={depts} onClose={() => setMoving(null)} onDone={() => { setMoving(null); reload() }} />
    </div>
  )
}

function MoveDeptDialog({ dept, departments, onClose, onDone }) {
  const [parent, setParent] = useState('none')
  useEffect(() => {
    setParent(dept?.parent_id ? String(dept.parent_id) : 'none')
  }, [dept])

  const save = async () => {
    try {
      await api(`/admin/departments/${dept.id}`, {
        method: 'PATCH',
        body: { name: dept.name, parent_id: parent === 'none' ? null : Number(parent) },
      })
      toast.success('已移动')
      onDone()
    } catch (e) {
      toast.error(e.message) // 成环等会被后端 400 拒绝
    }
  }

  return (
    <Dialog open={!!dept} onOpenChange={(v) => { if (!v) onClose() }}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader><DialogTitle>移动「{dept?.name}」到…</DialogTitle></DialogHeader>
        <div className="space-y-1.5">
          <Label className="text-xs">新的上级部门</Label>
          <DepartmentSelect value={parent} onValueChange={setParent} allowEmpty excludeId={dept?.id} departments={departments} />
          <p className="text-xs text-muted-foreground">不能移动到它自己的子部门下。</p>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>取消</Button>
          <Button onClick={save}>保存</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// ---------- 人员 ----------

const EMPTY_USER_FORM = { username: '', name: '', role: 'member', department_id: 'none', zhishu_user_id: '', password: '' }

function UsersTab() {
  const [users, setUsers] = useState(null)
  const [depts] = useDepartments()
  const [editing, setEditing] = useState(null) // null=关闭；'new'=新建；user 对象=编辑
  const [deptFilter, setDeptFilter] = useState('all') // 部门筛选：'all'=全部 / 'none'=未分配 / '<id>'

  const load = useCallback(() => {
    api('/admin/users').then(setUsers).catch((e) => toast.error(e.message))
  }, [])
  useEffect(load, [load])

  // 前端筛选（用户列表本就全量加载，无分页）：精确匹配所选部门，子部门各自独立筛选
  const filtered = (users || []).filter((u) =>
    deptFilter === 'all' ? true : deptFilter === 'none' ? !u.department_id : u.department_id === Number(deptFilter),
  )

  const toggleStatus = async (u) => {
    const disabling = u.status === 'active'
    if (disabling && !(await confirmDialog({ title: '禁用账号', desc: `确定禁用「${u.name}（${u.username}）」？其登录态将立即失效，历史数据保留。`, danger: true }))) return
    try {
      await api(`/admin/users/${u.id}`, { method: 'PATCH', body: { status: disabling ? 'disabled' : 'active' } })
      toast.success(disabling ? '已禁用' : '已启用')
      load()
    } catch (e) {
      toast.error(e.message)
    }
  }

  const unbind = async (u) => {
    if (!(await confirmDialog({
      title: '解绑飞书',
      desc: `确定解除「${u.name}」的飞书绑定？解绑后该用户需重新扫码、按姓名自动认领；若账号姓名与飞书姓名不一致将无法认领回来。`,
      danger: true,
    }))) return
    try {
      await api(`/admin/users/${u.id}`, { method: 'PATCH', body: { unbind_feishu: true } })
      toast.success('已解绑')
      load()
    } catch (e) {
      toast.error(e.message)
    }
  }

  return (
    <div className="mt-4">
      <div className="mb-3 flex items-center justify-between">
        <p className="text-xs text-muted-foreground">飞书登录会自动建档并绑定；此处用于提前建号、分配部门与角色。不提供删除，用禁用代替。</p>
        <div className="flex shrink-0 items-center gap-2">
          <Select value={deptFilter} onValueChange={setDeptFilter}>
            <SelectTrigger className="w-44"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部部门</SelectItem>
              <SelectItem value="none">（未分配部门）</SelectItem>
              {deptTree(depts).map((d) => (
                <SelectItem key={d.id} value={String(d.id)}>{deptLabel(d)}</SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button size="sm" onClick={() => setEditing('new')}><UserPlus className="mr-1.5 h-4 w-4" /> 新建账号</Button>
        </div>
      </div>
      <div className="rounded-2xl border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>姓名</TableHead>
              <TableHead>用户名</TableHead>
              <TableHead>部门</TableHead>
              <TableHead>角色</TableHead>
              <TableHead>飞书</TableHead>
              <TableHead>智书</TableHead>
              <TableHead>状态</TableHead>
              <TableHead className="w-32 text-right">操作</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {(filtered || []).map((u) => (
              <TableRow key={u.id} className={u.status === 'disabled' ? 'opacity-50' : ''}>
                <TableCell className="text-sm font-medium">{u.name}</TableCell>
                <TableCell className="text-sm text-muted-foreground">{u.username}</TableCell>
                <TableCell className="text-sm">{u.department_name || '—'}</TableCell>
                <TableCell><Badge variant={u.role === 'admin' ? 'default' : 'secondary'}>{u.role === 'admin' ? '管理员' : '员工'}</Badge></TableCell>
                <TableCell className="text-sm">
                  {u.feishu_bound ? (
                    <span className="font-mono text-xs text-muted-foreground" title={`飞书 open_id：${u.feishu_open_id}（点击复制，可用于管理员白名单）`}
                      onClick={() => { navigator.clipboard?.writeText(u.feishu_open_id); toast.success('open_id 已复制') }}
                      style={{ cursor: 'copy' }}>
                      {u.feishu_open_id.slice(0, 10)}…{u.feishu_open_id.slice(-4)}
                    </span>
                  ) : '—'}
                </TableCell>
                <TableCell className="text-sm font-mono text-xs text-muted-foreground">{u.zhishu_user_id || '—'}</TableCell>
                <TableCell className="text-sm">{u.status === 'active' ? '在用' : '已禁用'}</TableCell>
                <TableCell className="text-right">
                  <Button variant="ghost" size="icon" className="h-8 w-8" title="编辑" onClick={() => setEditing(u)}><Pencil className="h-3.5 w-3.5" /></Button>
                  {u.feishu_bound && <Button variant="ghost" size="sm" className="h-7 text-xs" onClick={() => unbind(u)}>解绑</Button>}
                  <Button variant="ghost" size="sm" className="h-7 text-xs" onClick={() => toggleStatus(u)}>{u.status === 'active' ? '禁用' : '启用'}</Button>
                </TableCell>
              </TableRow>
            ))}
            {users && filtered.length === 0 && (
              <TableRow><TableCell colSpan={8} className="py-10 text-center text-sm text-muted-foreground">
                {users.length === 0 ? '暂无账号' : deptFilter === 'none' ? '没有未分配部门的人员' : '该部门下暂无人员'}
              </TableCell></TableRow>
            )}
          </TableBody>
        </Table>
      </div>
      <UserFormDialog target={editing} departments={depts} onClose={() => setEditing(null)} onDone={() => { setEditing(null); load() }} />
    </div>
  )
}

function UserFormDialog({ target, departments, onClose, onDone }) {
  const isNew = target === 'new'
  const [form, setForm] = useState(EMPTY_USER_FORM)
  const [saving, setSaving] = useState(false)
  useEffect(() => {
    if (isNew) setForm(EMPTY_USER_FORM)
    else if (target) setForm({ name: target.name, role: target.role, department_id: target.department_id ? String(target.department_id) : 'none', zhishu_user_id: target.zhishu_user_id || '', password: '' })
  }, [target, isNew])

  const save = async (e) => {
    e.preventDefault()
    setSaving(true)
    try {
      if (isNew) {
        await api('/admin/users', {
          method: 'POST',
          body: {
            username: form.username, name: form.name, role: form.role,
            department_id: form.department_id === 'none' ? null : Number(form.department_id),
            ...(form.password ? { password: form.password } : {}),
          },
        })
        toast.success(form.password ? '已创建，可用账号密码登录（飞书扫码将自动绑定）' : '已创建，可告知用户名登录（飞书扫码将自动绑定）')
      } else {
        await api(`/admin/users/${target.id}`, {
          method: 'PATCH',
          body: {
            name: form.name, role: form.role, set_department: true,
            department_id: form.department_id === 'none' ? null : Number(form.department_id),
            zhishu_user_id: form.zhishu_user_id.trim() || null, set_zhishu_user_id: true,
            ...(form.password ? { password: form.password, set_password: true } : {}),
          },
        })
        toast.success('已保存')
      }
      onDone()
    } catch (err) {
      toast.error(err.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <Dialog open={!!target} onOpenChange={(v) => { if (!v) onClose() }}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader><DialogTitle>{isNew ? '新建账号' : `编辑「${target?.name}」`}</DialogTitle></DialogHeader>
        <form onSubmit={save} className="space-y-3">
          {isNew && (
            <div className="space-y-1.5">
              <Label>用户名（登录用，唯一）</Label>
              <Input value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} required maxLength={64} placeholder="如：zhangsan" />
            </div>
          )}
          <div className="space-y-1.5">
            <Label>姓名</Label>
            <Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required maxLength={128} placeholder="如：张三" />
            <p className="text-xs text-muted-foreground">需与飞书姓名一致，否则该员工扫码登录时无法自动认领此账号。</p>
          </div>
          <div className="space-y-1.5">
            <Label>部门</Label>
            <DepartmentSelect value={form.department_id} onValueChange={(v) => setForm({ ...form, department_id: v })} allowEmpty departments={departments} />
          </div>
          <div className="space-y-1.5">
            <Label>角色</Label>
            <Select value={form.role} onValueChange={(v) => setForm({ ...form, role: v })}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="member">员工</SelectItem>
                <SelectItem value="admin">管理员</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label>{isNew ? '登录密码（选填）' : '重置密码（留空不变）'}</Label>
            <Input type="password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} maxLength={128}
              autoComplete="new-password" placeholder="至少 6 位；设置后可用账号密码登录" />
          </div>
          {!isNew && (
            <div className="space-y-1.5">
              <Label>智书 user_id</Label>
              <Input value={form.zhishu_user_id} onChange={(e) => setForm({ ...form, zhishu_user_id: e.target.value })} maxLength={64} placeholder="如：944f4998" />
              <p className="text-xs text-muted-foreground">即飞书「用户ID」，登录时自动获取、通常无需手填；未获取到（如非飞书登录）时才手动补，留空则不读取该用户的智书合同。</p>
            </div>
          )}
          <DialogFooter>
            <Button variant="outline" type="button" onClick={onClose}>取消</Button>
            <Button type="submit" disabled={saving}>保存</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

// ---------- 权限矩阵（原「角色权限」页内容） ----------

function MatrixTab() {
  const [data, setData] = useState(null)

  const load = useCallback(() => {
    api('/admin/roles/modules').then(setData).catch((e) => toast.error(e.message))
  }, [])
  useEffect(load, [load])

  const toggle = async (role, module) => {
    const cur = new Set(data.matrix[role] || [])
    if (cur.has(module)) cur.delete(module)
    else cur.add(module)
    const modules = [...cur]
    // 乐观更新
    setData({ ...data, matrix: { ...data.matrix, [role]: modules } })
    try {
      const r = await api('/admin/roles/modules', { method: 'PUT', body: { role, modules } })
      if (!r.ok) {
        toast.error(r.detail)
        load()
      }
    } catch (e) {
      toast.error(e.message)
      load()
    }
  }

  if (!data) return null

  return (
    <div className="mt-4">
      <p className="mb-3 text-xs text-muted-foreground">勾选角色可见的功能模块。管理员必须保留模型网关权限（防止锁死）。</p>
      <div className="rounded-2xl border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>模块</TableHead>
              {data.roles.map((r) => <TableHead key={r.key} className="w-32 text-center">{r.name}</TableHead>)}
            </TableRow>
          </TableHeader>
          <TableBody>
            {data.modules.map((m) => (
              <TableRow key={m.key}>
                <TableCell className="text-sm font-medium">{m.name}</TableCell>
                {data.roles.map((r) => (
                  <TableCell key={r.key} className="text-center">
                    <Checkbox
                      checked={(data.matrix[r.key] || []).includes(m.key)}
                      onCheckedChange={() => toggle(r.key, m.key)}
                    />
                  </TableCell>
                ))}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </div>
  )
}
