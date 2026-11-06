import { ref } from 'vue'

import { settingsApi, type ImageStorageTestResult } from '@/api/settings'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { useOperationProgressRuntime } from '@/composables/useOperationProgressRuntime'
import { useToast } from '@/composables/useToast'
import { errorMessage } from '@/lib/errorMessage'
import { formatBytes } from '@/views/settings/settingsView'

type SettingsImageStorageRuntimeOptions = {
  requireSavedSettings: (actionLabel: string) => boolean
}

export function useSettingsImageStorageRuntime(options: SettingsImageStorageRuntimeOptions) {
  const imageStorageBusy = ref('')
  const imageStorageTestResult = ref<ImageStorageTestResult | null>(null)
  const toast = useToast()
  const confirmDialog = useConfirmDialog()
  const progressRuntime = useOperationProgressRuntime()
  const operationProgress = progressRuntime.state

  async function testImageStorageConnection() {
    if (!options.requireSavedSettings('测试存储连接')) return
    const confirmed = await confirmDialog.ask({
      title: '确认测试远程存储',
      message: '即将使用已保存的图片存储配置发起远程存储连接测试，可能访问外部存储服务。是否继续？',
      confirmText: '开始测试',
      cancelText: '取消',
    })
    if (!confirmed) return

    imageStorageBusy.value = 'test'
    imageStorageTestResult.value = null
    try {
      const response = await settingsApi.testImageStorage()
      imageStorageTestResult.value = response.result
      if (response.result.ok) toast.success('远程存储测试通过')
      else toast.warning(response.result.error || '远程存储测试失败')
    } catch (error) {
      const message = errorMessage(error, '远程存储测试失败')
      imageStorageTestResult.value = { ok: false, error: message }
      toast.error(message)
    } finally {
      imageStorageBusy.value = ''
    }
  }

  async function syncImageStorageFiles() {
    if (!options.requireSavedSettings('同步本地图片')) return
    const confirmed = await confirmDialog.ask({
      title: '确认同步图片存储',
      message: '即将扫描本地图片并同步到已配置的远程存储，可能产生外部上传流量。是否继续？',
      confirmText: '开始同步',
      cancelText: '取消',
    })
    if (!confirmed) return

    imageStorageBusy.value = 'sync'
    await progressRuntime.start({
      title: '全量同步图片',
      subtitle: '本地 → 远程存储',
      message: '正在扫描并上传本地图片...',
    })
    try {
      const response = await settingsApi.syncImageStorage()
      const summary = `上传 ${response.result.uploaded}，跳过 ${response.result.skipped}，失败 ${response.result.failed}`
      if (response.result.failed > 0) progressRuntime.warn(summary)
      else progressRuntime.succeed(summary)
    } catch (error) {
      progressRuntime.fail(errorMessage(error, '同步图片失败'))
    } finally {
      imageStorageBusy.value = ''
    }
  }

  // 打开"到期同时删除 R2"前统计现有过期图片，有 R2 副本时必须经用户确认，返回是否允许打开。
  async function confirmDeleteExpiredRemote(imageRetentionHours: number): Promise<boolean> {
    let preview
    try {
      preview = await settingsApi.previewImageRemoteCleanup({ image_retention_hours: imageRetentionHours })
    } catch (error) {
      toast.warning(`过期图片检测失败：${errorMessage(error, '无法统计 R2 上的过期图片')}，未开启`)
      return false
    }
    if (!preview.remote_removed) return true
    return confirmDialog.ask({
      title: '确认开启 R2 过期删除',
      message: [
        `按当前保留期（${preview.retention_hours} 小时），已有 ${preview.removed} 张过期图片，其中 ${preview.remote_removed} 张在 Cloudflare R2 上（合计约 ${formatBytes(preview.removed_size_bytes)}）。`,
        '开启并保存后，将立即永久删除这些图片及其 R2 副本，此操作不可恢复，之后每次自动清理也会同步删除 R2。',
        '是否开启？',
      ].join('\n'),
      confirmText: '开启',
      cancelText: '取消',
    })
  }

  return {
    confirmDeleteExpiredRemote,
    imageStorageBusy,
    imageStorageTestResult,
    operationProgress,
    closeOperationProgress: progressRuntime.close,
    testImageStorageConnection,
    syncImageStorageFiles,
  }
}
