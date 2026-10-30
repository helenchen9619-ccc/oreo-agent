// 全局 promise 式弹窗：confirmDialog / promptDialog，替代丑陋的 window.confirm / window.prompt。
// 用法：
//   if (!(await confirmDialog({ title: '删除对话', desc: '确定删除…？', danger: true }))) return
//   const name = await promptDialog({ title: '重命名', defaultValue: old, placeholder: '…' })  // null = 取消
// 需在根节点挂一次 <DialogHost />（main.jsx 已挂）。
import { useEffect, useRef, useState } from 'react'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'

let _open = null // DialogHost 挂载后注册

export function confirmDialog(opts) {
  return new Promise((resolve) => _open?.({ kind: 'confirm', ...opts, resolve }))
}

export function promptDialog(opts) {
  return new Promise((resolve) => _open?.({ kind: 'prompt', ...opts, resolve }))
}

export function DialogHost() {
  const [req, setReq] = useState(null)
  const [text, setText] = useState('')
  const inputRef = useRef(null)

  useEffect(() => {
    _open = (r) => {
      setText(r.defaultValue ?? '')
      setReq(r)
    }
    return () => { _open = null }
  }, [])

  const close = (value) => {
    req?.resolve(value)
    setReq(null)
  }

  return (
    <Dialog open={!!req} onOpenChange={(v) => { if (!v) close(req?.kind === 'prompt' ? null : false) }}>
      {req && (
        <DialogContent className="sm:max-w-md" onOpenAutoFocus={(e) => {
          // prompt 弹窗把焦点给输入框而不是默认的第一个按钮
          if (req.kind === 'prompt') {
            e.preventDefault()
            inputRef.current?.focus()
            inputRef.current?.select()
          }
        }}>
          <DialogHeader>
            <DialogTitle>{req.title}</DialogTitle>
            {req.desc && <DialogDescription>{req.desc}</DialogDescription>}
          </DialogHeader>
          {req.kind === 'prompt' && (
            <form
              onSubmit={(e) => {
                e.preventDefault()
                const v = text.trim()
                if (v || req.allowEmpty) close(v)  // allowEmpty：允许空值提交（如"留空=根目录"场景）
              }}
            >
              <Input
                ref={inputRef}
                value={text}
                onChange={(e) => setText(e.target.value)}
                placeholder={req.placeholder}
                maxLength={req.maxLength ?? 200}
              />
            </form>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => close(req.kind === 'prompt' ? null : false)}>
              {req.cancelText ?? '取消'}
            </Button>
            <Button
              variant={req.danger ? 'destructive' : 'default'}
              disabled={req.kind === 'prompt' && !text.trim() && !req.allowEmpty}
              onClick={() => close(req.kind === 'prompt' ? text.trim() : true)}
            >
              {req.confirmText ?? '确定'}
            </Button>
          </DialogFooter>
        </DialogContent>
      )}
    </Dialog>
  )
}
