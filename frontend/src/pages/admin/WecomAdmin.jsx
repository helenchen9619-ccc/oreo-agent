import { Archive, Plus, RefreshCw, Trash2 } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { toast } from 'sonner'
import { api } from '@/api'
import { confirmDialog, promptDialog } from '@/components/dialogs'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import WecomGrants from '@/pages/admin/WecomGrants'
import WecomMappings from '@/pages/admin/WecomMappings'
import WecomMessages from '@/pages/admin/WecomMessages'

// M31 企微会话存档管理端；M31.1 tab 化：消息 / 状态 / 关键词 / 映射（M31.4 别名）/ 授权
// M31.5：授权查看人也有 Tabs（消息/状态/关键词/映射，关键词+映射可读写），仅「授权」tab 与保留策略写仍 admin 专属
// 未授权用户进来只见提示——Tabs 不渲染，管理区组件不挂载（后端 require_wecom_viewer 双保险）

function StatusSection({ status, readOnly }) {
  const cs = status?.contact_sync
  const cards = status
    ? [
        ['存档配置', status.configured ? '已配置' : '未配置', !status.configured],
        ['SDK 文件', status.sdk_present ? '已就绪' : '缺失', !status.sdk_present],
        ['通讯录 Secret', status.contact_secret ? '已配置' : '未配置', !status.contact_secret],
        ['已存档消息', `${status.message_count}`, false],
        ['通讯录映射', `${status.contact_count} 人`, false],
        ['群映射', `${status.room_count} 个`, false],
        ['拉取游标 seq', `${status.last_seq}`, false],
        ['最近入库', status.last_sync_at ? new Date(status.last_sync_at).toLocaleString('zh-CN', { hour12: false }) : '—', false],
        // M31.2：通讯录刷新结果直接可见（48009 类权限失败不再只能翻日志）
        [
          '通讯录刷新',
          cs?.at
            ? `${cs.ok ? '成功' : '失败'} · ${new Date(cs.at).toLocaleString('zh-CN', { hour12: false })}${cs.ok ? '' : ` · ${cs.error || ''}`}`
            : '尚未刷新',
          !!(cs && cs.at && !cs.ok),
        ],
      ]
    : []
  return (
    <div>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        {cards.map(([label, value, warn]) => (
          <div key={label} className="rounded-xl border bg-card p-4">
            <div className="text-xs text-muted-foreground">{label}</div>
            <div className={`mt-1 text-lg font-semibold break-all ${warn ? 'text-amber-600' : ''}`}>{value}</div>
          </div>
        ))}
        {!status && <div className="col-span-2 py-6 text-sm text-muted-foreground md:col-span-4">加载中…</div>}
      </div>
      {status && !status.configured && (
        <p className="mt-3 text-xs text-amber-600">
          未配置 WECOM_CORPID / WECOM_ARCHIVE_SECRET（云上 backend/.env.prod），同步服务处于停用状态
        </p>
      )}
      {cs && cs.at && !cs.ok && (
        <p className="mt-3 text-xs text-amber-600">
          通讯录刷新失败（10 分钟后自动重试）：若是 48009=该 Secret 对应应用无通讯录读取权限，
          需企微管理后台开权限或更换 WECOM_CONTACT_SECRET 后重启 app
        </p>
      )}
      <RetentionCard readOnly={readOnly} />
    </div>
  )
}

// M31.2：保留策略配置（0=永久；清扫每小时一次 + 重启即扫，物理删除过期消息）
// M31.5：查看人只读展示（GET 已放开），写仍 admin 专属
function RetentionCard({ readOnly }) {
  const [days, setDays] = useState(null)
  const [saving, setSaving] = useState(false)
  useEffect(() => {
    api('/admin/wecom/retention').then((d) => setDays(d.days)).catch(() => {})
  }, [])
  const save = async () => {
    setSaving(true)
    try {
      const d = await api('/admin/wecom/retention', { method: 'PUT', body: { days: Number(days) || 0 } })
      setDays(d.days)
      toast.success(d.days > 0 ? `已设置保留 ${d.days} 天，超期消息将被物理删除` : '已设置永久保留')
    } catch (e) {
      toast.error(e.message)
    } finally {
      setSaving(false)
    }
  }
  if (days === null) return null
  return (
    <div className="mt-4 rounded-xl border bg-card p-4">
      <div className="text-xs text-muted-foreground">消息保留策略</div>
      <div className="mt-2 flex items-center gap-2">
        <Input
          type="number"
          min={0}
          max={3650}
          className="w-28"
          value={days}
          disabled={readOnly}
          onChange={(e) => setDays(e.target.value)}
        />
        <span className="text-sm text-muted-foreground">天（0 = 永久保留）</span>
        {readOnly ? (
          <span className="text-xs text-muted-foreground">仅管理员可修改</span>
        ) : (
          <Button variant="outline" size="sm" onClick={save} disabled={saving}>
            {saving ? '保存中…' : '保存'}
          </Button>
        )}
      </div>
    </div>
  )
}

function KeywordsSection({ keywords, onAdd, onRemove }) {
  return (
    <div>
      <div className="flex items-center justify-between">
        <h2 className="text-base font-semibold">敏感词（{keywords?.length ?? '…'}）</h2>
        <Button onClick={onAdd}>
          <Plus className="mr-1.5 h-4 w-4" /> 添加敏感词
        </Button>
      </div>
      <div className="mt-3 rounded-2xl border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>敏感词</TableHead>
              <TableHead className="w-48">添加时间</TableHead>
              <TableHead className="w-20 text-right">操作</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {keywords?.map((k) => (
              <TableRow key={k.id}>
                <TableCell><Badge variant="outline">{k.word}</Badge></TableCell>
                <TableCell className="text-sm text-muted-foreground">
                  {k.created_at ? new Date(k.created_at).toLocaleString('zh-CN', { hour12: false }) : '—'}
                </TableCell>
                <TableCell className="text-right">
                  <Button variant="ghost" size="icon" className="h-8 w-8 text-destructive" onClick={() => onRemove(k)}>
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </TableCell>
              </TableRow>
            ))}
            {keywords?.length === 0 && (
              <TableRow>
                <TableCell colSpan={3} className="py-10 text-center text-sm text-muted-foreground">
                  暂无敏感词——添加后命中即刻告警
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </div>
    </div>
  )
}

export default function WecomAdmin() {
  const [access, setAccess] = useState(null) // {admin, viewer}；null=查询中
  const [status, setStatus] = useState(null)
  const [keywords, setKeywords] = useState(null)

  useEffect(() => {
    api('/admin/wecom/access').then(setAccess).catch((e) => {
      toast.error(e.message)
      setAccess({ admin: false, viewer: false })
    })
  }, [])

  const isAdmin = !!access?.admin
  const load = useCallback(() => {
    // 消息区是独立组件自带 state——这里的刷新只动状态卡与关键词，不重置分页
    api('/admin/wecom/status').then(setStatus).catch((e) => toast.error(e.message))
    api('/admin/wecom/keywords').then(setKeywords).catch((e) => toast.error(e.message))
  }, [])
  useEffect(() => {
    if (access?.viewer) load() // M31.5：授权查看人也拉状态/关键词（后端 require_wecom_viewer 双保险）
  }, [access, load])

  const add = async () => {
    const word = await promptDialog({ title: '添加敏感词', desc: '命中敏感词的企微消息会在分钟级内推送告警给接收人', placeholder: '如：回扣', confirmText: '添加' })
    if (!word || !word.trim()) return
    try {
      await api('/admin/wecom/keywords', { method: 'POST', body: { word: word.trim() } })
      toast.success('已添加')
      load()
    } catch (e) {
      toast.error(e.message)
    }
  }

  const remove = async (k) => {
    if (!(await confirmDialog({ title: '删除敏感词', desc: `确定删除敏感词「${k.word}」？删除后不再对它告警。`, confirmText: '删除', danger: true }))) return
    try {
      await api(`/admin/wecom/keywords/${k.id}`, { method: 'DELETE' })
      toast.success('已删除')
      load()
    } catch (e) {
      toast.error(e.message)
    }
  }

  return (
    <div className="h-full overflow-y-auto p-8">
      <div className="mx-auto max-w-6xl">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
              <Archive className="h-6 w-6" /> 企微存档
            </h1>
            <p className="mt-1 text-sm text-muted-foreground">
              企业微信会话内容每分钟同步到本地存档；命中敏感词的消息会分钟级推送告警给接收人
            </p>
          </div>
          {access?.viewer && (
            <Button variant="outline" onClick={load}>
              <RefreshCw className="mr-1.5 h-4 w-4" /> 刷新
            </Button>
          )}
        </div>

        {access === null && <div className="mt-10 text-sm text-muted-foreground">加载中…</div>}
        {access && !access.viewer && (
          <div className="mt-10 rounded-2xl border p-10 text-center text-sm text-muted-foreground">
            你未被授权查看企微存档消息，请联系管理员开通
          </div>
        )}
        {access && access.viewer && (
          <Tabs defaultValue="messages" className="mt-6">
            <TabsList>
              <TabsTrigger value="messages">消息</TabsTrigger>
              <TabsTrigger value="status">状态</TabsTrigger>
              <TabsTrigger value="keywords">关键词</TabsTrigger>
              <TabsTrigger value="mappings">映射</TabsTrigger>
              {isAdmin && <TabsTrigger value="grants">授权</TabsTrigger>}
            </TabsList>
            <TabsContent value="messages" className="mt-4">
              <WecomMessages />
            </TabsContent>
            <TabsContent value="status" className="mt-4">
              <StatusSection status={status} readOnly={!isAdmin} />
            </TabsContent>
            <TabsContent value="keywords" className="mt-4">
              <KeywordsSection keywords={keywords} onAdd={add} onRemove={remove} />
            </TabsContent>
            <TabsContent value="mappings" className="mt-4">
              <WecomMappings />
            </TabsContent>
            {isAdmin && (
              <TabsContent value="grants" className="mt-4">
                <WecomGrants />
              </TabsContent>
            )}
          </Tabs>
        )}
      </div>
    </div>
  )
}
