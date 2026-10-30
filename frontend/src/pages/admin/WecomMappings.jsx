import { Save, Search } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { toast } from 'sonner'
import { api } from '@/api'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'

// M31.4 手工名称映射：wm/wr 开头的外部人员/外部群企微不开放接口（无姓名/群名 API），
// 管理员认出后命名一次——别名落 wecom_contacts/wecom_rooms，消息列表/筛选/导出自动生效。
// 两块表都自管理：群改名后仍在列表（可再改）；人命名后从「待认领」消失（不再是 unknown）。

function NameRow({ id, sub, initial, saving, onChange, onSave, value }) {
  return (
    <TableRow>
      <TableCell className="max-w-0">
        <div className="truncate font-mono text-xs" title={id}>{id}</div>
        {sub && <div className="text-xs text-muted-foreground">{sub}</div>}
      </TableCell>
      <TableCell>
        <Input
          className="h-8 w-52"
          placeholder={initial ? '' : '输入名字'}
          value={value}
          onChange={(e) => onChange(e.target.value)}
        />
      </TableCell>
      <TableCell className="w-24 text-right">
        <Button variant="outline" size="sm" onClick={onSave} disabled={saving}>
          <Save className="mr-1 h-3.5 w-3.5" /> {saving ? '保存中…' : '保存'}
        </Button>
      </TableCell>
    </TableRow>
  )
}

export default function WecomMappings() {
  const [rooms, setRooms] = useState(null)
  const [unknowns, setUnknowns] = useState(null)
  const [edits, setEdits] = useState({}) // 'r:'+roomid / 'c:'+userid -> 输入中名字
  const [saving, setSaving] = useState('')
  const [roomQuery, setRoomQuery] = useState('') // M38：群搜索（纯前端过滤，rooms 全量就几十~几百行）

  // roomid 片段 + 群名模糊双匹配（同事可能只记得其中一个）
  const rq = roomQuery.trim().toLowerCase()
  const filteredRooms = rq
    ? (rooms || []).filter((r) => r.roomid.toLowerCase().includes(rq) || (r.roomname || '').toLowerCase().includes(rq))
    : rooms

  const load = useCallback(() => {
    api('/admin/wecom/messages/meta')
      .then((d) => {
        setRooms(d.rooms)
        setEdits((prev) => {
          const next = { ...prev }
          for (const r of d.rooms) {
            const k = `r:${r.roomid}`
            if (!(k in next)) next[k] = r.roomname || ''
          }
          return next
        })
      })
      .catch((e) => toast.error(e.message))
    api('/admin/wecom/contacts/unknown')
      .then((d) => setUnknowns(d.items))
      .catch((e) => toast.error(e.message))
  }, [])
  useEffect(load, [load])

  const save = async (kind, id) => {
    const key = `${kind}:${id}`
    const name = (edits[key] || '').trim()
    if (!name) {
      toast.error('名字不能为空')
      return
    }
    setSaving(key)
    try {
      const path = kind === 'r' ? 'rooms' : 'contacts'
      await api(`/admin/wecom/${path}/${encodeURIComponent(id)}`, { method: 'PUT', body: { name } })
      toast.success(`已命名「${name}」`)
      load()
    } catch (e) {
      toast.error(e.message)
    } finally {
      setSaving('')
    }
  }

  return (
    <div className="space-y-8">
      <p className="text-xs text-muted-foreground">
        内部同事和内部群自动同步名字；wm / wr 开头的是外部人员 / 外部群，企业微信不开放查询接口——认出后在这里命名一次，之后消息列表、筛选和导出都会显示名字。
      </p>

      {/* 群名映射 */}
      <div>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-base font-semibold">群名映射（{rooms?.length ?? '…'}）</h2>
          <div className="relative">
            <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={roomQuery}
              onChange={(e) => setRoomQuery(e.target.value)}
              placeholder="搜索群名 / roomid"
              className="h-8 w-48 pl-8 text-sm"
            />
          </div>
        </div>
        <div className="mt-3 rounded-2xl border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>群 roomid</TableHead>
                <TableHead>群名</TableHead>
                <TableHead className="w-24 text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {filteredRooms?.map((r) => (
                <NameRow
                  key={r.roomid}
                  id={r.roomid}
                  sub={r.roomname ? `当前：${r.roomname}` : '未命名'}
                  initial={r.roomname}
                  value={edits[`r:${r.roomid}`] ?? ''}
                  saving={saving === `r:${r.roomid}`}
                  onChange={(v) => setEdits({ ...edits, [`r:${r.roomid}`]: v })}
                  onSave={() => save('r', r.roomid)}
                />
              ))}
              {rooms?.length === 0 && (
                <TableRow>
                  <TableCell colSpan={3} className="py-10 text-center text-sm text-muted-foreground">暂无群聊记录</TableCell>
                </TableRow>
              )}
              {rooms?.length > 0 && filteredRooms?.length === 0 && (
                <TableRow>
                  <TableCell colSpan={3} className="py-10 text-center text-sm text-muted-foreground">没有匹配「{roomQuery.trim()}」的群</TableCell>
                </TableRow>
              )}
              {!rooms && (
                <TableRow>
                  <TableCell colSpan={3} className="py-10 text-center text-sm text-muted-foreground">加载中…</TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </div>
      </div>

      {/* 外部人员别名 */}
      <div>
        <h2 className="text-base font-semibold">待认领的发送人（{unknowns?.length ?? '…'}）</h2>
        <div className="mt-3 rounded-2xl border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>发送人 userid</TableHead>
                <TableHead>别名</TableHead>
                <TableHead className="w-24 text-right">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {unknowns?.map((u) => (
                <NameRow
                  key={u.userid}
                  id={u.userid}
                  sub={`${u.messages} 条消息`}
                  value={edits[`c:${u.userid}`] ?? ''}
                  saving={saving === `c:${u.userid}`}
                  onChange={(v) => setEdits({ ...edits, [`c:${u.userid}`]: v })}
                  onSave={() => save('c', u.userid)}
                />
              ))}
              {unknowns?.length === 0 && (
                <TableRow>
                  <TableCell colSpan={3} className="py-10 text-center text-sm text-muted-foreground">
                    所有发送人都已有名字映射
                  </TableCell>
                </TableRow>
              )}
              {!unknowns && (
                <TableRow>
                  <TableCell colSpan={3} className="py-10 text-center text-sm text-muted-foreground">加载中…</TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </div>
      </div>
    </div>
  )
}
