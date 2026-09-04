import { onDeactivated, onScopeDispose, ref } from 'vue'

import { settingsApi } from '@/api/settings'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { useToast } from '@/composables/useToast'
import { writeClipboardText } from '@/lib/clipboard'
import { errorMessage } from '@/lib/errorMessage'

const REVEAL_TIMEOUT_MS = 120000

type SettingsAccountImportApiRuntimeOptions = {
  requireSavedSettings: (actionLabel: string) => boolean
  reloadSettings: () => Promise<void> | void
}

export function useSettingsAccountImportApiRuntime(
  options: SettingsAccountImportApiRuntimeOptions,
) {
  const revealedKey = ref('')
  const busy = ref<'reveal' | 'regenerate' | ''>('')
  const toast = useToast()
  const confirmDialog = useConfirmDialog()
  let revealClearTimer: ReturnType<typeof window.setTimeout> | null = null

  function clearRevealedKey() {
    if (revealClearTimer !== null) {
      window.clearTimeout(revealClearTimer)
      revealClearTimer = null
    }
    revealedKey.value = ''
  }

  function revealKey(apiKey: string) {
    clearRevealedKey()
    revealedKey.value = apiKey
    if (!apiKey) return
    revealClearTimer = window.setTimeout(clearRevealedKey, REVEAL_TIMEOUT_MS)
  }

  onDeactivated(clearRevealedKey)
  onScopeDispose(clearRevealedKey)

  async function showImportApiKey() {
    if (busy.value) return
    if (revealedKey.value) {
      clearRevealedKey()
      return
    }
    busy.value = 'reveal'
    try {
      const result = await settingsApi.getAccountImportApiKey()
      revealKey(result.api_key)
    } catch (error) {
      toast.error(errorMessage(error, '无法读取账号导入密钥'))
    } finally {
      busy.value = ''
    }
  }

  async function copyImportApiKey() {
    if (busy.value) return
    let apiKey = revealedKey.value
    if (!apiKey) {
      busy.value = 'reveal'
      try {
        apiKey = (await settingsApi.getAccountImportApiKey()).api_key
      } catch (error) {
        toast.error(errorMessage(error, '无法读取账号导入密钥'))
        return
      } finally {
        busy.value = ''
      }
    }
    try {
      await writeClipboardText(apiKey)
      toast.success('已复制账号导入密钥')
    } catch {
      toast.error('复制失败，请手动复制')
    }
  }

  async function regenerateImportApiKey() {
    if (busy.value) return
    if (!options.requireSavedSettings('重新生成账号导入密钥')) return
    const confirmed = await confirmDialog.ask({
      title: '确认重新生成账号导入密钥',
      message: '重新生成后旧密钥立即失效，所有正在使用旧密钥推送账号的外部系统都需要同步更新。是否继续？',
      confirmText: '重新生成',
      cancelText: '取消',
    })
    if (!confirmed) return

    busy.value = 'regenerate'
    try {
      const result = await settingsApi.regenerateAccountImportApiKey()
      revealKey(result.api_key)
      // 重新生成会改变 has_api_key，重新读取投影而不是在本地推断。
      await options.reloadSettings()
      toast.success('账号导入密钥已重新生成，请复制保存')
    } catch (error) {
      toast.error(errorMessage(error, '重新生成账号导入密钥失败'))
    } finally {
      busy.value = ''
    }
  }

  function invalidate() {
    clearRevealedKey()
  }

  return {
    importApiKeyRevealed: revealedKey,
    importApiKeyBusy: busy,
    showImportApiKey,
    copyImportApiKey,
    regenerateImportApiKey,
    invalidate,
  }
}
