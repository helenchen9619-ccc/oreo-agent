// 消息气泡公共组件（AGENT-OPS 从 ChatPage 抽取）：对话页与运行中心只读视图共用。
// readOnly=true 时附件图片渲染为占位（管理端无用户附件端点权限，一期不做图片代理）。
import { Bot, FileText, GitFork, Image as ImageIcon, Loader2, Wrench } from 'lucide-react'
import { memo, useEffect, useState } from 'react'
import { getToken } from '@/api'
import Markdown from '@/components/Markdown'
import { Avatar, AvatarFallback } from '@/components/ui/avatar'

export const TOOL_NAMES = {
  memory_search: '检索记忆',
  memory_write: '写入记忆',
  memory_update_curated: '更新长期记忆',
  read_skill: '读取技能',
  workspace_list: '列出工作区文件',
  workspace_read: '读取工作区文件',
  workspace_write: '写入工作区文件',
  workspace_delete: '删除工作区文件/文件夹',
  run_code: '执行代码',
  web_search: '联网搜索',
  web_fetch: '读取网页',
  image_generate: '生成图片',
  list_all_agents: '查看全部数字人',
  read_agent_file: '查看数字人文件',
  get_agent_skills: '查看技能绑定',
  usage_summary: '用量统计',
  list_users: '查看用户清单',
}

// ---------- 消息时间戳 ----------
export function fmtTime(iso) {
  if (!iso) return null
  const d = new Date(iso)
  const today = new Date()
  const sameDay = d.toDateString() === today.toDateString()
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  return sameDay ? hm : `${d.getMonth() + 1}-${String(d.getDate()).padStart(2, '0')} ${hm}`
}

// ---------- 消息附件图片（带鉴权 fetch → blob URL） ----------
// M20：附件带 ws_path 且父级给了 onOpen 时，点击改为工作区同款预览（不再新标签开 blob）
function AttachmentImage({ messageId, att, onOpen }) {
  const [url, setUrl] = useState(null)
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    let objUrl = null
    let cancelled = false
    fetch(`/api/chat/messages/${messageId}/attachments/${att.idx}`, {
      headers: { Authorization: `Bearer ${getToken()}` },
    })
      .then((r) => (r.ok ? r.blob() : Promise.reject(new Error(String(r.status)))))
      .then((b) => {
        if (cancelled) return
        objUrl = URL.createObjectURL(b)
        setUrl(objUrl)
      })
      .catch(() => !cancelled && setFailed(true)) // 如工作区文件已删（404）→ 降级占位
    return () => {
      cancelled = true
      if (objUrl) URL.revokeObjectURL(objUrl)
    }
  }, [messageId, att.idx])
  if (failed) return <AttachmentPlaceholder att={att} />
  if (!url) return <span className="inline-block h-16 w-16 animate-pulse rounded-md bg-muted" />
  return (
    <a
      href={url}
      target="_blank"
      rel="noreferrer"
      onClick={onOpen ? (e) => { e.preventDefault(); onOpen(att.ws_path) } : undefined}
    >
      <img src={url} alt={att.name} className="max-h-40 max-w-full rounded-md border object-contain" />
    </a>
  )
}

function AttachmentPlaceholder({ att }) {
  return (
    <span className="inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs text-muted-foreground">
      <ImageIcon className="h-3 w-3" />📷 {att.name}
    </span>
  )
}

// M20：非图片产物（docx/xlsx/md 等）的文件 chip；可定位到工作区时可点击预览，否则纯展示
function AttachmentFileChip({ att, onOpen }) {
  const cls = 'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs'
  if (!onOpen) {
    return (
      <span className={`${cls} text-muted-foreground`}>
        <FileText className="h-3 w-3" />{att.name}
      </span>
    )
  }
  return (
    <button
      title={`预览 ${att.ws_path}`}
      onClick={() => onOpen(att.ws_path)}
      className={`${cls} cursor-pointer text-muted-foreground transition-colors hover:bg-accent hover:text-foreground`}
    >
      <FileText className="h-3 w-3" />{att.name}
    </button>
  )
}

// memo：流式期间每条消息对象引用不变（只有最后一条被替换），历史消息不重复渲染
// onFork（M6a，可选）：用户消息 hover 显示「从此处分叉」按钮；运行中心只读视图不传则不渲染
// onOpenWorkspaceFile（M20，可选）：附件带 ws_path 时点击打开工作区同款预览；不传则回退旧行为
const MessageItem = memo(function MessageItem({ m, readOnly = false, onFork, onOpenWorkspaceFile }) {
  // 附件统一渲染：图片内嵌缩略图，非图片文件 chip；ws_path + 回调 → 点击进工作区预览
  const renderAtt = (att) => {
    if (readOnly) return <AttachmentPlaceholder key={att.idx} att={att} />
    const onOpen = att.ws_path && onOpenWorkspaceFile ? onOpenWorkspaceFile : null
    if ((att.mime || '').startsWith('image/')) {
      return <AttachmentImage key={att.idx} messageId={m.id} att={att} onOpen={onOpen} />
    }
    return <AttachmentFileChip key={att.idx} att={att} onOpen={onOpen} />
  }
  if (m.role === 'tool') {
    return (
      <div className="flex items-start gap-2 text-xs text-muted-foreground">
        <Wrench className="mt-0.5 h-3.5 w-3.5" />
        <div className="min-w-0">
          <span className="font-medium">{TOOL_NAMES[m.name] || m.name}</span>
          <pre className="mt-1 max-h-32 overflow-auto whitespace-pre-wrap break-all rounded bg-muted p-2">{m.content?.slice(0, 2000)}</pre>
        </div>
      </div>
    )
  }
  if (m.role === 'user') {
    return (
      <div className="group flex items-start justify-end gap-1">
        {onFork && typeof m.id === 'number' && (
          <button
            className="mt-2 shrink-0 rounded p-1 text-muted-foreground opacity-100 transition-opacity hover:bg-accent hover:text-foreground md:opacity-0 md:group-hover:opacity-100"
            title="从此处分叉为新会话（原会话不受影响）"
            onClick={() => onFork(m)}
          >
            <GitFork className="h-3.5 w-3.5" />
          </button>
        )}
        <div className="max-w-[80%] space-y-1.5">
          {m.refChips?.length > 0 && (
            <div className="flex flex-wrap justify-end gap-1">
              {m.refChips.map((r) => (
                <span key={r} className="inline-flex items-center gap-1 rounded-full border border-blue-200 bg-blue-50 px-2 py-0.5 text-xs text-blue-700 dark:border-blue-900 dark:bg-blue-950 dark:text-blue-300">
                  <FileText className="h-3 w-3" />{r}
                </span>
              ))}
            </div>
          )}
          {m.localImages?.length > 0 && (
            <div className="flex flex-wrap justify-end gap-1.5">
              {m.localImages.map((im, i) => <img key={i} src={im.dataUrl} alt={im.name} className="max-h-40 max-w-full rounded-md border object-contain" />)}
            </div>
          )}
          {m.attachments?.length > 0 && (
            <div className="flex flex-wrap justify-end gap-1.5">
              {m.attachments.map(renderAtt)}
            </div>
          )}
          {m.content && (
            <div className="whitespace-pre-wrap break-words rounded-2xl rounded-br-sm bg-primary px-4 py-2.5 text-sm text-primary-foreground">
              {m.content}
            </div>
          )}
          {fmtTime(m.created_at) && <div className="text-right text-[10px] text-muted-foreground">{fmtTime(m.created_at)}</div>}
        </div>
      </div>
    )
  }
  return (
    <div className="flex gap-3">
      <Avatar className="mt-0.5 h-7 w-7">
        <AvatarFallback><Bot className="h-3.5 w-3.5" /></AvatarFallback>
      </Avatar>
      <div className="min-w-0 flex-1 space-y-2">
        {m.tool_calls?.map((tc, i) => (
          <div key={i} className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <Wrench className="h-3.5 w-3.5" /> 调用 {TOOL_NAMES[tc.function?.name] || tc.function?.name}
          </div>
        ))}
        {m.tools?.map((t, i) => (
          <div key={i} className="flex items-center gap-1.5 text-xs text-muted-foreground">
            {t.status === 'start' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Wrench className="h-3.5 w-3.5" />}
            {TOOL_NAMES[t.name] || t.name}
            {t.status === 'done' && ' · 完成'}
          </div>
        ))}
        {m.content && <Markdown>{m.content}</Markdown>}
        {/* M13a/M20：assistant 消息附件（本轮产物），图片内嵌、文件 chip，可点进工作区预览 */}
        {m.attachments?.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {m.attachments.map(renderAtt)}
          </div>
        )}
        {fmtTime(m.created_at) && <div className="text-[10px] text-muted-foreground">{fmtTime(m.created_at)}</div>}
      </div>
    </div>
  )
})

export default MessageItem
