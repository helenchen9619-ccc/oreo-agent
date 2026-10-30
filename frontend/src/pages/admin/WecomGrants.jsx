import { Plus, Trash2 } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { toast } from 'sonner'
import { api } from '@/api'
import { confirmDialog } from '@/components/dialogs'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'

// M31.1 企微存档「授权」tab：授权查看人（能看消息进不了管理区）+ 告警接收人（空名单回落全体 admin）
function GrantSection({ title, desc, list, candidates, uid, setUid, onAdd, onRemove, addText, emptyText }) {
  return (
    <div>
      <div className="flex items-center justify-between gap-3">
        <div>
          <h3 className="text-base font-semibold">{title}（{list?.length ?? '…'}）</h3>
          <p className="mt-0.5 text-xs text-muted-foreground">{desc}</p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Select value={uid} onValueChange={setUid}>
            <SelectTrigger className="w-56 min-w-0">
              <SelectValue placeholder="选择用户" />
            </SelectTrigger>
            <SelectContent>
              {candidates.map((u) => (
                <SelectItem key={u.id} value={String(u.id)}>
                  {u.name}（{u.username}）
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button onClick={onAdd} disabled={!uid}>
            <Plus className="mr-1.5 h-4 w-4" /> {addText}
          </Button>
        </div>
      </div>
      <div className="mt-3 rounded-2xl border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>姓名</TableHead>
              <TableHead>用户名</TableHead>
              <TableHead className="w-48">添加时间</TableHead>
              <TableHead className="w-20 text-right">操作</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {list?.map((g) => (
              <TableRow key={g.id}>
                <TableCell className="text-sm">{g.user.name}</TableCell>
                <TableCell className="text-sm text-muted-foreground">{g.user.username}</TableCell>
                <TableCell className="text-sm text-muted-foreground">
                  {g.created_at ? new Date(g.created_at).toLocaleString('zh-CN', { hour12: false }) : '—'}
                </TableCell>
                <TableCell className="text-right">
                  <Button variant="ghost" size="icon" className="h-8 w-8 text-destructive" onClick={() => onRemove(g)}>
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </TableCell>
              </TableRow>
            ))}
            {list?.length === 0 && (
              <TableRow>
                <TableCell colSpan={4} className="py-8 text-center text-sm text-muted-foreground">
                  {emptyText}
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </div>
    </div>
  )
}

export default function WecomGrants() {
  const [users, setUsers] = useState([])
  const [viewers, setViewers] = useState(null)
  const [receivers, setReceivers] = useState(null)
  const [viewerUid, setViewerUid] = useState('')
  const [receiverUid, setReceiverUid] = useState('')

  const load = useCallback(() => {
    api('/auth/users').then(setUsers).catch(() => {})
    api('/admin/wecom/viewers').then(setViewers).catch((e) => toast.error(e.message))
    api('/admin/wecom/alert-receivers').then(setReceivers).catch((e) => toast.error(e.message))
  }, [])
  useEffect(load, [load])

  const grantedViewer = new Set((viewers || []).map((g) => g.user.id))
  const grantedReceiver = new Set((receivers || []).map((g) => g.user.id))
  // 接收人必须已绑定飞书（feishu_bound 由后端以布尔返回，open_id 不出后端）
  const viewerCandidates = users.filter((u) => !grantedViewer.has(u.id))
  const receiverCandidates = users.filter((u) => !grantedReceiver.has(u.id) && u.feishu_bound)

  const addViewer = async () => {
    try {
      await api('/admin/wecom/viewers', { method: 'POST', body: { user_id: Number(viewerUid) } })
      toast.success('已授权')
      setViewerUid('')
      load()
    } catch (e) {
      toast.error(e.message)
    }
  }
  const removeViewer = async (g) => {
    if (!(await confirmDialog({ title: '撤销查看授权', desc: `确定撤销 ${g.user.name} 的存档消息查看权限？`, confirmText: '撤销', danger: true }))) return
    try {
      await api(`/admin/wecom/viewers/${g.id}`, { method: 'DELETE' })
      toast.success('已撤销')
      load()
    } catch (e) {
      toast.error(e.message)
    }
  }
  const addReceiver = async () => {
    try {
      await api('/admin/wecom/alert-receivers', { method: 'POST', body: { user_id: Number(receiverUid) } })
      toast.success('已添加')
      setReceiverUid('')
      load()
    } catch (e) {
      toast.error(e.message)
    }
  }
  const removeReceiver = async (g) => {
    if (!(await confirmDialog({ title: '移除告警接收人', desc: `确定把 ${g.user.name} 移出告警接收人名单？名单为空时告警会回落给全体绑定飞书的管理员。`, confirmText: '移除', danger: true }))) return
    try {
      await api(`/admin/wecom/alert-receivers/${g.id}`, { method: 'DELETE' })
      toast.success('已移除')
      load()
    } catch (e) {
      toast.error(e.message)
    }
  }

  return (
    <div className="space-y-8">
      <GrantSection
        title="授权查看人"
        desc="被授权的非管理员可以查看存档消息（只能看消息，看不到状态/关键词/授权管理）"
        list={viewers}
        candidates={viewerCandidates}
        uid={viewerUid}
        setUid={setViewerUid}
        onAdd={addViewer}
        onRemove={removeViewer}
        addText="授权"
        emptyText="暂无授权查看人——仅管理员可看消息"
      />
      <GrantSection
        title="告警接收人"
        desc="敏感词命中的飞书告警推给名单内用户；名单为空时推给全体绑定飞书的管理员（候选人须已绑定飞书）"
        list={receivers}
        candidates={receiverCandidates}
        uid={receiverUid}
        setUid={setReceiverUid}
        onAdd={addReceiver}
        onRemove={removeReceiver}
        addText="添加"
        emptyText="名单为空——告警当前推给全体绑定飞书的管理员"
      />
    </div>
  )
}
