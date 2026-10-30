import { AtSign, Bot, CalendarClock, Check, ChevronDown, ChevronRight, Download, ExternalLink, FileText, Folder, FolderInput, FolderOpen, FolderPlus, GitFork, ImagePlus, Keyboard, Loader2, MessageSquare, Mic, MoreHorizontal, PanelLeftClose, PanelLeftOpen, Paperclip, Pencil, Pin, PinOff, Plus, Save, Search, Send, Share2, Shrink, Square, Trash2, Wrench, X, ZoomOut } from 'lucide-react'
import { Fragment, memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { toast } from 'sonner'
import { DndContext, PointerSensor, closestCenter, useSensor, useSensors } from '@dnd-kit/core'
import { SortableContext, arrayMove, useSortable, verticalListSortingStrategy } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import { api, getToken, streamChat } from '@/api'
import { Avatar, AvatarFallback } from '@/components/ui/avatar'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Progress } from '@/components/ui/progress'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import ScheduledTasksPanel from '@/components/ScheduledTasksPanel'
import Markdown from '@/components/Markdown'
import MessageItem from '@/components/MessageItem'
import PreviewPanel, { downloadRaw, fetchRaw } from '@/components/FilePreview'
import DepartmentSelect from '@/components/DepartmentSelect'
import { confirmDialog, promptDialog } from '@/components/dialogs'
import { useIsMobile } from '@/lib/useIsMobile'
import { createVoiceRecorder } from '@/lib/voiceRecorder'
import { cn } from '@/lib/utils'


const MAX_UPLOAD = 100 * 1024 * 1024 // 与后端 MAX_SIZE 一致

// 录音秒数 → "0:SS"（上限 60s，分位恒为 0）
const fmtRec = (s) => `0:${String(Math.floor(s)).padStart(2, '0')}`

// ---------- @文件名 引用匹配：文件名可含空格/括号（如 "报告 (1).html"），不能按空白切词——用文件清单做最长匹配 ----------
function findRefs(text, fileNames) {
  const hits = [] // [{start, end, name}]
  let i = 0
  while (i < text.length) {
    if (text[i] === '@') {
      const rest = text.slice(i + 1)
      const hit = fileNames.find((n) => rest.startsWith(n))
      if (hit) {
        hits.push({ start: i, end: i + 1 + hit.length, name: hit })
        i += 1 + hit.length
        continue
      }
    }
    i += 1
  }
  return hits
}

export default function ChatPage() {
  const { agentId, conversationId } = useParams()
  const navigate = useNavigate()
  const [agent, setAgent] = useState(null)
  const [models, setModels] = useState([])
  const [modelId, setModelId] = useState(null)
  const [conversations, setConversations] = useState([])
  const [messages, setMessages] = useState([])
  const [context, setContext] = useState({ tokens: 0, window: 0 })
  const [input, setInput] = useState('')
  const [wsFileNames, setWsFileNames] = useState([]) // 工作区文件名集合（用于 @引用 高亮与解析）
  const [images, setImages] = useState([])    // 待发送图片 [{name, dataUrl}]
  const [streaming, setStreaming] = useState(false)
  const [retrying, setRetrying] = useState(null) // M35：模型 429/过载自动重试中（{attempt, max, delay}）
  const [recovering, setRecovering] = useState(false) // 服务端有上一轮在跑（离开过页面），正在轮询等结果
  const [dragOver, setDragOver] = useState(false)
  // M8e：Enter 发送/换行偏好（浏览器本地，非账号级同步；enter_send=Enter 发送，enter_newline=Enter 换行）
  const [enterBehavior, setEnterBehavior] = useState(() => localStorage.getItem('ea_enter_behavior') || 'enter_send')
  const setEnterBehaviorPersist = (v) => { setEnterBehavior(v); localStorage.setItem('ea_enter_behavior', v) }
  const [convOpen, setConvOpen] = useState(true) // 对话列表可折叠（桌面）
  const isMobile = useIsMobile()
  const [convDrawerOpen, setConvDrawerOpen] = useState(false) // 移动端：会话列表抽屉
  const [wsDrawerOpen, setWsDrawerOpen] = useState(false)     // 移动端：工作区抽屉
  const [preview, setPreview] = useState(null)   // 中间预览区（工作区文件）
  const bottomRef = useRef(null)
  const forceBottomRef = useRef(false) // 发送消息/打开会话时强制滚到底（绕过"仅近底部跟随"守卫）
  const inputRef = useRef(null)
  const imageInputRef = useRef(null)
  // delta 合帧：ref 累积 + 定时器节流（deepseek 37 delta/s → 约 20 次渲染/s，与 kimi 同量级）
  const deltaBuf = useRef('')
  const deltaTimer = useRef(0)
  // 流式期间禁止历史回填覆盖本地消息（新会话 navigate 与 GET messages 竞态会把流式气泡抹掉 = 内容闪现后消失）
  const streamingRef = useRef(false)
  const streamConvRef = useRef(null)
  const pollTimer = useRef(0) // 恢复轮询定时器
  const abortRef = useRef(null) // 本地 SSE 的 AbortController（停止按钮）

  const flushDelta = useCallback(() => {
    deltaTimer.current = 0
    const d = deltaBuf.current
    if (!d) return
    deltaBuf.current = ''
    setMessages((ms) => {
      const next = [...ms]
      const last = next[next.length - 1]
      if (!last || last.role !== 'assistant') return ms
      next[next.length - 1] = { ...last, content: last.content + d }
      return next
    })
  }, [])

  useEffect(() => () => {
    if (deltaTimer.current) clearTimeout(deltaTimer.current)
  }, [])

  // ---------- 初始化 ----------
  useEffect(() => {
    api('/agents')
      .then((d) => setAgent(d.my_instances.find((a) => a.id === Number(agentId)) || null))
      .catch((e) => toast.error(e.message))
    api(`/chat/agents/${agentId}/models`)
      .then((rows) => {
        setModels(rows)
        // 1.4 记住上次选择：切走再回来不变回默认模型；已下线/无权则回落默认
        const saved = Number(localStorage.getItem(`ea_model_choice_${agentId}`))
        const def = rows.find((m) => m.id === saved) || rows.find((m) => m.is_default) || rows[0]
        if (def) setModelId(def.id)
      })
      .catch((e) => toast.error(e.message))
    loadConversations()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId])

  const loadConversations = useCallback(() => {
    api(`/chat/conversations?agent_id=${agentId}`).then(setConversations).catch((e) => toast.error(e.message))
  }, [agentId])

  // 恢复轮询：本会话有上一轮正在服务端执行（此前离开过页面/刷新过），SSE 已断，
  // 改为每 2.5s 拉一次消息，直到服务端跑完落库——回复自动出现，无需手动刷新。
  const startRecovery = useCallback(
    (cid) => {
      if (pollTimer.current) return // 已在轮询
      setRecovering(true)
      setStreaming(true) // 禁用发送，复用"处理中"语义
      const tick = async () => {
        try {
          const data = await api(`/chat/conversations/${cid}/messages`)
          if (streamingRef.current) {
            // 期间用户又在本会话发起了本地流式（理论上被禁用挡住，双保险）：以流为准，停止轮询
            pollTimer.current = 0
            setRecovering(false)
            return
          }
          setMessages(data.messages)
          setContext((c) => ({ ...c, tokens: data.context_tokens || 0 }))
          if (data.active) {
            pollTimer.current = window.setTimeout(tick, 2500)
          } else {
            // 服务端已跑完落库：上面的 setMessages 已带出最终回复
            pollTimer.current = 0
            setRecovering(false)
            setStreaming(false)
            loadConversations()
            window.dispatchEvent(new CustomEvent('workspace-changed')) // run_code 可能回写了产物
          }
        } catch (e) {
          pollTimer.current = 0
          setRecovering(false)
          setStreaming(false)
          toast.error(e.message)
        }
      }
      pollTimer.current = window.setTimeout(tick, 2500)
    },
    [loadConversations],
  )

  // 切会话/离开页面时停掉恢复轮询
  useEffect(() => {
    return () => {
      if (pollTimer.current) {
        clearTimeout(pollTimer.current)
        pollTimer.current = 0
      }
      setRecovering(false)
    }
  }, [conversationId])

  // M8e 双会话并发：切走时把正在流的旧会话「交回后台」——清本地流状态 + 断 SSE（不 cancel 后端，
  // 后端继续跑完），新会话立即可发；切回旧会话由下方 fetch effect 走恢复轮询接住结果。
  useEffect(() => {
    if (streamingRef.current && streamConvRef.current !== Number(conversationId)) {
      streamingRef.current = false
      streamConvRef.current = null
      setStreaming(false)
      setRetrying(null) // M35：切走清重试提示（重试是服务端内部态，切回由恢复轮询接结果）
      if (deltaTimer.current) { clearTimeout(deltaTimer.current); deltaTimer.current = 0 }
      deltaBuf.current = ''
      abortRef.current?.abort()
      abortRef.current = null
    }
  }, [conversationId])

  // 选中会话 → 拉历史
  useEffect(() => {
    if (!conversationId) {
      setMessages([])
      setContext({ tokens: 0, window: 0 })
      return
    }
    // 正在向本会话流式输出时，跳过回填（本地气泡就是最新状态，回填会抹掉它）
    if (streamingRef.current && Number(conversationId) === streamConvRef.current) return
    let cancelled = false
    api(`/chat/conversations/${conversationId}/messages`)
      .then((data) => {
        if (cancelled) return
        setMessages(data.messages)
        forceBottomRef.current = true // 打开会话定位到最新消息
        setContext((c) => ({ ...c, tokens: data.context_tokens || 0 }))
        // 服务端还有上一轮在跑（离开期间 SSE 已断）：进入恢复轮询，跑完自动带出回复
        if (data.active) startRecovery(Number(conversationId))
      })
      .catch((e) => {
        if (!cancelled) toast.error(e.message)
      })
    return () => {
      cancelled = true
    }
  }, [conversationId, startRecovery])

  const curModel = useMemo(() => models.find((x) => x.id === modelId), [models, modelId])
  const contextWindow = useMemo(() => {
    if (context.window) return context.window
    return curModel?.context_window || 0
  }, [context.window, curModel])

  useEffect(() => {
    // 不用 smooth：流式期间高频触发，平滑动画会不停被打断重启造成整屏抖动
    const el = bottomRef.current
    if (!el) return
    if (forceBottomRef.current) {
      // 发送消息/打开会话：无条件跳到底（用户刚发的内容必须立即可见）
      forceBottomRef.current = false
      el.scrollIntoView({ behavior: 'auto' })
      return
    }
    // 流式输出期间：仅近底部时才跟随滚动，用户上翻回看历史不被打断
    const vp = el.closest('[data-radix-scroll-area-viewport]')
    if (vp && vp.scrollHeight - vp.scrollTop - vp.clientHeight > 120) return
    el.scrollIntoView({ behavior: 'auto' })
  }, [messages])

  // 工作区文件名（@引用 高亮 + 发送时解析）；与 WorkspacePanel 共用 workspace-changed 事件
  useEffect(() => {
    const load = () =>
      api(`/agents/${agentId}/workspace`)
        .then((rows) => setWsFileNames(rows.map((f) => f.filename).filter((n) => !n.endsWith('/.keep') && n !== '.keep')))
        .catch(() => {})
    load()
    window.addEventListener('workspace-changed', load)
    return () => window.removeEventListener('workspace-changed', load)
  }, [agentId])

  // 长的文件名排前面：@a 和 @a b 都存在时必须优先匹配长的
  const fileNames = useMemo(() => [...new Set(wsFileNames)].sort((a, b) => b.length - a.length), [wsFileNames])

  // 在光标处插入 @文件名 引用文本
  const insertRef = useCallback((name) => {
    const token = `@${name} `
    const el = inputRef.current
    if (!el) {
      setInput((v) => v + token)
      return
    }
    const start = el.selectionStart ?? input.length
    const end = el.selectionEnd ?? start
    setInput((v) => v.slice(0, start) + token + v.slice(end))
    requestAnimationFrame(() => {
      el.focus()
      const pos = start + token.length
      el.setSelectionRange(pos, pos)
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [input])

  // 发送时从文本解析引用（@文件名 且真实存在于工作区；文件名可含空格/括号，用清单最长匹配）
  const parseRefs = useCallback((text) => {
    const found = []
    for (const h of findRefs(text, fileNames)) {
      if (!found.includes(h.name)) found.push(h.name)
    }
    return found
  }, [fileNames])

  // ---------- 发消息 ----------
  const newConversation = async () => {
    try {
      const c = await api('/chat/conversations', { method: 'POST', body: { agent_id: Number(agentId) } })
      await loadConversations()
      navigate(`/chat/${agentId}/${c.id}`)
    } catch (e) {
      toast.error(e.message)
    }
  }

  const removeConversation = async (c) => {
    if (!(await confirmDialog({ title: '删除对话', desc: `确定删除对话「${c.title}」？删除后不再展示。`, confirmText: '删除', danger: true }))) return
    try {
      await api(`/chat/conversations/${c.id}`, { method: 'DELETE' })
      toast.success('已删除')
      if (Number(conversationId) === c.id) navigate(`/chat/${agentId}`)
      loadConversations()
    } catch (e) {
      toast.error(e.message)
    }
  }

  const renameConversation = async (c) => {
    const title = await promptDialog({ title: '重命名对话', defaultValue: c.title, placeholder: '输入新的会话标题', confirmText: '保存' })
    if (!title || title === c.title) return
    try {
      await api(`/chat/conversations/${c.id}`, { method: 'PATCH', body: { title } })
      loadConversations()
    } catch (e) {
      toast.error(e.message)
    }
  }

  const togglePinConversation = async (c) => {
    try {
      await api(`/chat/conversations/${c.id}`, { method: 'PATCH', body: { pinned: !c.pinned } })
      loadConversations()
    } catch (e) {
      toast.error(e.message)
    }
  }

  // M6a 会话分叉：从某条用户消息复制前缀建新会话并跳转（原会话不动）
  const forkConversation = async (m) => {
    try {
      const r = await api(`/chat/conversations/${conversationId}/fork`, { method: 'POST', body: { message_id: m.id } })
      toast.success('已分叉为新会话')
      await loadConversations()
      navigate(`/chat/${agentId}/${r.id}`)
    } catch (e) {
      toast.error(e.message)
    }
  }

  // M20：消息附件点击 → 按 ws_path 在工作区列表里找到文件 id，打开工作区同款预览
  const openWorkspaceFile = async (wsPath) => {
    if (!wsPath) return
    try {
      const list = await api(`/agents/${agentId}/workspace`)
      const f = (Array.isArray(list) ? list : []).find((x) => x.filename === wsPath)
      if (!f) throw new Error('文件已不在工作区（可能已被删除）')
      const detail = await api(`/agents/${agentId}/workspace/${f.id}`)
      setPreview(detail)
    } catch (e) {
      toast.error(e.message)
    }
  }

  // 手动压缩上下文（openclaw /compact 同款）：不等 70% 阈值，用户可提前压
  const [compacting, setCompacting] = useState(false)
  const compactContext = async () => {
    if (!conversationId || compacting || streaming) return
    setCompacting(true)
    try {
      const r = await api(`/chat/conversations/${conversationId}/compact`, { method: 'POST' })
      if (r.compacted) {
        toast.success(`已压缩上下文：较早消息已摘要（${r.summary_len} 字），原文仍在库中`)
        setContext((c) => ({ ...c, tokens: r.context_tokens || 0 }))
        // 被压缩的消息本地也移除（与重新打开会话的展示一致）
        const data = await api(`/chat/conversations/${conversationId}/messages`)
        setMessages(data.messages)
      } else {
        toast.info(r.reason || '当前无需压缩')
      }
    } catch (e) {
      toast.error(e.message)
    } finally {
      setCompacting(false)
    }
  }

  const pickImages = (fileList) => {
    const files = Array.from(fileList).filter((f) => f.type.startsWith('image/'))
    if (!files.length) return
    files.forEach((f) => {
      if (f.size > 5 * 1024 * 1024) {
        toast.error(`图片「${f.name}」超过 5MB`)
        return
      }
      const reader = new FileReader()
      reader.onload = () =>
        setImages((imgs) => (imgs.length >= 4 ? (toast.error('每条消息最多附 4 张图片'), imgs) : [...imgs, { name: f.name, dataUrl: reader.result }]))
      reader.readAsDataURL(f)
    })
  }

  // 粘贴图片：剪贴板含图片文件时拦截转为消息附件；纯文本粘贴不拦截（files 为空直接放行）
  const onPasteImage = (e) => {
    const files = Array.from(e.clipboardData?.files || []).filter((f) => f.type.startsWith('image/'))
    if (!files.length) return
    e.preventDefault()
    if (!curModel?.supports_vision) {
      toast.error(`当前模型「${curModel?.display_name || ''}」不支持图片，请切换到支持视觉的模型`)
      return
    }
    // 截图粘贴的文件名通常为 image.png 或为空，统一命名便于识别
    pickImages(files.map((f) => new File([f], f.name && f.name !== 'image.png' ? f.name : `粘贴图片_${Date.now()}.png`, { type: f.type })))
  }

  const send = async () => {
    const content = input.trim()
    if ((!content && images.length === 0) || streaming) return
    if (images.length && !curModel?.supports_vision) {
      toast.error(`当前模型「${curModel?.display_name || ''}」不支持图片，请切换到支持视觉的模型`)
      return
    }
    let cid = Number(conversationId)
    try {
      if (!cid) {
        const c = await api('/chat/conversations', { method: 'POST', body: { agent_id: Number(agentId) } })
        cid = c.id
        await loadConversations()
        navigate(`/chat/${agentId}/${cid}`, { replace: true })
      }
    } catch (e) {
      toast.error(e.message)
      return
    }
    // 从正文解析 @引用 → 给数字人的读取指令前缀（正文里的 @文件名 保留原文）
    const refs = parseRefs(content)
    const finalContent = refs.length
      ? `【我引用了工作区文件：${refs.join('、')}，请用 workspace_read 读取后再回答】\n${content}`
      : content || '（请看图片）'
    const sentRefs = refs
    const sentImages = images
    setInput('')
    setImages([])
    streamingRef.current = true
    streamConvRef.current = cid
    setStreaming(true)
    forceBottomRef.current = true // 发送后强制滚到底，用户刚发的气泡立即可见
    setMessages((ms) => [
      ...ms,
      { id: `u-${Date.now()}`, role: 'user', content, refChips: sentRefs, localImages: sentImages },
      { id: `a-${Date.now()}`, role: 'assistant', content: '', tools: [] },
    ])
    const controller = new AbortController()
    abortRef.current = controller
    await streamChat(cid, {
      content: finalContent,
      model_id: modelId,
      images: sentImages.map((im) => ({ name: im.name, data_url: im.dataUrl })),
    }, {
      signal: controller.signal,
      onDelta: (d) => {
        if (streamConvRef.current !== cid) return // M8e：已切走，旧流回调 no-op（由恢复轮询接管）
        setRetrying(null) // M35：流恢复，清掉重试提示
        deltaBuf.current += d
        if (!deltaTimer.current) deltaTimer.current = window.setTimeout(flushDelta, 40)
      },
      onRetrying: (r) => {
        if (streamConvRef.current !== cid) return
        setRetrying(r) // M35：模型繁忙自动重试中
      },
      onTool: (t) => {
        if (streamConvRef.current !== cid) return
        // agent 写/删文件成功后立刻刷新工作区面板（无需手动刷新页面）
        if ((t.name === 'workspace_write' || t.name === 'workspace_delete' || t.name === 'run_code' || t.name === 'image_generate') && t.status === 'done') {
          window.dispatchEvent(new CustomEvent('workspace-changed'))
        }
        setMessages((ms) => {
          const next = [...ms]
          const last = next[next.length - 1]
          const tools = [...(last.tools || [])]
          const idx = tools.findIndex((x) => x.id === t.id && x.name === t.name)
          if (t.status === 'start') tools.push({ id: t.id, name: t.name, status: 'start' })
          else if (idx >= 0) tools[idx] = { ...tools[idx], status: 'done' }
          next[next.length - 1] = { ...last, tools }
          return next
        })
      },
      onDone: (d) => {
        if (streamConvRef.current !== cid) return
        flushDelta() // 流结束前把缓冲里剩余的 delta 落屏
        setRetrying(null)
        streamingRef.current = false
        streamConvRef.current = null
        setContext({ tokens: d.context_tokens, window: d.context_window })
        // M13a：把真实 message_id 和本轮产物附件（生成的图片）合并进最后一条 assistant 气泡
        if (d.attachments?.length) {
          setMessages((ms) => {
            const next = [...ms]
            const last = next[next.length - 1]
            if (last?.role === 'assistant') next[next.length - 1] = { ...last, id: d.message_id, attachments: d.attachments }
            return next
          })
        }
        setStreaming(false)
        loadConversations()
      },
      onAborted: () => {
        if (streamConvRef.current !== cid) return // 已切走（摘流）：断流回调 no-op，避免误改新会话
        // 用户点了停止：本地流已断，后端取消请求由 stopTurn 另行发出
        flushDelta()
        setRetrying(null)
        streamingRef.current = false
        streamConvRef.current = null
        setStreaming(false)
        // 空壳气泡（还没出任何内容就被停掉）清掉；已有部分内容的保留
        setMessages((ms) => ms.filter((m) => !(typeof m.id === 'string' && m.role === 'assistant' && !m.content && !m.tools?.length)))
      },
      onError: (e) => {
        if (streamConvRef.current !== cid) return
        flushDelta()
        setRetrying(null)
        streamingRef.current = false
        streamConvRef.current = null
        if (e.status === 409) {
          // 上一轮还在服务端跑（离开过页面），本条没发出去：退回输入框，进入恢复轮询等上一轮完成
          toast.error(e.message)
          setMessages((ms) => ms.filter((m) => typeof m.id !== 'string'))
          setInput(content)
          setImages(sentImages)
          startRecovery(cid)
          return
        }
        toast.error(e.message)
        setStreaming(false)
        setMessages((ms) => ms.filter((m) => !(typeof m.id === 'string' && m.role === 'assistant' && !m.content)))
      },
    })
    // M8e：仅当本流仍是当前流时才收尾——摘流后（已切走，streamConvRef 已指向别的/空）不得误清新会话状态
    if (streamConvRef.current === cid) {
      streamingRef.current = false
      streamConvRef.current = null
      if (!pollTimer.current) setStreaming(false) // 409 恢复轮询进行中时保持发送禁用
    }
    inputRef.current?.focus()
  }

  // ---------- 语音输入（M13b）：录音 WAV → /api/chat/asr → 文字回填输入框（可编辑再发） ----------
  const [recState, setRecState] = useState('idle') // idle | recording | transcribing
  const [recSeconds, setRecSeconds] = useState(0)
  const recorderRef = useRef(null)

  const startRecording = async () => {
    try {
      const rec = createVoiceRecorder({
        onTick: (s) => setRecSeconds(s),
        onAutoStop: () => finishRecording(), // 到 60s 上限自动完成
      })
      recorderRef.current = rec
      await rec.start()
      setRecSeconds(0)
      setRecState('recording')
    } catch {
      recorderRef.current = null
      toast.error('无法访问麦克风，请检查浏览器授权（需 HTTPS 或 localhost 访问）')
    }
  }

  // 注意：不要在内部读 recState（onAutoStop 是定时器回调，闭包里的 state 是旧的），只看 recorderRef
  const finishRecording = async () => {
    const rec = recorderRef.current
    if (!rec) return
    recorderRef.current = null
    setRecState('transcribing')
    try {
      const blob = await rec.stop()
      if (!blob) return
      const fd = new FormData()
      fd.append('file', blob, 'voice.wav')
      const { text } = await api('/chat/asr', { method: 'POST', formData: fd })
      if (text) setInput((prev) => (prev.trim() ? `${prev.trim()} ${text}` : text))
      else toast.info('没有识别到语音内容，请再试一次')
    } catch (e) {
      toast.error(e.message)
    } finally {
      setRecState('idle')
    }
  }

  const cancelRecording = async () => {
    const rec = recorderRef.current
    recorderRef.current = null
    setRecState('idle')
    await rec?.cancel()
  }

  // 停止按钮：断本地 SSE + 通知服务端取消后台任务（恢复轮询中也可停）
  const stopTurn = async () => {    const cid = streamConvRef.current ?? Number(conversationId)
    abortRef.current?.abort()
    abortRef.current = null
    if (pollTimer.current) {
      clearTimeout(pollTimer.current)
      pollTimer.current = 0
    }
    setRecovering(false)
    setStreaming(false)
    streamingRef.current = false
    streamConvRef.current = null
    if (!cid) return
    try {
      await api(`/chat/conversations/${cid}/cancel`, { method: 'POST' })
      toast.success('已停止')
    } catch (e) {
      toast.error(e.message)
    }
  }

  // ---------- 工作区上传（拖到对话区任意处） ----------
  const uploadFile = useCallback(
    async (file) => {
      if (file.size > MAX_UPLOAD) {
        toast.error(`文件「${file.name}」超过 100MB 上限`)
        return
      }
      const fd = new FormData()
      fd.append('file', file)
      try {
        await api(`/agents/${agentId}/workspace`, { method: 'POST', formData: fd })
        toast.success(`已上传到工作区：${file.name}`)
        window.dispatchEvent(new CustomEvent('workspace-changed'))
      } catch (e) {
        toast.error(e.message)
      }
    },
    [agentId],
  )

  const pct = contextWindow ? Math.min(100, Math.round((context.tokens / contextWindow) * 100)) : 0

  // 会话列表内容：桌面左栏与移动端抽屉共用（inDrawer=移动端抽屉：点会话/关闭都收起抽屉）
  const renderConvList = (inDrawer = false) => (
    <>
      <div className="flex items-center gap-1 p-3 pb-2">
        <Button className="flex-1" variant="outline" onClick={() => { newConversation(); if (inDrawer) setConvDrawerOpen(false) }}>
          <Plus className="mr-1.5 h-4 w-4" /> 新对话
        </Button>
        <Button
          variant="ghost"
          size="icon"
          className="h-9 w-9 shrink-0"
          title={inDrawer ? '关闭' : '收起对话列表'}
          onClick={() => (inDrawer ? setConvDrawerOpen(false) : setConvOpen(false))}
        >
          {inDrawer ? <X className="h-4 w-4" /> : <PanelLeftClose className="h-4 w-4" />}
        </Button>
      </div>
      {/* 原生 overflow-y-auto：Radix ScrollArea 的 display:table 内层会按未截断标题撑宽整行，把右侧删除按钮推出可视区裁掉 */}
      <div className="min-h-0 flex-1 overflow-y-auto px-2">
        {conversations.map((c) => (
          <div
            key={c.id}
            className={cn(
              'group mb-0.5 rounded-md transition-colors',
              Number(conversationId) === c.id ? 'bg-accent' : 'hover:bg-accent/60',
            )}
          >
            <div className="flex items-center">
              <button
                onClick={() => { navigate(`/chat/${agentId}/${c.id}`); if (inDrawer) setConvDrawerOpen(false) }}
                className={cn(
                  'min-w-0 flex-1 truncate px-3 py-2 text-left text-sm',
                  c.forked_from_id && 'pb-0',
                  Number(conversationId) === c.id ? 'font-medium' : 'text-muted-foreground group-hover:text-foreground',
                )}
              >
                {c.pinned && <Pin className="mr-1 inline h-3 w-3 align-[-1px] text-muted-foreground" />}
                {c.channel === 'cron' && <CalendarClock className="mr-1 inline h-3 w-3 align-[-1px] text-muted-foreground" />}
                {c.title}
              </button>
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <button
                    title="会话设置"
                    className="mr-1 shrink-0 rounded p-1 text-muted-foreground opacity-100 hover:bg-background hover:text-foreground focus:opacity-100 data-[state=open]:opacity-100 md:opacity-0 md:group-hover:opacity-100 md:focus:opacity-100 md:data-[state=open]:opacity-100"
                  >
                    <MoreHorizontal className="h-3.5 w-3.5" />
                  </button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" sideOffset={4} className="w-36">
                  <DropdownMenuItem onClick={() => renameConversation(c)}>
                    <Pencil className="mr-2 h-3.5 w-3.5" /> 重命名
                  </DropdownMenuItem>
                  <DropdownMenuItem onClick={() => togglePinConversation(c)}>
                    {c.pinned ? <PinOff className="mr-2 h-3.5 w-3.5" /> : <Pin className="mr-2 h-3.5 w-3.5" />}
                    {c.pinned ? '取消置顶' : '置顶'}
                  </DropdownMenuItem>
                  <DropdownMenuSeparator />
                  <DropdownMenuItem className="text-destructive focus:text-destructive" onClick={() => removeConversation(c)}>
                    <Trash2 className="mr-2 h-3.5 w-3.5" /> 删除
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            </div>
            {c.forked_from_id && (
              <div className="truncate px-3 pb-1.5 text-[10px] text-muted-foreground" title={c.forked_from_title ? `分叉自：${c.forked_from_title}` : '分叉自一个已删除的会话'}>
                <GitFork className="mr-0.5 inline h-2.5 w-2.5 align-[-1px]" />
                分叉自：{c.forked_from_title ?? '已删除的会话'}
              </div>
            )}
          </div>
        ))}
        {conversations.length === 0 && <div className="px-3 py-8 text-center text-xs text-muted-foreground">暂无历史会话</div>}
      </div>
    </>
  )

  if (!agent) return <div className="flex h-full items-center justify-center text-muted-foreground"><Loader2 className="h-5 w-5 animate-spin" /></div>

  return (
    <div className="flex h-full min-h-0">
      {/* 左栏：会话列表（桌面可折叠成细条；移动端改抽屉） */}
      {!isMobile &&
        (convOpen ? (
          <div className="flex w-52 shrink-0 flex-col border-r bg-card">{renderConvList()}</div>
        ) : (
          <div className="flex w-10 shrink-0 flex-col items-center gap-1 border-r bg-card py-2">
            <Button variant="ghost" size="icon" className="h-8 w-8" title="展开对话列表" onClick={() => setConvOpen(true)}>
              <PanelLeftOpen className="h-4 w-4" />
            </Button>
            <Button variant="ghost" size="icon" className="h-8 w-8" title="新对话" onClick={newConversation}>
              <Plus className="h-4 w-4" />
            </Button>
          </div>
        ))}
      {isMobile && convDrawerOpen && (
        <div className="fixed inset-0 z-50">
          <div className="absolute inset-0 bg-black/40" onClick={() => setConvDrawerOpen(false)} />
          <div className="absolute left-0 top-0 flex h-full w-72 max-w-[85vw] flex-col bg-card shadow-xl">{renderConvList(true)}</div>
        </div>
      )}

      {/* 中栏：工作区文件面板（移动端改右侧抽屉；引用后自动收抽屉回到输入框） */}
      {!isMobile && <WorkspacePanel agentId={agentId} onInsert={insertRef} preview={preview} onPreview={setPreview} />}
      {isMobile && wsDrawerOpen && (
        <div className="fixed inset-0 z-40">
          <div className="absolute inset-0 bg-black/40" onClick={() => setWsDrawerOpen(false)} />
          <div className="absolute right-0 top-0 flex h-full w-[85vw] max-w-sm flex-col bg-background shadow-xl">
            <WorkspacePanel
              agentId={agentId}
              onInsert={(name) => { insertRef(name); setWsDrawerOpen(false) }}
              preview={preview}
              onPreview={setPreview}
              mobile
            />
          </div>
        </div>
      )}

      {/* 预览区：点击工作区文件后展开（桌面=工作区与对话窗之间并排；移动端=全屏覆盖） */}
      {preview &&
        (isMobile ? (
          <div className="fixed inset-0 z-50 flex bg-background">
            <PreviewPanel readOnly={!!preview.raw_url_override} rawUrl={preview.raw_url_override || `/api/agents/${agentId}/workspace/${preview.id}/raw`} file={preview} onClose={() => setPreview(null)} onInsert={insertRef} />
          </div>
        ) : (
          <PreviewPanel readOnly={!!preview.raw_url_override} rawUrl={preview.raw_url_override || `/api/agents/${agentId}/workspace/${preview.id}/raw`} file={preview} onClose={() => setPreview(null)} onInsert={insertRef} />
        ))}

      {/* 右栏：对话区 */}
      <div
        className="relative flex min-w-0 flex-1 flex-col"
        onDragOver={(e) => { e.preventDefault(); setDragOver(true) }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault()
          setDragOver(false)
          // 从工作区面板拖来的文件 → 引用 chip；操作系统文件 → 上传（视觉模型的图片→消息附件）
          const refName = e.dataTransfer.getData('text/workspace-file')
          if (refName) {
            insertRef(refName)
            return
          }
          const files = Array.from(e.dataTransfer.files)
          const imgs = files.filter((f) => f.type.startsWith('image/'))
          const others = files.filter((f) => !f.type.startsWith('image/'))
          if (imgs.length && curModel?.supports_vision) {
            pickImages(imgs)
            others.forEach(uploadFile)
          } else {
            if (imgs.length) toast.error('当前模型不支持图片，图片已按普通文件上传到工作区')
            files.forEach(uploadFile)
          }
        }}
      >
        {dragOver && (
          <div className="pointer-events-none absolute inset-2 z-10 flex items-center justify-center rounded-lg border-2 border-dashed border-primary bg-primary/5 text-sm text-primary">
            松开以上传到工作区{curModel?.supports_vision ? '（图片将作为消息附件）' : ''}
          </div>
        )}

        {/* 顶栏（移动端：左右各加会话列表/工作区入口） */}
        <div className="flex shrink-0 items-center gap-3 border-b px-4 py-2.5 max-md:gap-2 max-md:px-3">
          {isMobile && (
            <Button variant="ghost" size="icon" className="h-8 w-8 shrink-0" title="会话列表" onClick={() => setConvDrawerOpen(true)}>
              <MessageSquare className="h-4 w-4" />
            </Button>
          )}
          <Avatar className="h-8 w-8">
            <AvatarFallback><Bot className="h-4 w-4" /></AvatarFallback>
          </Avatar>
          <div className="min-w-0 flex-1">
            <div className="truncate text-sm font-semibold">{agent.name}</div>
          </div>
          {isMobile && (
            <Button variant="ghost" size="icon" className="h-8 w-8 shrink-0" title="工作区 / 定时任务" onClick={() => setWsDrawerOpen(true)}>
              <FolderOpen className="h-4 w-4" />
            </Button>
          )}
          <Select value={modelId ? String(modelId) : undefined} onValueChange={(v) => { const id = Number(v); setModelId(id); localStorage.setItem(`ea_model_choice_${agentId}`, String(id)) }}>
            <SelectTrigger className="h-8 w-44 text-xs max-md:w-28">
              <SelectValue placeholder="选择模型" />
            </SelectTrigger>
            <SelectContent>
              {models.map((m) => (
                <SelectItem key={m.id} value={String(m.id)}>
                  {m.display_name} · {m.provider}{m.supports_vision ? ' · 视觉' : ''}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        {/* 上下文进度（移动端只留百分比，防溢出） */}
        <div className="flex shrink-0 items-center gap-2 border-b px-4 py-1.5 max-md:px-3">
          <Progress value={pct} className={cn('h-1.5 flex-1', pct > 70 && '[&>div]:bg-amber-500')} />
          <span className="whitespace-nowrap text-xs text-muted-foreground">
            {contextWindow ? (isMobile ? `${pct}%` : `上下文 ${pct}%（${context.tokens}/${contextWindow}）`) : '—'}
            {!isMobile && pct > 70 && ' · 将自动压缩'}
          </span>
          {conversationId && (
            <Button
              variant="ghost"
              size="sm"
              className="h-6 gap-1 px-2 text-xs text-muted-foreground"
              title="提前把较早的对话压缩成摘要，释放上下文空间（原消息不会删除）"
              disabled={compacting || streaming}
              onClick={compactContext}
            >
              {compacting ? <Loader2 className="h-3 w-3 animate-spin" /> : <Shrink className="h-3 w-3" />}
              压缩
            </Button>
          )}
        </div>

        {/* 消息区（min-h-0：只有这里滚，整页不滚） */}
        <ScrollArea className="min-h-0 flex-1">
          <div className="mx-auto max-w-3xl space-y-5 px-4 py-6">
            {messages.length === 0 && (
              <div className="py-20 text-center text-sm text-muted-foreground">
                {isMobile
                  ? `开始和「${agent.name}」对话吧。右上角可打开会话列表与工作区（文件的 ⋯ 菜单可引用到输入框）。`
                  : `开始和「${agent.name}」对话吧。中间面板是工作区文件，可拖到输入框引用。`}
              </div>
            )}
            {messages.map((m, i) => (
              <Fragment key={m.id}>
                {/* M24.5：压缩只影响模型上下文，历史消息始终可回看；在压缩边界插分隔条并弱化旧气泡 */}
                {i > 0 && messages[i - 1].compacted && !m.compacted && (
                  <div className="flex items-center gap-2 py-1">
                    <span className="flex-1 border-t" />
                    <span className="text-xs text-muted-foreground">以上内容已压缩为摘要，不再计入模型上下文</span>
                    <span className="flex-1 border-t" />
                  </div>
                )}
                <div className={m.compacted ? 'opacity-60' : undefined}>
                  <MessageItem m={m} onFork={forkConversation} onOpenWorkspaceFile={openWorkspaceFile} />
                </div>
              </Fragment>
            ))}
            {retrying && (
              <div className="flex items-center gap-2 text-sm text-amber-600 dark:text-amber-400">
                <Loader2 className="h-4 w-4 animate-spin" /> 模型服务繁忙，正在自动重试…（第 {retrying.attempt}/{retrying.max} 次）
              </div>
            )}
            {streaming && !retrying && messages[messages.length - 1]?.role === 'assistant' && !messages[messages.length - 1]?.content && !messages[messages.length - 1]?.tools?.length && (
              <div className="flex items-center gap-2 text-sm text-muted-foreground"><Loader2 className="h-4 w-4 animate-spin" /> 思考中…</div>
            )}
            {recovering && (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" /> 上一条消息仍在服务端处理中，完成后会自动显示回复…
              </div>
            )}
            <div ref={bottomRef} />
          </div>
        </ScrollArea>

        {/* 输入区 */}
        <div className="shrink-0 border-t p-4">
          <div className="mx-auto max-w-3xl">
            {/* 待发送图片缩略图 */}
            {images.length > 0 && (
              <div className="mb-2 flex flex-wrap items-center gap-1.5">
                {images.map((im, i) => (
                  <span key={i} className="relative inline-block">
                    <img src={im.dataUrl} alt={im.name} className="h-12 w-12 rounded-md border object-cover" />
                    <button
                      className="absolute -right-1.5 -top-1.5 rounded-full border bg-background p-0.5 shadow-sm hover:bg-accent"
                      onClick={() => setImages((x) => x.filter((_, j) => j !== i))}
                    >
                      <X className="h-3 w-3" />
                    </button>
                  </span>
                ))}
              </div>
            )}
            <div className="flex items-end gap-2">
              <Button
                variant="ghost"
                size="icon"
                className="shrink-0"
                title={curModel?.supports_vision ? '添加图片（临时附件，不进工作区）' : '当前模型不支持图片，请切换到视觉模型'}
                disabled={!curModel?.supports_vision}
                onClick={() => imageInputRef.current?.click()}
              >
                <ImagePlus className="h-4 w-4" />
              </Button>
              <input
                ref={imageInputRef}
                type="file"
                accept="image/*"
                multiple
                className="hidden"
                onChange={(e) => { pickImages(e.target.files); e.target.value = '' }}
              />
              {/* 语音输入（M13b）：idle=麦克风；recording=红色脉冲+计时+取消；transcribing=转圈 */}
              {recState === 'recording' ? (
                <>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="shrink-0 animate-pulse text-red-500"
                    title="完成录音并识别"
                    onClick={finishRecording}
                  >
                    <Mic className="h-4 w-4" />
                  </Button>
                  <Button variant="ghost" size="icon" className="shrink-0" title="取消录音" onClick={cancelRecording}>
                    <X className="h-4 w-4" />
                  </Button>
                  <span className="shrink-0 self-center text-xs tabular-nums text-red-500">{fmtRec(recSeconds)} / 1:00</span>
                </>
              ) : (
                <Button
                  variant="ghost"
                  size="icon"
                  className="shrink-0"
                  title={recState === 'transcribing' ? '识别中…' : '语音输入（最长 60 秒，识别后填入输入框）'}
                  disabled={streaming || recState === 'transcribing'}
                  onClick={startRecording}
                >
                  {recState === 'transcribing' ? <Loader2 className="h-4 w-4 animate-spin" /> : <Mic className="h-4 w-4" />}
                </Button>
              )}
              {/* M8e：回车键行为设置（Enter 发送 / Enter 换行，浏览器本地偏好） */}
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button variant="ghost" size="icon" className="shrink-0" title="回车键行为设置">
                    <Keyboard className="h-4 w-4" />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="start" className="w-64">
                  <DropdownMenuItem onClick={() => setEnterBehaviorPersist('enter_send')}>
                    <span className={enterBehavior === 'enter_send' ? 'font-medium' : ''}>Enter 发送 · Shift+Enter 换行</span>
                    {enterBehavior === 'enter_send' && <Check className="ml-auto h-4 w-4" />}
                  </DropdownMenuItem>
                  <DropdownMenuItem onClick={() => setEnterBehaviorPersist('enter_newline')}>
                    <span className={enterBehavior === 'enter_newline' ? 'font-medium' : ''}>Enter 换行 · Shift+Enter 发送</span>
                    {enterBehavior === 'enter_newline' && <Check className="ml-auto h-4 w-4" />}
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
              <HighlightInput
                value={input}
                onChange={setInput}
                onEnter={send}
                fileNames={fileNames}
                placeholder={enterBehavior === 'enter_send'
                  ? '输入消息，Enter 发送（Shift+Enter 换行）；可直接粘贴图片；拖工作区文件进来会形成蓝色的 @文件名 引用'
                  : '输入消息，Enter 换行（Shift+Enter 发送）；可直接粘贴图片；拖工作区文件进来会形成蓝色的 @文件名 引用'}
                textareaRef={inputRef}
                onPaste={onPasteImage}
                enterSends={enterBehavior === 'enter_send'}
              />
              {streaming ? (
                <Button onClick={stopTurn} size="icon" variant="outline" title="停止生成（服务端本轮也会随之取消）">
                  <Square className="h-4 w-4" />
                </Button>
              ) : (
                <Button onClick={send} disabled={!input.trim() && images.length === 0} size="icon" title="发送">
                  <Send className="h-4 w-4" />
                </Button>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

// ---------- 高亮输入框：透明文字 textarea 叠加高亮底层（@文件名 命中工作区文件时变蓝） ----------
// scrollbar-gutter:stable：两层都恒定预留滚动条槽位——否则超 10 行时 textarea 出现滚动条、
// 排版宽度比高亮层窄 ~15px，换行点不同导致光标/选区与可见文字逐行累积错位
const HL_SHARED = 'whitespace-pre-wrap break-words rounded-md border px-3 py-2 text-sm leading-relaxed [scrollbar-gutter:stable]'

function HighlightInput({ value, onChange, onEnter, fileNames, placeholder, textareaRef, onPaste, enterSends }) {
  const backdropRef = useRef(null)
  const parts = useMemo(() => {
    const out = []
    const hits = findRefs(value, fileNames)
    let cursor = 0
    hits.forEach((h, i) => {
      if (h.start > cursor) out.push(<span key={`t${i}`}>{value.slice(cursor, h.start)}</span>)
      out.push(
        <span key={`r${i}`} className="rounded bg-blue-100 text-blue-700 dark:bg-blue-900/60 dark:text-blue-300">
          {value.slice(h.start, h.end)}
        </span>,
      )
      cursor = h.end
    })
    if (cursor < value.length) out.push(<span key="tail">{value.slice(cursor)}</span>)
    return out
  }, [value, fileNames])

  return (
    <div className="relative flex-1">
      {/* 高亮底层进文档流撑高度：随内容 1→10 行自动长高（16.25em = 10 行 × leading-relaxed 1.625 + 内边距边框），
          超 10 行封顶，textarea 内部滚动，onScroll 同步底层 scrollTop 保持高亮对齐 */}
      <div
        ref={backdropRef}
        aria-hidden
        className={cn(HL_SHARED, 'pointer-events-none relative max-h-[calc(16.25em+18px)] min-h-[44px] overflow-hidden border-transparent text-foreground')}
      >
        {parts}
        {'​'}
      </div>
      <textarea
        ref={textareaRef}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.nativeEvent.isComposing) return // 中文输入法组词回车不触发发送/换行
          if (e.key !== 'Enter') return
          const sendNow = enterSends ? !e.shiftKey : e.shiftKey // M8e：按偏好决定 Enter 还是 Shift+Enter 发送
          if (sendNow) { e.preventDefault(); onEnter() } // else 不 preventDefault，走默认换行
        }}
        onScroll={(e) => {
          if (backdropRef.current) backdropRef.current.scrollTop = e.target.scrollTop
        }}
        onPaste={onPaste}
        placeholder={placeholder}
        rows={1}
        className={cn(
          HL_SHARED,
          'absolute inset-0 h-full resize-none overflow-auto bg-transparent text-transparent caret-black placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring dark:caret-white',
        )}
      />
    </div>
  )
}


// ---------- 中栏：工作区文件面板（支持子文件夹分组） ----------
// 工作区面板可拖宽（M3.14）：默认 256px（原 w-64），上限 384px（+50%），宽度持久化
const WS_PANEL_MIN_W = 256
const WS_PANEL_MAX_W = 384
const WS_PANEL_W_KEY = 'ea_ws_panel_width'

// M36：可拖拽排序的文件夹行（dnd-kit；仅桌面端使用，移动端抽屉渲染普通行）
function SortableFolderRow({ id, children }) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id })
  return (
    <div
      ref={setNodeRef}
      style={{ transform: CSS.Transform.toString(transform), transition }}
      {...attributes}
      {...listeners}
      className={isDragging ? 'relative z-10 opacity-70' : undefined}
    >
      {children}
    </div>
  )
}

function WorkspacePanel({ agentId, onInsert, preview, onPreview, mobile = false }) {
  const [files, setFiles] = useState([])
  // M36：折叠状态语义反转——expanded 默认空 = 全折叠（新文件夹天然落入默认），localStorage per-agent 持久化
  const wsExpandedKey = `ea_ws_expanded_${agentId}`
  const [expanded, setExpanded] = useState(() => {
    try { return new Set(JSON.parse(localStorage.getItem(`ea_ws_expanded_${agentId}`) || '[]')) } catch { return new Set() }
  })
  const [folderPrefs, setFolderPrefs] = useState({}) // M36：path → {pinned, sort_order}（服务端存，跨设备同步）
  const [q, setQ] = useState('') // M36：工作区搜索（只搜文件/文件夹名，纯前端过滤）
  const [tab, setTab] = useState('files') // files=工作区文件 / tasks=定时任务（M3.8）/ shared=共享给我（M5）
  const [shareOpen, setShareOpen] = useState(false)   // 共享管理 Dialog
  const [sharePaths, setSharePaths] = useState([])    // 从文件/文件夹行进入时预填
  const [width, setWidth] = useState(() => {
    const saved = Number(localStorage.getItem(WS_PANEL_W_KEY))
    return saved >= WS_PANEL_MIN_W && saved <= WS_PANEL_MAX_W ? saved : WS_PANEL_MIN_W
  })
  const fileInputRef = useRef(null)
  const uploadFolderRef = useRef('') // 本次上传目标文件夹

  const startResize = (e) => {
    e.preventDefault()
    const startX = e.clientX
    const startW = width
    const move = (ev) => {
      const w = Math.min(WS_PANEL_MAX_W, Math.max(WS_PANEL_MIN_W, startW + ev.clientX - startX))
      setWidth(w)
      localStorage.setItem(WS_PANEL_W_KEY, String(w))
    }
    const up = () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
  }

  const load = useCallback(() => {
    api(`/agents/${agentId}/workspace`).then(setFiles).catch((e) => toast.error(e.message))
  }, [agentId])

  useEffect(load, [load])
  useEffect(() => {
    const h = () => load()
    window.addEventListener('workspace-changed', h)
    return () => window.removeEventListener('workspace-changed', h)
  }, [load])

  // M36：切换 agent 时重读该 agent 的展开态（组件不一定随路由重挂载）
  useEffect(() => {
    try { setExpanded(new Set(JSON.parse(localStorage.getItem(wsExpandedKey) || '[]'))) } catch { setExpanded(new Set()) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId])

  const setExpandedAndSave = (updater) => {
    setExpanded((prev) => {
      const next = typeof updater === 'function' ? updater(prev) : updater
      try { localStorage.setItem(wsExpandedKey, JSON.stringify([...next])) } catch { /* 隐私模式等写入失败不挡交互 */ }
      return next
    })
  }

  // M36：文件夹置顶/排序偏好（失败静默，不挡文件列表）
  const loadPrefs = useCallback(() => {
    api(`/agents/${agentId}/workspace/folder-prefs`)
      .then((rows) => setFolderPrefs(Object.fromEntries(rows.map((r) => [r.path, r]))))
      .catch(() => {})
  }, [agentId])
  useEffect(loadPrefs, [loadPrefs])
  const prefOf = (p) => folderPrefs[p] || { pinned: false, sort_order: Number.MAX_SAFE_INTEGER }

  const remove = async (f) => {
    if (!(await confirmDialog({ title: '删除文件', desc: `确定删除文件「${f.filename.split('/').pop()}」？`, confirmText: '删除', danger: true }))) return
    try {
      await api(`/agents/${agentId}/workspace/${f.id}`, { method: 'DELETE' })
      if (preview?.id === f.id) onPreview(null)
      toast.success(`已删除 ${f.filename}`)
      load()
      window.dispatchEvent(new Event('workspace-changed')) // 引用清单同步移除
    } catch (e) {
      toast.error(e.message)
    }
  }

  const renameFile = async (f) => {
    const oldName = f.filename.split('/').pop()
    const name = await promptDialog({ title: '重命名文件', defaultValue: oldName, placeholder: '输入新的文件名', confirmText: '保存' })
    if (!name || name === oldName) return
    try {
      const r = await api(`/agents/${agentId}/workspace/${f.id}`, { method: 'PATCH', body: { filename: name } })
      toast.success(`已重命名为 ${r.filename.split('/').pop()}`)
      if (preview?.id === f.id) onPreview(null)
      load()
      window.dispatchEvent(new Event('workspace-changed')) // 引用清单同步新文件名
    } catch (e) {
      toast.error(e.message)
    }
  }

  const togglePinFile = async (f) => {
    try {
      await api(`/agents/${agentId}/workspace/${f.id}`, { method: 'PATCH', body: { pinned: !f.pinned } })
      load()
    } catch (e) {
      toast.error(e.message)
    }
  }

  const moveFile = async (f, folder) => {
    try {
      const r = await api(`/agents/${agentId}/workspace/${f.id}`, { method: 'PATCH', body: { folder } })
      toast.success(`已移动到 ${folder || '根目录'}`)
      if (preview?.id === f.id && r.filename !== f.filename) onPreview(null)
      load()
      window.dispatchEvent(new Event('workspace-changed'))
    } catch (e) {
      toast.error(e.message)
    }
  }

  // 下载到本地：复用 /raw 端点（带鉴权拉字节），文件名取相对路径最后一段
  const downloadFile = async (f) => {
    try {
      const buf = await fetchRaw(`/api/agents/${agentId}/workspace/${f.id}/raw`)
      const url = URL.createObjectURL(new Blob([buf]))
      const a = document.createElement('a')
      a.href = url
      a.download = f.filename.split('/').pop()
      a.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      toast.error(e.message || '下载失败')
    }
  }

  const removeFolder = async (folder) => {
    const n = files.filter((f) => f.filename.startsWith(folder + '/')).length
    if (!(await confirmDialog({ title: '删除文件夹', desc: `确定删除文件夹「${folder}」？其中的 ${n} 个文件会一并删除。`, confirmText: '删除', danger: true }))) return
    try {
      const r = await api(`/agents/${agentId}/workspace/folder?path=${encodeURIComponent(folder)}`, { method: 'DELETE' })
      if (preview?.filename?.startsWith(folder + '/')) onPreview(null)
      toast.success(`已删除文件夹 ${folder}（${r.deleted} 个文件）`)
      load()
    } catch (e) {
      toast.error(e.message)
    }
  }

  const openPreview = async (f) => {
    if (preview?.id === f.id) {
      onPreview(null)
      return
    }
    try {
      const detail = await api(`/agents/${agentId}/workspace/${f.id}`)
      onPreview(detail)
    } catch (e) {
      toast.error(e.message)
    }
  }

  const pickUpload = (folder) => {
    uploadFolderRef.current = folder
    fileInputRef.current?.click()
  }

  const doUpload = async (fileList) => {
    const folder = uploadFolderRef.current
    for (const file of fileList) {
      if (file.size > MAX_UPLOAD) {
        toast.error(`文件「${file.name}」超过 100MB 上限`)
        continue
      }
      const fd = new FormData()
      fd.append('file', file)
      if (folder) fd.append('folder', folder)
      try {
        await api(`/agents/${agentId}/workspace`, { method: 'POST', formData: fd })
        toast.success(`已上传 ${file.name}`)
      } catch (err) {
        toast.error(err.message)
      }
    }
    load()
    window.dispatchEvent(new Event('workspace-changed')) // @引用清单同步新文件（否则新文件引用不高亮、发送解析不到）
  }

  // M38：parent 非空 = 从文件夹 ⋯ 菜单进入，在其内部建直属子文件夹（名字禁含 /）；
  // parent 为空 = 顶部工具条入口，保留「支持层级，如 reports/2026」。
  const newFolder = async (parent = '') => {
    const name = await promptDialog(
      parent
        ? { title: `在「${parent.split('/').pop()}」内新建子文件夹`, placeholder: '子文件夹名', confirmText: '创建' }
        : { title: '新建文件夹', placeholder: '支持层级，如 reports/2026', confirmText: '创建' },
    )
    if (!name) return
    if (parent && name.includes('/')) {
      toast.error('子文件夹名不能包含 /')
      return
    }
    const path = parent ? `${parent}/${name}` : name
    try {
      await api(`/agents/${agentId}/workspace/file`, { method: 'PUT', body: { path: `${path}/.keep`, content: '' } })
      toast.success(`已创建文件夹 ${path}`)
      if (parent) expandToFolder(parent) // 展开父链，新子文件夹立即可见
      load()
    } catch (e) {
      toast.error(e.message)
    }
  }

  // 按父路径分组（隐藏 .keep 占位文件）；置顶排前面
  const visible = files.filter((f) => !f.filename.endsWith('/.keep') && f.filename !== '.keep')
  const byPin = (a, b) => (b.pinned - a.pinned) || (b.id - a.id)
  const folders = useMemo(() => {
    const set = new Set()
    files.forEach((f) => {
      const parts = f.filename.split('/')
      for (let i = 1; i < parts.length; i++) set.add(parts.slice(0, i).join('/'))
    })
    return [...set].sort()
  }, [files])
  const rootFiles = visible.filter((f) => !f.filename.includes('/')).sort(byPin)
  const filesIn = (folder) => visible.filter((f) => f.filename.startsWith(folder + '/') && !f.filename.slice(folder.length + 1).includes('/')).sort(byPin)

  // M36：文件夹渲染序 = 按 parent 分组 → 兄弟组内（置顶优先 → sort_order → 字母）→ DFS 展平
  const orderedFolders = useMemo(() => {
    const byParent = new Map()
    folders.forEach((p) => {
      const parent = p.includes('/') ? p.slice(0, p.lastIndexOf('/')) : ''
      byParent.set(parent, [...(byParent.get(parent) || []), p])
    })
    const byPref = (a, b) =>
      ((folderPrefs[b]?.pinned ? 1 : 0) - (folderPrefs[a]?.pinned ? 1 : 0)) ||
      ((folderPrefs[a]?.sort_order ?? Number.MAX_SAFE_INTEGER) - (folderPrefs[b]?.sort_order ?? Number.MAX_SAFE_INTEGER)) ||
      a.localeCompare(b, 'zh-CN')
    const out = []
    const walk = (parent) => (byParent.get(parent) || []).sort(byPref).forEach((p) => { out.push(p); walk(p) })
    walk('')
    return out
  }, [folders, folderPrefs])

  // 任一祖先未展开时，后代文件夹与文件都隐藏
  const isFolderHidden = (p) => {
    const parts = p.split('/')
    return parts.slice(0, -1).some((_, i) => !expanded.has(parts.slice(0, i + 1).join('/')))
  }
  const renderedFolders = useMemo(() => orderedFolders.filter((p) => !isFolderHidden(p)), [orderedFolders, expanded]) // eslint-disable-line react-hooks/exhaustive-deps

  // M36：搜索命中（大小写不敏感，纯前端过滤）
  const ql = q.trim().toLowerCase()
  const hitFolders = ql ? orderedFolders.filter((p) => p.toLowerCase().includes(ql)) : []
  const hitFiles = ql ? visible.filter((f) => f.filename.toLowerCase().includes(ql)) : []

  const toggleFolder = (p) => {
    setExpandedAndSave((prev) => {
      const next = new Set(prev)
      if (next.has(p)) next.delete(p)
      else next.add(p)
      return next
    })
  }

  // M36：搜索命中文件夹后，把它和全部祖先展开再回树视图定位
  const expandToFolder = (p) => {
    const parts = p.split('/')
    setExpandedAndSave((prev) => {
      const next = new Set(prev)
      for (let i = 1; i <= parts.length; i++) next.add(parts.slice(0, i).join('/'))
      return next
    })
  }

  // M36：文件夹改名后迁移展开态（前缀替换），保住用户已展开的层级
  const migrateExpanded = (oldPath, newPath) => {
    setExpandedAndSave((prev) => {
      const next = new Set()
      prev.forEach((p) => {
        if (p === oldPath) next.add(newPath)
        else if (p.startsWith(oldPath + '/')) next.add(newPath + p.slice(oldPath.length))
        else next.add(p)
      })
      return next
    })
  }

  const renameFolder = async (folder) => {
    const oldName = folder.split('/').pop()
    const name = await promptDialog({ title: '重命名文件夹', defaultValue: oldName, placeholder: '输入新的文件夹名', confirmText: '保存' })
    if (!name || name === oldName) return
    try {
      const r = await api(`/agents/${agentId}/workspace/folder`, { method: 'PATCH', body: { path: folder, name } })
      migrateExpanded(folder, r.path)
      toast.success(`已重命名为 ${r.path}`)
      load()
      loadPrefs()
      window.dispatchEvent(new Event('workspace-changed')) // 引用清单同步新路径
    } catch (e) {
      toast.error(e.message)
    }
  }

  const togglePinFolder = async (folder) => {
    const next = !prefOf(folder).pinned
    setFolderPrefs((prev) => ({ ...prev, [folder]: { ...prefOf(folder), pinned: next } })) // 乐观更新
    try {
      await api(`/agents/${agentId}/workspace/folder-prefs`, { method: 'PUT', body: { items: [{ path: folder, pinned: next }] } })
    } catch (e) {
      toast.error(e.message)
      loadPrefs() // 回滚
    }
  }

  // M36：拖拽排序（dnd-kit，仅桌面端挂 DndContext；同层兄弟间且不跨置顶分组才生效）
  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 5 } }))
  const parentOf = (p) => (p.includes('/') ? p.slice(0, p.lastIndexOf('/')) : '')
  const onFolderDragEnd = ({ active, over }) => {
    if (!over || active.id === over.id) return
    const a = String(active.id)
    const b = String(over.id)
    if (parentOf(a) !== parentOf(b)) return // 只允许同层兄弟间排序
    if (!!prefOf(a).pinned !== !!prefOf(b).pinned) return // 不跨置顶分组，保住「置顶恒在前」
    const siblings = orderedFolders.filter((p) => parentOf(p) === parentOf(a))
    const from = siblings.indexOf(a)
    const to = siblings.indexOf(b)
    if (from < 0 || to < 0) return
    const items = arrayMove(siblings, from, to).map((p, i) => ({ path: p, sort_order: i }))
    setFolderPrefs((prev) => {
      const merged = { ...prev }
      items.forEach((it) => { merged[it.path] = { ...(merged[it.path] || { pinned: false }), sort_order: it.sort_order } })
      return merged
    })
    api(`/agents/${agentId}/workspace/folder-prefs`, { method: 'PUT', body: { items } }).catch((e) => {
      toast.error(e.message)
      loadPrefs() // 回滚
    })
  }

  const [moveTarget, setMoveTarget] = useState(null) // 「移动至」对话框的目标文件

  const renderFile = (f, indent) => (
    <div
      key={f.id}
      draggable={!mobile}
      onDragStart={mobile ? undefined : (e) => e.dataTransfer.setData('text/workspace-file', f.filename)}
      onClick={() => openPreview(f)}
      className={cn(
        'group mb-0.5 flex items-center gap-2 rounded-md px-2 py-1.5 text-sm hover:bg-accent/60',
        mobile ? 'cursor-pointer' : 'cursor-grab',
        preview?.id === f.id && 'bg-accent',
      )}
      style={indent ? { paddingLeft: `${8 + indent * 16}px` } : undefined}
      title={mobile ? `${f.filename}\n点击预览，⋯ 菜单可引用` : `${f.filename}\n点击预览，拖到输入框可引用`}
    >
      <FileText className="h-4 w-4 shrink-0 text-muted-foreground" />
      {f.pinned && <Pin className="h-3 w-3 shrink-0 text-muted-foreground" />}
      <span className="min-w-0 flex-1 truncate">{f.filename.split('/').pop()}</span>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          {/* 必须用原生 button：Button 组件没 forwardRef，React 18 下 Radix asChild 挂不上 ref 导致菜单打不开 */}
          <button
            title="文件设置"
            className="shrink-0 rounded p-1 text-muted-foreground opacity-100 hover:bg-background hover:text-foreground focus:opacity-100 data-[state=open]:opacity-100 md:opacity-0 md:group-hover:opacity-100 md:focus:opacity-100 md:data-[state=open]:opacity-100"
            onClick={(e) => e.stopPropagation()}
          >
            <MoreHorizontal className="h-3 w-3" />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" sideOffset={4} className="w-36" onClick={(e) => e.stopPropagation()}>
          <DropdownMenuItem onClick={() => onInsert(f.filename)}>
            <AtSign className="mr-2 h-3.5 w-3.5" /> 引用到输入框
          </DropdownMenuItem>
          <DropdownMenuItem onClick={() => togglePinFile(f)}>
            {f.pinned ? <PinOff className="mr-2 h-3.5 w-3.5" /> : <Pin className="mr-2 h-3.5 w-3.5" />}
            {f.pinned ? '取消置顶' : '置顶'}
          </DropdownMenuItem>
          <DropdownMenuItem onClick={() => renameFile(f)}>
            <Pencil className="mr-2 h-3.5 w-3.5" /> 重命名
          </DropdownMenuItem>
          <DropdownMenuItem onClick={() => setMoveTarget(f)}>
            <FolderInput className="mr-2 h-3.5 w-3.5" /> 移动至
          </DropdownMenuItem>
          <DropdownMenuItem onClick={() => downloadFile(f)}>
            <Download className="mr-2 h-3.5 w-3.5" /> 下载
          </DropdownMenuItem>
          <DropdownMenuItem onClick={() => { setSharePaths([f.filename]); setShareOpen(true) }}>
            <Share2 className="mr-2 h-3.5 w-3.5" /> 分享此文件
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem className="text-destructive focus:text-destructive" onClick={() => remove(f)}>
            <Trash2 className="mr-2 h-3.5 w-3.5" /> 删除
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  )

  // M36：文件夹行 = 展开箭头 + 图标 + 名 + ⋯ 菜单（共享/上传/置顶/重命名/删除）
  const renderFolder = (folder) => {
    const parts = folder.split('/')
    const depth = parts.length
    const isCollapsed = !expanded.has(folder)
    const pinned = !!folderPrefs[folder]?.pinned
    const row = (
      <div
        className="group mb-0.5 flex cursor-pointer items-center gap-1.5 rounded-md px-2 py-1.5 text-sm hover:bg-accent/60"
        style={{ paddingLeft: `${2 + (depth - 1) * 16}px` }}
        onClick={() => toggleFolder(folder)}
        title={folder}
      >
        {isCollapsed ? <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted-foreground" /> : <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />}
        <Folder className="h-4 w-4 shrink-0 text-amber-500" />
        {pinned && <Pin className="h-3 w-3 shrink-0 text-muted-foreground" />}
        <span className="min-w-0 flex-1 truncate font-medium">{parts[parts.length - 1]}</span>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            {/* 必须用原生 button：Button 组件没 forwardRef，React 18 下 Radix asChild 挂不上 ref 导致菜单打不开 */}
            <button
              title="文件夹设置"
              className="shrink-0 rounded p-1 text-muted-foreground opacity-100 hover:bg-background hover:text-foreground focus:opacity-100 data-[state=open]:opacity-100 md:opacity-0 md:group-hover:opacity-100 md:focus:opacity-100 md:data-[state=open]:opacity-100"
              onClick={(e) => e.stopPropagation()}
            >
              <MoreHorizontal className="h-3 w-3" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" sideOffset={4} className="w-40" onClick={(e) => e.stopPropagation()}>
            <DropdownMenuItem onClick={() => { setSharePaths([folder]); setShareOpen(true) }}>
              <Share2 className="mr-2 h-3.5 w-3.5" /> 共享此文件夹
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => pickUpload(folder)}>
              <Paperclip className="mr-2 h-3.5 w-3.5" /> 上传到此文件夹
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => newFolder(folder)}>
              <FolderPlus className="mr-2 h-3.5 w-3.5" /> 新建子文件夹
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => togglePinFolder(folder)}>
              {pinned ? <PinOff className="mr-2 h-3.5 w-3.5" /> : <Pin className="mr-2 h-3.5 w-3.5" />}
              {pinned ? '取消置顶' : '置顶'}
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => renameFolder(folder)}>
              <Pencil className="mr-2 h-3.5 w-3.5" /> 重命名
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem className="text-destructive focus:text-destructive" onClick={() => removeFolder(folder)}>
              <Trash2 className="mr-2 h-3.5 w-3.5" /> 删除文件夹
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    )
    const children = isCollapsed ? null : filesIn(folder).map((f) => renderFile(f, depth))
    // 移动端抽屉不挂 dnd（触屏长按拖拽与抽屉手势冲突）
    if (mobile) return <div key={folder}>{row}{children}</div>
    return <SortableFolderRow key={folder} id={folder}>{row}{children}</SortableFolderRow>
  }

  const folderTree = mobile ? (
    renderedFolders.map((folder) => renderFolder(folder))
  ) : (
    <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={onFolderDragEnd}>
      <SortableContext items={renderedFolders} strategy={verticalListSortingStrategy}>
        {renderedFolders.map((folder) => renderFolder(folder))}
      </SortableContext>
    </DndContext>
  )

  return (
    <div className={cn('relative flex shrink-0 flex-col bg-muted/30', mobile ? 'min-h-0 w-full flex-1' : 'border-r')} style={mobile ? undefined : { width }}>
      {/* 右缘拖拽条：pointer 拖宽/拖窄面板（w-1 不挡行内 ⋯ 按钮）；移动端抽屉无拖宽 */}
      {!mobile && (
        <div
          className="absolute inset-y-0 right-0 z-10 w-1 cursor-col-resize hover:bg-accent"
          title="拖动调整面板宽度"
          onPointerDown={startResize}
        />
      )}
      <Tabs value={tab} onValueChange={setTab} className="flex min-h-0 flex-1 flex-col gap-0">
        <div className="flex shrink-0 items-center justify-between border-b px-2 py-2">
          <TabsList className="h-8 p-[2px]">
            <TabsTrigger value="files" className="px-2.5 text-xs">工作区文件</TabsTrigger>
            <TabsTrigger value="shared" className="px-2.5 text-xs">共享给我</TabsTrigger>
            <TabsTrigger value="tasks" className="px-2.5 text-xs">定时任务</TabsTrigger>
          </TabsList>
          <input
            ref={fileInputRef}
            type="file"
            multiple
            className="hidden"
            onChange={async (e) => { await doUpload(e.target.files); e.target.value = '' }}
          />
        </div>

        <TabsContent value="files" className="mt-0 flex min-h-0 flex-col">
          {/* 工作区文件专属操作：只在 files tab 内显示，随 tab 切换隐藏 */}
          <div className="flex shrink-0 items-center justify-end gap-0.5 border-b px-2 py-1.5">
            <Button variant="ghost" size="icon" className="h-7 w-7" title="共享文件/文件夹给其他同事" onClick={() => { setSharePaths([]); setShareOpen(true) }}>
              <Share2 className="h-3.5 w-3.5" />
            </Button>
            <Button variant="ghost" size="icon" className="h-7 w-7" title="新建文件夹" onClick={newFolder}>
              <FolderPlus className="h-3.5 w-3.5" />
            </Button>
            <Button variant="ghost" size="icon" className="h-7 w-7" title="上传文件" onClick={() => pickUpload('')}>
              <Paperclip className="h-3.5 w-3.5" />
            </Button>
          </div>
          {/* M36：搜索框（只搜文件/文件夹名，纯前端过滤；min-w-0 防撑破窄面板） */}
          <div className="flex shrink-0 items-center gap-1 border-b px-2 py-1.5">
            <Search className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
            <Input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="搜索文件或文件夹"
              className="h-7 min-w-0 flex-1 border-0 bg-transparent px-1 text-xs shadow-none focus-visible:ring-0"
            />
            {q && (
              <Button variant="ghost" size="icon" className="h-6 w-6 shrink-0" title="清空" onClick={() => setQ('')}>
                <X className="h-3 w-3" />
              </Button>
            )}
          </div>
          {/* 原生滚动：同会话列表，避免 Radix display:table 内层把行尾按钮裁掉 */}
          <div className="min-h-0 flex-1 overflow-y-auto">
            <div className="p-2">
          {q.trim() ? (
            <>
              {/* 搜索模式：扁平结果列表；点中文件夹 → 展开定位回树视图 */}
              {hitFolders.map((p) => (
                <div
                  key={p}
                  className="mb-0.5 flex cursor-pointer items-center gap-1.5 rounded-md px-2 py-1.5 text-sm hover:bg-accent/60"
                  title={p}
                  onClick={() => { expandToFolder(p); setQ('') }}
                >
                  <Folder className="h-4 w-4 shrink-0 text-amber-500" />
                  <span className="min-w-0 flex-1 truncate font-medium">{p}</span>
                </div>
              ))}
              {hitFiles.map((f) => renderFile(f, 0))}
              {hitFolders.length === 0 && hitFiles.length === 0 && (
                <div className="px-2 py-8 text-center text-xs text-muted-foreground">无匹配文件</div>
              )}
            </>
          ) : (
            <>
          {rootFiles.map((f) => renderFile(f, 0))}
          {folderTree}
          {files.length === 0 && (
            <div className="px-2 py-8 text-center text-xs text-muted-foreground">暂无文件<br />{mobile ? '点右上角回形针上传' : '可拖文件到对话区上传'}</div>
          )}
            </>
          )}
            </div>
          </div>
        </TabsContent>

        <TabsContent value="shared" className="mt-0 flex min-h-0 flex-col">
          <SharedWithMe agentId={agentId} preview={preview} onPreview={onPreview} />
        </TabsContent>

        <TabsContent value="tasks" className="mt-0 flex min-h-0 flex-col">
          <ScheduledTasksPanel agentId={agentId} />
        </TabsContent>
      </Tabs>

      <ShareDialog agentId={agentId} folders={folders} files={visible} initialPaths={sharePaths} open={shareOpen} onOpenChange={setShareOpen} />

      {/* 移动至：选目标文件夹（排除当前所在文件夹） */}
      <Dialog open={!!moveTarget} onOpenChange={(v) => { if (!v) setMoveTarget(null) }}>
        <DialogContent className="sm:max-w-sm">
          <DialogHeader>
            <DialogTitle>移动文件</DialogTitle>
            <DialogDescription className="truncate">把「{moveTarget?.filename.split('/').pop()}」移动到：</DialogDescription>
          </DialogHeader>
          <div className="max-h-64 overflow-y-auto">
            {['', ...folders]
              .filter((fd) => fd !== (moveTarget?.filename.includes('/') ? moveTarget.filename.slice(0, moveTarget.filename.lastIndexOf('/')) : ''))
              .map((fd) => (
                <button
                  key={fd || '__root__'}
                  className="flex w-full items-center gap-2 rounded-md px-3 py-2 text-left text-sm hover:bg-accent"
                  onClick={() => { const t = moveTarget; setMoveTarget(null); moveFile(t, fd) }}
                  title={fd || '根目录'}
                >
                  <Folder className="h-4 w-4 shrink-0 text-amber-500" />
                  <span className="truncate">{fd || '根目录'}</span>
                </button>
              ))}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setMoveTarget(null)}>取消</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

// ---------- 工作区共享（M5/M5.1）：A 把文件/文件夹（可多选）只读共享给 B/部门/全员；B 实时看最新内容，可另存到自己工作区 ----------

// paths=[] = 整个工作区（含今后新增文件）；元素可为文件或文件夹路径
function pathsLabel(paths) {
  if (!paths || paths.length === 0) return '整个工作区'
  return paths.map((p) => p.split('/').pop()).join('、')
}

// M20：「转交给数字人」并入下方 ShareDialog（共享给 → 我的数字人），不再单独成对话框
function ShareDialog({ agentId, folders, files, initialPaths, open, onOpenChange }) {
  const [shares, setShares] = useState([])
  const [options, setOptions] = useState({ users: [], departments: [] })
  const [all, setAll] = useState(false)               // 勾选「整个工作区」
  const [selected, setSelected] = useState(new Set()) // 勾选的文件/文件夹路径
  const [granteeType, setGranteeType] = useState('user')
  const [granteeId, setGranteeId] = useState('')
  const [myAgents, setMyAgents] = useState(null) // M20：「我的数字人」转交目标列表（null=加载中）
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    api(`/agents/${agentId}/workspace/shares`).then(setShares).catch((e) => toast.error(e.message))
  }, [agentId])

  useEffect(() => {
    if (!open) return
    setAll(false)
    setSelected(new Set(initialPaths || []))
    setGranteeType('user')
    setGranteeId('')
    setMyAgents(null)
    load()
    api('/shared/options').then(setOptions).catch(() => {})
    // 转交目标 = 我的其他数字人（注意没有 /my-agents/wall 这个端点，会撞 /{agent_id} 路由 422）
    api('/agents')
      .then((w) => setMyAgents((w.my_instances || []).filter((a) => a.id !== Number(agentId))))
      .catch(() => setMyAgents([]))
  }, [open, initialPaths, load, agentId])

  const toggle = (p) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(p)) next.delete(p)
      else next.add(p)
      return next
    })
  }

  const create = async () => {
    // M20：「我的数字人」= 转交副本（不建共享），逐项走 M19 转交端点；单个失败继续其余
    if (granteeType === 'agent') {
      const paths = [...selected]
      if (paths.length === 0) {
        toast.error('请先勾选要转交的文件或文件夹')
        return
      }
      const target = (myAgents || []).find((a) => String(a.id) === granteeId)
      setBusy(true)
      let ok = 0
      for (const p of paths) {
        try {
          if (folders.includes(p)) {
            await api(`/agents/${agentId}/workspace/transfer-folder`, { method: 'POST', body: { path: p, target_agent_id: Number(granteeId) } })
          } else {
            const f = files.find((x) => x.filename === p)
            if (!f) continue
            await api(`/agents/${agentId}/workspace/${f.id}/transfer`, { method: 'POST', body: { target_agent_id: Number(granteeId) } })
          }
          ok++
        } catch (e) {
          toast.error(e.message)
        }
      }
      setBusy(false)
      if (ok > 0) {
        toast.success(`已转交 ${ok} 项给「${target?.name || '目标数字人'}」，去它的对话里继续处理吧`)
        onOpenChange(false)
      }
      return
    }
    const paths = all ? [] : [...selected]
    if (!all && paths.length === 0) {
      toast.error('请先勾选要共享的文件或文件夹')
      return
    }
    try {
      await api(`/agents/${agentId}/workspace/shares`, {
        method: 'POST',
        body: { paths, grantee_type: granteeType, grantee_id: granteeType === 'all' ? null : Number(granteeId) },
      })
      toast.success('已创建共享')
      load()
    } catch (e) {
      toast.error(e.message)
    }
  }

  const revoke = async (s) => {
    if (!(await confirmDialog({ title: '撤销共享', desc: `确定撤销「${pathsLabel(s.paths)}」对 ${s.grantee_label} 的共享？对方将立即看不到这些内容。`, confirmText: '撤销', danger: true }))) return
    try {
      await api(`/agents/${agentId}/workspace/shares/${s.id}`, { method: 'DELETE' })
      toast.success('已撤销共享')
      load()
    } catch (e) {
      toast.error(e.message)
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>共享文件 / 文件夹</DialogTitle>
          <DialogDescription>
            {granteeType === 'agent'
              ? '把勾选的文件/文件夹复制一份到目标数字人的工作区（原件保留，两边互不影响）。'
              : '把本工作区的文件或文件夹只读共享给同事；对方实时看到最新内容，可另存到他自己的工作区。'}
          </DialogDescription>
        </DialogHeader>

        {shares.length > 0 && (
          <div className="max-h-40 space-y-1 overflow-y-auto rounded-md border p-2">
            {shares.map((s) => (
              <div key={s.id} className="flex items-center gap-2 text-sm">
                <Share2 className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                <span className="truncate" title={(s.paths || []).join('\n') || '整个工作区'}>{pathsLabel(s.paths)}</span>
                <span className="shrink-0 text-muted-foreground">→ {s.grantee_label}</span>
                <button className="ml-auto shrink-0 rounded p-1 text-muted-foreground hover:bg-accent hover:text-destructive" title="撤销共享" onClick={() => revoke(s)}>
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </div>
            ))}
          </div>
        )}

        <div className="space-y-1.5">
          <div className="text-xs text-muted-foreground">
            {granteeType === 'agent' ? '转交内容' : '共享内容'}{!all && selected.size > 0 ? `（已选 ${selected.size} 项）` : ''}
          </div>
          <div className="max-h-48 overflow-y-auto rounded-md border p-2">
            {/* 转交是一次性复制，无法覆盖未来文件——agent 模式下禁用「整个工作区」 */}
            <label className={cn('flex cursor-pointer items-center gap-2 rounded px-1.5 py-1 text-sm hover:bg-accent/60', granteeType === 'agent' && 'pointer-events-none opacity-40')} title={granteeType === 'agent' ? '转交副本不支持整个工作区' : undefined}>
              <input type="checkbox" className="h-3.5 w-3.5 accent-foreground" checked={all} disabled={granteeType === 'agent'} onChange={(e) => setAll(e.target.checked)} />
              <span className="font-medium">整个工作区</span>
              <span className="text-xs text-muted-foreground">（含今后新增的文件）</span>
            </label>
            <div className={cn('mt-1 space-y-0.5', all && 'pointer-events-none opacity-40')}>
              {folders.map((fd) => (
                <label key={fd} className="flex cursor-pointer items-center gap-2 rounded px-1.5 py-1 text-sm hover:bg-accent/60" title={fd}>
                  <input type="checkbox" className="h-3.5 w-3.5 accent-foreground" checked={selected.has(fd)} onChange={() => toggle(fd)} />
                  <Folder className="h-3.5 w-3.5 shrink-0 text-amber-500" />
                  <span className="truncate">{fd}</span>
                </label>
              ))}
              {files.map((f) => (
                <label key={f.id} className="flex cursor-pointer items-center gap-2 rounded px-1.5 py-1 text-sm hover:bg-accent/60" title={f.filename}>
                  <input type="checkbox" className="h-3.5 w-3.5 accent-foreground" checked={selected.has(f.filename)} onChange={() => toggle(f.filename)} />
                  <FileText className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                  <span className="truncate">{f.filename}</span>
                </label>
              ))}
              {folders.length === 0 && files.length === 0 && (
                <div className="px-1.5 py-2 text-xs text-muted-foreground">工作区还没有内容</div>
              )}
            </div>
          </div>
        </div>

        <div className="space-y-1.5">
          <div className="text-xs text-muted-foreground">共享给</div>
          <Select value={granteeType} onValueChange={(v) => { setGranteeType(v); setGranteeId(''); if (v === 'agent') setAll(false) }}>
            <SelectTrigger className="h-8 text-sm"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="user">指定用户</SelectItem>
              <SelectItem value="department">指定部门</SelectItem>
              <SelectItem value="all">全员</SelectItem>
              <SelectItem value="agent">我的数字人（复制副本给它）</SelectItem>
            </SelectContent>
          </Select>
        </div>
        {granteeType === 'department' && (
          <DepartmentSelect value={granteeId} onValueChange={setGranteeId} departments={options.departments} />
        )}
        {granteeType === 'user' && (
          <Select value={granteeId} onValueChange={setGranteeId}>
            <SelectTrigger className="h-8 text-sm"><SelectValue placeholder="选择用户" /></SelectTrigger>
            <SelectContent>
              {options.users.map((u) => <SelectItem key={u.id} value={String(u.id)}>{u.name}</SelectItem>)}
            </SelectContent>
          </Select>
        )}
        {granteeType === 'agent' && (
          myAgents === null ? (
            <div className="py-1.5 text-xs text-muted-foreground"><Loader2 className="mr-1 inline h-3.5 w-3.5 animate-spin" />加载中…</div>
          ) : myAgents.length === 0 ? (
            <div className="py-1.5 text-xs text-muted-foreground">你还没有其他数字人，先去「数字人市场」领用一个</div>
          ) : (
            <Select value={granteeId} onValueChange={setGranteeId}>
              <SelectTrigger className="h-8 text-sm"><SelectValue placeholder="选择数字人" /></SelectTrigger>
              <SelectContent>
                {myAgents.map((a) => <SelectItem key={a.id} value={String(a.id)}>{a.name}</SelectItem>)}
              </SelectContent>
            </Select>
          )
        )}

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>关闭</Button>
          <Button onClick={create} disabled={busy || (granteeType !== 'all' && !granteeId)}>
            {granteeType === 'agent' ? (busy ? '转交中…' : '转交副本') : '创建共享'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function SharedWithMe({ agentId, preview, onPreview }) {
  const [shares, setShares] = useState([])
  const [expanded, setExpanded] = useState(null) // 展开的共享 id
  const [files, setFiles] = useState([])
  const [myAgents, setMyAgents] = useState([])

  const load = useCallback(() => {
    api('/shared').then(setShares).catch((e) => toast.error(e.message))
  }, [])
  useEffect(load, [load])
  useEffect(() => {
    api('/agents').then((w) => setMyAgents(w.my_instances || [])).catch(() => {}) // 修 M5 残留 bug：/my-agents/wall 不存在（422），另存下拉一直是空的
  }, [])

  const toggle = async (s) => {
    if (expanded === s.id) {
      setExpanded(null)
      return
    }
    setExpanded(s.id)
    try {
      setFiles(await api(`/shared/${s.id}/files`))
    } catch (e) {
      toast.error(e.message)
    }
  }

  const openSharedPreview = async (f) => {
    if (preview?.raw_url_override && preview?.id === f.id) {
      onPreview(null)
      return
    }
    try {
      const detail = await api(`/shared/files/${f.id}`)
      onPreview({ ...detail, raw_url_override: `/api/shared/files/${f.id}/raw` })
    } catch (e) {
      toast.error(e.message)
    }
  }

  const saveTo = async (f, target) => {
    try {
      const r = await api(`/shared/files/${f.id}/save`, { method: 'POST', body: { agent_id: target.id } })
      toast.success(`已另存到「${target.name}」：${r.filename}`)
      if (target.id === Number(agentId)) window.dispatchEvent(new Event('workspace-changed'))
    } catch (e) {
      toast.error(e.message)
    }
  }

  return (
    <div className="min-h-0 flex-1 overflow-y-auto">
      <div className="p-2">
        {shares.map((s) => (
          <div key={s.id} className="mb-1">
            <div
              className="flex cursor-pointer items-center gap-1.5 rounded-md px-2 py-1.5 text-sm hover:bg-accent/60"
              onClick={() => toggle(s)}
              title={`${s.owner_name} 共享的「${(s.paths || []).join('、') || '整个工作区'}」`}
            >
              {expanded === s.id ? <ChevronDown className="h-3.5 w-3.5 text-muted-foreground" /> : <ChevronRight className="h-3.5 w-3.5 text-muted-foreground" />}
              <Share2 className="h-4 w-4 shrink-0 text-muted-foreground" />
              <span className="min-w-0 flex-1 truncate font-medium">{pathsLabel(s.paths)}</span>
              <span className="shrink-0 text-xs text-muted-foreground">{s.owner_name} · {s.agent_name}</span>
            </div>
            {expanded === s.id && (
              <div className="ml-4">
                {files.map((f) => (
                  <div
                    key={f.id}
                    className={cn('group mb-0.5 flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-sm hover:bg-accent/60', preview?.raw_url_override && preview?.id === f.id && 'bg-accent')}
                    onClick={() => openSharedPreview(f)}
                    title={`${f.filename}\n点击预览（只读）`}
                  >
                    <FileText className="h-4 w-4 shrink-0 text-muted-foreground" />
                    <span className="min-w-0 flex-1 truncate">{f.filename.split('/').pop()}</span>
                    <button
                      className="shrink-0 rounded p-1 text-muted-foreground opacity-100 hover:bg-background hover:text-foreground md:opacity-0 md:group-hover:opacity-100"
                      title="下载"
                      onClick={(e) => { e.stopPropagation(); downloadRaw(`/api/shared/files/${f.id}/raw`, f.filename).catch((err) => toast.error(err.message || '下载失败')) }}
                    >
                      <Download className="h-3 w-3" />
                    </button>
                    <DropdownMenu>
                      <DropdownMenuTrigger asChild>
                        {/* 同 renderFile：原生 button 挂 ref */}
                        <button
                          className="shrink-0 rounded p-1 text-muted-foreground opacity-100 hover:bg-background hover:text-foreground focus:opacity-100 data-[state=open]:opacity-100 md:opacity-0 md:group-hover:opacity-100 md:focus:opacity-100 md:data-[state=open]:opacity-100"
                          title="另存到我的数字人"
                          onClick={(e) => e.stopPropagation()}
                        >
                          <Save className="h-3 w-3" />
                        </button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end" sideOffset={4} className="w-44" onClick={(e) => e.stopPropagation()}>
                        {myAgents.length === 0 && <DropdownMenuItem disabled>暂无可用数字人</DropdownMenuItem>}
                        {myAgents.map((a) => (
                          <DropdownMenuItem key={a.id} onClick={() => saveTo(f, a)}>
                            <Bot className="mr-2 h-3.5 w-3.5" /> {a.name}
                          </DropdownMenuItem>
                        ))}
                      </DropdownMenuContent>
                    </DropdownMenu>
                  </div>
                ))}
                {files.length === 0 && <div className="px-2 py-2 text-xs text-muted-foreground">该共享暂无可见文件</div>}
              </div>
            )}
          </div>
        ))}
        {shares.length === 0 && (
          <div className="px-2 py-8 text-center text-xs text-muted-foreground">暂无同事共享给你的内容</div>
        )}
      </div>
    </div>
  )
}


