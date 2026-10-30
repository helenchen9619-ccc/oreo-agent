// 部门相关共用组件/工具（M3.12）：
//   useDepartments()      —— 拉取部门列表（扁平，含 parent_id/user_count）
//   deptTree(depts)       —— 扁平列表 → 树序数组（带 depth，供缩进展示）
//   <DepartmentSelect />  —— 部门下拉（树形缩进）；value 为字符串 id，'none' 表示无部门
import { useCallback, useEffect, useState } from 'react'
import { toast } from 'sonner'
import { api } from '@/api'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'

export function useDepartments() {
  const [depts, setDepts] = useState([])
  const reload = useCallback(() => {
    api('/admin/departments').then(setDepts).catch((e) => toast.error(e.message))
  }, [])
  useEffect(reload, [reload])
  return [depts, reload]
}

/** 扁平部门列表 → 树序（先序遍历）数组，每项带 depth。父级缺失的孤儿挂到末尾，不丢数据。 */
export function deptTree(depts) {
  const byParent = new Map()
  for (const d of depts) {
    const key = d.parent_id ?? 0
    if (!byParent.has(key)) byParent.set(key, [])
    byParent.get(key).push(d)
  }
  const out = []
  const walk = (pid, depth) => {
    for (const d of byParent.get(pid) || []) {
      out.push({ ...d, depth })
      walk(d.id, depth + 1)
    }
  }
  walk(0, 0)
  const seen = new Set(out.map((d) => d.id))
  for (const d of depts) if (!seen.has(d.id)) out.push({ ...d, depth: 0 })
  return out
}

/** 树形缩进的部门显示名（全角空格缩进，子级加 └） */
export function deptLabel(d) {
  return '　'.repeat(d.depth) + (d.depth > 0 ? '└ ' : '') + d.name
}

/**
 * 部门下拉。value/onValueChange 用字符串 id；allowEmpty 时首项为"无部门"（值 'none'）。
 * 可通过 departments 传入已拉好的列表（带 depth 或扁平均可），否则自行拉取。
 */
export default function DepartmentSelect({ value, onValueChange, placeholder = '选择部门', className, allowEmpty = false, departments, excludeId }) {
  const [fetched, reload] = useDepartments()
  const raw = departments ?? fetched
  const tree = deptTree(raw).filter((d) => d.id !== excludeId)

  return (
    <Select value={value || (allowEmpty ? 'none' : undefined)} onValueChange={onValueChange}>
      <SelectTrigger className={className}><SelectValue placeholder={placeholder} /></SelectTrigger>
      <SelectContent>
        {allowEmpty && <SelectItem value="none">（无部门）</SelectItem>}
        {tree.map((d) => (
          <SelectItem key={d.id} value={String(d.id)}>{deptLabel(d)}</SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}
