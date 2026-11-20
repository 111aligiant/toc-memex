/**
 * 剪贴板写入。
 *
 * navigator.clipboard 只在安全上下文（https / localhost）可用，而且可能
 * 因为权限被拒、或在不支持它的宿主 WebView 里直接抛错。失败时退回
 * execCommand('copy')：先把文本放进一个屏幕外的 textarea，选中后复制。
 *
 * 返回值是"是否真的写进去了"，调用方据此决定弹成功还是失败提示——
 * 不能无条件报成功。
 */
export async function copyText(text) {
  const value = text == null ? '' : String(text)
  if (!value) return false

  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(value)
      return true
    } catch {
      /* 落到下面的兜底 */
    }
  }

  // 兜底：临时 textarea + 选区 + execCommand
  try {
    const area = document.createElement('textarea')
    area.value = value
    area.setAttribute('readonly', '')
    area.style.position = 'fixed'
    area.style.top = '-9999px'
    area.style.opacity = '0'
    document.body.appendChild(area)
    area.select()
    area.setSelectionRange(0, value.length)
    const ok = document.execCommand('copy')
    document.body.removeChild(area)
    return ok
  } catch {
    return false
  }
}
