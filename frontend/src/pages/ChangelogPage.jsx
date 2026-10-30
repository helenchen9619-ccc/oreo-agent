import { Loader2, Megaphone, Pencil, Plus, Trash2 } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import { api, getUser } from '@/api'
import Markdown from '@/components/Markdown'
import { confirmDialog } from '@/components/dialogs'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'

const EMPTY_FORM = { title: '', pub_date: '', content: '' }

// 更新公告（M12）：全员只读；管理员可在本页直接发布/编辑/删除（不再发版改代码）
export default function ChangelogPage() {
  const isAdmin = (getUser() || {}).role === 'admin'
  const [items, setItems] = useState(null)
  const [dialogOpen, setDialogOpen] = useState(false)
  const [editing, setEditing] = useState(null) // null=新建
  const [form, setForm] = useState(EMPTY_FORM)
  const [saving, setSaving] = useState(false)
  // M30：公告目录——activeId = 当前阅读位置（IntersectionObserver 联动），scrollRef = 页面滚动容器
  const [activeId, setActiveId] = useState(null)
  const scrollRef = useRef(null)
  // 少于 2 篇不渲染目录；此时内容区必须跨满两列，否则落进 220px 目录列被压成竖排
  const showToc = (items?.length ?? 0) >= 2

  const scrollToAnn = (id) => {
    setActiveId(id)
    document.getElementById(`ann-${id}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  // 阅读位置联动：视口上部 20%~30% 区域内的卡片视为「当前在读」
  useEffect(() => {
    if (!items || items.length < 2) return
    const root = scrollRef.current
    if (!root) return
    const obs = new IntersectionObserver(
      (entries) => {
        for (const e of entries) if (e.isIntersecting) setActiveId(Number(e.target.dataset.annId))
      },
      { root, rootMargin: '-20% 0px -70% 0px' },
    )
    root.querySelectorAll('[data-ann-id]').forEach((el) => obs.observe(el))
    return () => obs.disconnect()
  }, [items])

  const load = useCallback(() => {
    api('/announcements').then(setItems).catch((e) => toast.error(e.message))
  }, [])
  useEffect(load, [load])

  // M27：进入本页即视为已读（游标推到最新），并通知侧栏立即摘掉 NEW 标签
  useEffect(() => {
    if (!items) return
    api('/announcements/read', { method: 'PUT' })
      .catch(() => {})
      .finally(() => window.dispatchEvent(new Event('ea-announcement-read')))
  }, [items])

  const openCreate = () => {
    setEditing(null)
    setForm({ ...EMPTY_FORM, pub_date: new Date().toISOString().slice(0, 10) })
    setDialogOpen(true)
  }

  const openEdit = (a) => {
    setEditing(a)
    setForm({ title: a.title, pub_date: a.pub_date, content: a.content })
    setDialogOpen(true)
  }

  const save = async () => {
    if (!form.title.trim() || !form.pub_date || !form.content.trim()) {
      toast.error('请填完整：标题、日期、内容')
      return
    }
    setSaving(true)
    try {
      const body = { title: form.title.trim(), pub_date: form.pub_date, content: form.content }
      if (editing) await api(`/announcements/${editing.id}`, { method: 'PATCH', body })
      else await api('/announcements', { method: 'POST', body })
      toast.success(editing ? '已保存' : '已发布')
      setDialogOpen(false)
      load()
    } catch (e) {
      toast.error(e.message)
    } finally {
      setSaving(false)
    }
  }

  const remove = async (a) => {
    if (!(await confirmDialog({ title: '删除公告', desc: `确定删除公告「${a.title}」？删除后全员不可见。`, confirmText: '删除', danger: true }))) return
    try {
      await api(`/announcements/${a.id}`, { method: 'DELETE' })
      toast.success('已删除')
      load()
    } catch (e) {
      toast.error(e.message)
    }
  }

  return (
    <div ref={scrollRef} className="h-full overflow-y-auto p-4 md:p-8">
      <div className="mx-auto max-w-6xl">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
              <Megaphone className="h-6 w-6" /> 更新公告
            </h1>
          </div>
          {isAdmin && (
            <Button onClick={openCreate} className="shrink-0">
              <Plus className="mr-1.5 h-4 w-4" /> 发布公告
            </Button>
          )}
        </div>

        <div className="mt-6 md:grid md:grid-cols-[220px_minmax(0,1fr)] md:gap-8">
          {/* M30 桌面目录：sticky 驻留 + 阅读位置高亮联动；少于 2 篇不渲染 */}
          {showToc && (
            <aside className="hidden md:block">
              <div className="sticky top-0 max-h-[calc(100dvh-8rem)] overflow-y-auto pb-4">
                <div className="px-3 pb-2 text-xs font-medium text-muted-foreground">公告目录 · 共 {items.length} 篇</div>
                <nav className="space-y-0.5">
                  {items.map((a) => (
                    <button
                      key={a.id}
                      onClick={() => scrollToAnn(a.id)}
                      className={`flex w-full items-start gap-2.5 rounded-lg px-3 py-2 text-left transition-colors hover:bg-accent ${activeId === a.id ? 'bg-accent' : ''}`}
                    >
                      <span className={`mt-1.5 h-4 w-0.5 shrink-0 rounded-full ${activeId === a.id ? 'bg-primary' : 'bg-transparent'}`} />
                      <span className="min-w-0">
                        <span className="block text-xs text-muted-foreground">{a.pub_date}</span>
                        <span className={`mt-0.5 block text-sm leading-5 line-clamp-2 ${activeId === a.id ? 'font-medium text-foreground' : 'text-muted-foreground'}`}>
                          {a.title}
                        </span>
                      </span>
                    </button>
                  ))}
                </nav>
              </div>
            </aside>
          )}

          <div className={`min-w-0${showToc ? '' : ' md:col-span-2'}`}>
            {/* M30 移动端目录：收进下拉（纯导航控件，选完即跳转、不留选中态） */}
            {showToc && (
              <div className="mb-4 md:hidden">
                <Select value="" onValueChange={(v) => v && scrollToAnn(Number(v))}>
                  <SelectTrigger className="w-full min-w-0">
                    <SelectValue placeholder={`公告目录（共 ${items.length} 篇）`} />
                  </SelectTrigger>
                  <SelectContent>
                    {items.map((a) => (
                      <SelectItem key={a.id} value={String(a.id)}>
                        {a.pub_date}　{a.title}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            )}

            <div className="space-y-4">
              {items === null && (
                <div className="flex justify-center py-16 text-muted-foreground"><Loader2 className="h-5 w-5 animate-spin" /></div>
              )}
              {items?.map((a) => (
                <div key={a.id} id={`ann-${a.id}`} data-ann-id={a.id} className="scroll-mt-6 rounded-xl border bg-card p-5">
                  <div className="flex items-start gap-2">
                    <Badge variant="outline" className="mt-0.5 shrink-0">{a.pub_date}</Badge>
                    <h2 className="min-w-0 flex-1 text-base font-semibold leading-6">{a.title}</h2>
                    {isAdmin && (
                      <div className="flex shrink-0 items-center">
                        <Button variant="ghost" size="icon" className="h-7 w-7" title="编辑" onClick={() => openEdit(a)}>
                          <Pencil className="h-3.5 w-3.5" />
                        </Button>
                        <Button variant="ghost" size="icon" className="h-7 w-7 text-destructive" title="删除" onClick={() => remove(a)}>
                          <Trash2 className="h-3.5 w-3.5" />
                        </Button>
                      </div>
                    )}
                  </div>
                  <div className="mt-3">
                    <Markdown>{a.content}</Markdown>
                  </div>
                </div>
              ))}
              {items?.length === 0 && (
                <div className="rounded-lg border border-dashed py-12 text-center text-sm text-muted-foreground">暂无公告</div>
              )}
            </div>
          </div>
        </div>
      </div>

      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent className="sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>{editing ? '编辑公告' : '发布公告'}</DialogTitle>
            <DialogDescription>内容支持 markdown（## 小标题、**加粗**、- 列表）；发布后全员在「更新公告」页可见。</DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <div className="flex flex-col gap-3 sm:flex-row">
              <div className="flex-1 space-y-1.5">
                <Label>标题</Label>
                <Input value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} placeholder="如：飞书生态全面打通" />
              </div>
              <div className="space-y-1.5 sm:w-40">
                <Label>日期</Label>
                <Input type="date" value={form.pub_date} onChange={(e) => setForm({ ...form, pub_date: e.target.value })} />
              </div>
            </div>
            <div className="space-y-1.5">
              <Label>内容</Label>
              <Textarea
                value={form.content}
                onChange={(e) => setForm({ ...form, content: e.target.value })}
                rows={12}
                placeholder={'先说一句话总结，再分小节写每块能力：\n\n## 能力一\n……\n\n## 能力二\n……'}
                className="font-mono text-xs"
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDialogOpen(false)}>取消</Button>
            <Button onClick={save} disabled={saving}>
              {saving && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />}
              {editing ? '保存' : '发布'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
