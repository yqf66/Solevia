export async function prepareAvatar(file: File): Promise<string> {
  if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type)) throw new Error('请选择 JPG、PNG 或 WebP 图片。')
  if (file.size > 10 * 1024 * 1024) throw new Error('图片请小于 10 MB。')
  const url = URL.createObjectURL(file)
  try {
    const image = new Image()
    image.src = url
    await image.decode()
    const canvas = document.createElement('canvas')
    canvas.width = 256; canvas.height = 256
    const context = canvas.getContext('2d')
    if (!context || !image.naturalWidth || !image.naturalHeight) throw new Error('图片无法读取，请换一张试试。')
    const size = Math.min(image.naturalWidth, image.naturalHeight)
    context.drawImage(image, (image.naturalWidth - size) / 2, (image.naturalHeight - size) / 2, size, size, 0, 0, 256, 256)
    return canvas.toDataURL('image/webp', .86)
  } catch { throw new Error('图片无法读取，请换一张完整的图片。') }
  finally { URL.revokeObjectURL(url) }
}
