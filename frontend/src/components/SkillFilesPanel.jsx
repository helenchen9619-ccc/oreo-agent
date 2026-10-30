import { FilePlus2, FileText, Folder, FolderPlus, Save, Trash2, Upload, X } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import { api } from '@/api'
import { Button } from '@/components/ui/button'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Textarea } from '@/components/ui/textarea'
import { confirmDialog, promptDialog } from '@/components/dialogs'
import { MAX_FILE_BYTES, TEXT_EXTS } from '@/lib/importSkillFolder'
import { cn } from '@/lib/utils'

const PATH_OK = /^[A-Za-z0-9._一-龥-]+(\/[A-Za-z0-9._一-龥-]+)*$/ // 字母/数字/中文/._-，/ 分层

/**
 * 技能文件夹编辑面板（右侧滑出，非弹窗）：
 * 左侧文件树（按 / 分组缩进）+ 右侧内容编辑区；保存为全量替换语义。
 * mineBase=true 时走员工端 /skills/mine/:id/files，否则走管理端 /admin/skills/:id/files。
 */
export default function SkillFilesPanel({ skill, mineBase = false, onClose }) {
  const base = mineBase ? `/skills/mine/${skill.id}/files` : `/admin/skills/${skill.id}/files`
  const [files, setFiles] = useState(null)
  const [active, setActive] = useState('SKILL.md')
  const [saving, setSaving] = useState(false)
  const uploadRef = useRef(null)

  const load = useCallback(() => {
    api(base)
      .then((rows) => {
        setFiles(rows)
        if (!rows.find((f) => f.path === active)) setActive(rows[0]?.path || 'SKILL.md')
      })
      .catch((e) => toast.error(e.message))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [base])
  useEffect(load, [load])

  const cur = files?.find((f) => f.path === active)

  const setContent = (v) => setFiles(files.map((f) => (f.path === active ? { ...f, content: v } : f)))

  const addFile = async () => {
    const path = await promptDialog({ title: '新建文件', placeholder: '如 scripts/format.py（仅字母数字中文 . _ - /）', confirmText: '创建' })
    if (!path) return
    if (!PATH_OK.test(path)) {
      toast.error('路径格式不合法')
      return
    }
    if (files.find((f) => f.path === path)) {
      toast.error('文件已存在')
      return
    }
    setFiles([...files, { path, content: '' }])
    setActive(path)
  }

  const removeFile = async (path) => {
    if (path === 'SKILL.md') {
      toast.error('SKILL.md 是主文件，不能删除')
      return
    }
    if (!(await confirmDialog({ title: '删除文件', desc: `确定删除 ${path}？点「保存全部文件」后生效。`, confirmText: '删除', danger: true }))) return
    setFiles(files.filter((f) => f.path !== path))
    if (active === path) setActive('SKILL.md')
  }

  const addFolder = async () => {
    const name = await promptDialog({ title: '新建文件夹', placeholder: '支持层级，如 scripts/utils（仅字母数字中文 . _ - /）', confirmText: '创建' })
    if (!name) return
    if (!PATH_OK.test(name)) {
      toast.error('文件夹名称不合法')
      return
    }
    if (files.some((f) => f.path === name || f.path.startsWith(name + '/'))) {
      toast.error('文件夹已存在')
      return
    }
    // 文件夹靠 .keep 占位文件承载（树里不显示 .keep，保存后 agent 也看不到它）
    setFiles([...files, { path: `${name}/.keep`, content: '' }])
    toast.success(`已添加文件夹 ${name}（记得保存）`)
  }

  // 上传本地文件（可多选）：可选目标文件夹，读文本内容进列表，点「保存全部文件」才落库
  const onUpload = async (e) => {
    // 先复制成数组再清空 input——FileList 是活引用
    const list = Array.from(e.target.files || [])
    e.target.value = ''
    if (!list.length) return
    const folder = await promptDialog({
      title: `上传 ${list.length} 个文件`,
      desc: '上传到哪个文件夹？留空 = 根目录',
      placeholder: '如 scripts',
      confirmText: '上传',
      allowEmpty: true,
    })
    if (folder === null || folder === undefined) return
    const prefix = folder.trim().replace(/\/+$/, '')
    if (prefix && !PATH_OK.test(prefix)) {
      toast.error('文件夹名不合法')
      return
    }
    const added = []
    const skipped = []
    for (const f of list) {
      // 文件名清洗：路径只允许 字母/数字/中文/._-，其余（空格、括号等）替换为 _
      const name = f.name.replace(/[^A-Za-z0-9._一-龥-]+/g, '_')
      const ext = '.' + (name.split('.').pop() || '').toLowerCase()
      const path = prefix ? `${prefix}/${name}` : name
      if (!TEXT_EXTS.has(ext)) skipped.push(`${f.name}（非文本类型）`)
      else if (f.size > MAX_FILE_BYTES) skipped.push(`${f.name}（超 50MB）`)
      else if (!PATH_OK.test(path)) skipped.push(`${f.name}（文件名不合法）`)
      else if (files.some((x) => x.path === path) || added.some((x) => x.path === path)) skipped.push(`${f.name}（重名已存在）`)
      else added.push({ path, content: await f.text() })
    }
    if (added.length) {
      setFiles([...files, ...added])
      setActive(added[0].path)
      toast.success(`已加入 ${added.length} 个文件（记得点「保存全部文件」）${skipped.length ? `；跳过 ${skipped.length} 个` : ''}`)
    }
    if (skipped.length) toast.warning(`跳过：${skipped.join('、')}`, { duration: 6000 })
  }

  const save = async () => {
    setSaving(true)
    try {
      const rows = await api(base, { method: 'PUT', body: { files } })
      setFiles(rows)
      toast.success('技能文件已保存')
    } catch (e) {
      toast.error(e.message)
    } finally {
      setSaving(false)
    }
  }

  // 文件夹分组：顶层文件 + 每个文件夹下的文件；.keep 占位文件隐藏（仅用于承载空文件夹）
  const visibleFiles = (files ?? []).filter((f) => !f.path.endsWith('/.keep') && f.path !== '.keep')
  const topFiles = visibleFiles.filter((f) => !f.path.includes('/'))
  const folders = [...new Set(files?.filter((f) => f.path.includes('/')).map((f) => f.path.split('/')[0]) ?? [])].sort()
  const filesIn = (folder) => visibleFiles.filter((f) => f.path.startsWith(folder + '/'))

  const renderRow = (f, indent) => (
    <div
      key={f.path}
      className={cn(
        'group mb-0.5 flex cursor-pointer items-center gap-1.5 rounded-md px-2 py-1.5 text-xs hover:bg-accent/60',
        f.path === active && 'bg-accent font-medium',
      )}
      style={indent ? { paddingLeft: `${8 + indent * 14}px` } : undefined}
      onClick={() => setActive(f.path)}
    >
      <FileText className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
      <span className="min-w-0 flex-1 truncate font-mono">{f.path.split('/').pop()}</span>
      {f.path !== 'SKILL.md' && (
        <button
          className="rounded p-0.5 opacity-0 hover:bg-background group-hover:opacity-100"
          title="删除文件"
          onClick={(e) => { e.stopPropagation(); removeFile(f.path) }}
        >
          <X className="h-3 w-3" />
        </button>
      )}
    </div>
  )

  return (
    <div className="fixed inset-y-0 right-0 z-40 flex w-[560px] max-w-[92vw] flex-col border-l bg-background shadow-2xl">
      {/* 头 */}
      <div className="flex shrink-0 items-center gap-2 border-b px-4 py-3">
        <div className="min-w-0 flex-1">
          <div className="truncate font-mono text-sm font-semibold">{skill.name} · 技能文件夹</div>
          <div className="text-xs text-muted-foreground">SKILL.md 为主指令；可加 scripts/*.py 等附件；保存为全量替换（列表外的文件会被删除）</div>
        </div>
        <Button variant="ghost" size="icon" className="h-8 w-8 shrink-0" onClick={onClose}><X className="h-4 w-4" /></Button>
      </div>

      {/* 主体：左文件树 + 右编辑区 */}
      {!files ? (
        <div className="flex flex-1 items-center justify-center text-sm text-muted-foreground">加载中…</div>
      ) : (
        <div className="flex min-h-0 flex-1">
          <div className="flex w-44 shrink-0 flex-col border-r bg-muted/30">
            <ScrollArea className="min-h-0 flex-1">
              <div className="p-2">
                {topFiles.map((f) => renderRow(f, 0))}
                {folders.map((folder) => (
                  <div key={folder}>
                    <div className="mb-0.5 flex items-center gap-1.5 px-2 py-1.5 text-xs text-muted-foreground">
                      <Folder className="h-3.5 w-3.5 text-amber-500" />
                      <span className="truncate font-medium">{folder}</span>
                    </div>
                    {filesIn(folder).map((f) => renderRow(f, 1))}
                  </div>
                ))}
              </div>
            </ScrollArea>
            <div className="shrink-0 space-y-1.5 border-t p-2">
              <Button variant="outline" size="sm" className="h-7 w-full text-xs" onClick={() => uploadRef.current?.click()}>
                <Upload className="mr-1 h-3.5 w-3.5" /> 传本地文件
              </Button>
              <Button variant="outline" size="sm" className="h-7 w-full text-xs" onClick={addFile}>
                <FilePlus2 className="mr-1 h-3.5 w-3.5" /> 加文件
              </Button>
              <Button variant="outline" size="sm" className="h-7 w-full text-xs" onClick={addFolder}>
                <FolderPlus className="mr-1 h-3.5 w-3.5" /> 加文件夹
              </Button>
              <input ref={uploadRef} type="file" multiple className="hidden" onChange={onUpload} />
            </div>
          </div>
          <div className="flex min-w-0 flex-1 flex-col p-3">
            <div className="mb-2 flex items-center gap-2">
              <span className="min-w-0 flex-1 truncate font-mono text-xs text-muted-foreground" title={active}>{active}</span>
              {active !== 'SKILL.md' && (
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-6 shrink-0 px-2 text-xs text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
                  onClick={() => removeFile(active)}
                >
                  <Trash2 className="mr-1 h-3 w-3" /> 删除此文件
                </Button>
              )}
            </div>
            <Textarea
              value={cur?.content ?? ''}
              onChange={(e) => setContent(e.target.value)}
              className="min-h-0 flex-1 resize-none font-mono text-xs"
              placeholder={`# ${active}`}
            />
          </div>
        </div>
      )}

      {/* 底 */}
      <div className="flex shrink-0 justify-end gap-2 border-t px-4 py-3">
        <Button variant="outline" onClick={onClose}>关闭</Button>
        <Button onClick={save} disabled={saving || !files}>
          <Save className="mr-1.5 h-3.5 w-3.5" /> {saving ? '保存中…' : '保存全部文件'}
        </Button>
      </div>
    </div>
  )
}
