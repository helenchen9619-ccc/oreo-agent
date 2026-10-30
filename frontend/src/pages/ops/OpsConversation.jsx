// 运行中心 · 会话只读视图（AGENT-OPS）：复用 MessageItem 渲染，顶部固定审计横幅。
// 打开本页后端即写 admin.read_conversation 审计；附件图片在只读模式下显示占位。
import { ArrowLeft, Loader2, ShieldAlert } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { toast } from 'sonner'
import { api } from '@/api'
import MessageItem from '@/components/MessageItem'
import { Button } from '@/components/ui/button'
import { ScrollArea } from '@/components/ui/scroll-area'

export default function OpsConversation() {
  const { cid } = useParams()
  const navigate = useNavigate()
  const [data, setData] = useState(null)

  useEffect(() => {
    api(`/admin/ops/conversations/${cid}/messages`).then(setData).catch((e) => toast.error(e.message))
  }, [cid])

  return (
    <div className="flex h-full flex-col">
      {/* 固定审计横幅：让管理员明确知道查看行为被记录 */}
      <div className="flex shrink-0 items-center gap-2 border-b bg-amber-50 px-4 py-2 text-xs text-amber-800 dark:bg-amber-950/40 dark:text-amber-300">
        <ShieldAlert className="h-3.5 w-3.5 shrink-0" />
        审计模式 · 本次查看已记录到审计日志 · 只读，无法在此发送消息
      </div>
      <div className="flex shrink-0 items-center gap-3 border-b px-4 py-2.5">
        <Button variant="ghost" size="icon" className="h-8 w-8" onClick={() => navigate(-1)}>
          <ArrowLeft className="h-4 w-4" />
        </Button>
        {data ? (
          <div className="min-w-0">
            <div className="truncate text-sm font-medium">{data.conversation.title}</div>
            <div className="text-xs text-muted-foreground">
              用户 {data.conversation.user_name || data.conversation.user_id} · 渠道 {data.conversation.channel} · 会话 #{data.conversation.id}
              {data.active && <span className="ml-2 font-medium text-blue-600 dark:text-blue-400">正在运行中</span>}
            </div>
          </div>
        ) : (
          <div className="text-sm text-muted-foreground">加载中…</div>
        )}
      </div>
      <ScrollArea className="min-h-0 flex-1">
        <div className="mx-auto max-w-3xl space-y-4 p-6">
          {data ? (
            <>
              {data.messages.map((m) => <MessageItem key={m.id} m={m} readOnly />)}
              {data.messages.length === 0 && <div className="py-10 text-center text-sm text-muted-foreground">（无消息）</div>}
            </>
          ) : (
            <div className="flex justify-center py-10"><Loader2 className="h-5 w-5 animate-spin text-muted-foreground" /></div>
          )}
        </div>
      </ScrollArea>
    </div>
  )
}
