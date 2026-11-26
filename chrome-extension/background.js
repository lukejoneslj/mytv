const DEFAULT_SERVER = 'http://localhost:8400'
const tabKey = (tabId) => `tab-${tabId}`
const advancing = new Set()

async function server() {
  const values = await chrome.storage.local.get('server')
  return (values.server || DEFAULT_SERVER).replace(/\/$/, '')
}

async function api(path, method = 'GET', body) {
  const response = await fetch(`${await server()}${path}`, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(data.detail || `MyTV returned HTTP ${response.status}`)
  return data
}

async function assignment(tabId) {
  const values = await chrome.storage.local.get(tabKey(tabId))
  return values[tabKey(tabId)] || null
}

async function saveAssignment(tabId, value) {
  if (value) await chrome.storage.local.set({ [tabKey(tabId)]: value })
  else await chrome.storage.local.remove(tabKey(tabId))
}

async function configure(tabId) {
  const linked = await assignment(tabId)
  if (!linked) return { enabled: false, autoplay: false }
  const config = await api(`/api/cast/${linked.channel}/autoplay`)
  return { enabled: config.enabled, autoplay: !!linked.pending, channel: linked.channel }
}

async function advance(tabId) {
  if (advancing.has(tabId)) throw new Error('Already choosing the next title')
  const linked = await assignment(tabId)
  if (!linked) throw new Error('Link this tab to a live channel first')
  advancing.add(tabId)
  try {
    const config = await api(`/api/cast/${linked.channel}/autoplay`)
    if (!config.enabled) throw new Error('Autoplay is off for this channel')
    const next = await api(`/api/cast/${linked.channel}/autoplay/next`, 'POST', {})
    await saveAssignment(tabId, { ...linked, pending: true })
    await chrome.tabs.update(tabId, { url: next.url })
    return next
  } finally {
    advancing.delete(tabId)
  }
}

async function notice(tabId, message) {
  try { await chrome.tabs.sendMessage(tabId, { type: 'notice', message }) } catch { /* the old page has gone */ }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  const tabId = message.tabId ?? sender.tab?.id
  const work = (async () => {
    if (message.type === 'ready') {
      const config = await configure(tabId)
      if (sender.frameId === 0 && config.enabled) {
        await api(`/api/cast/${config.channel}/autoplay/state`, 'POST', { title: message.title || '', url: message.url })
      }
      return config
    }
    if (message.type === 'ended') {
      const next = await advance(tabId)
      return { ok: true, next }
    }
    if (message.type === 'playing') {
      const linked = await assignment(tabId)
      if (linked?.pending) await saveAssignment(tabId, { ...linked, pending: false })
      return { ok: true }
    }
    if (message.type === 'blocked') {
      await notice(tabId, 'The next title is ready. Click its play button once; Chrome blocked automatic playback.')
      return { ok: true }
    }
    if (message.type === 'popupState') {
      const [channels, linked, address] = await Promise.all([api('/api/cast'), assignment(tabId), server()])
      const config = linked ? await api(`/api/cast/${linked.channel}/autoplay`) : null
      return { channels, linked, config, server: address }
    }
    if (message.type === 'setServer') {
      const url = new URL(message.server)
      if (!['http:', 'https:'].includes(url.protocol)) throw new Error('Use an http:// or https:// address')
      await chrome.storage.local.set({ server: url.origin })
      return { ok: true }
    }
    if (message.type === 'link') {
      const config = await api(`/api/cast/${message.channel}/autoplay`, 'PATCH', { enabled: true, collection: message.collection })
      await saveAssignment(tabId, { channel: message.channel, pending: false })
      await chrome.tabs.sendMessage(tabId, { type: 'configure', config: { enabled: true, autoplay: false } }).catch(() => {})
      return config
    }
    if (message.type === 'unlink') {
      const linked = await assignment(tabId)
      if (linked) await api(`/api/cast/${linked.channel}/autoplay`, 'PATCH', { enabled: false })
      await saveAssignment(tabId, null)
      await chrome.tabs.sendMessage(tabId, { type: 'configure', config: { enabled: false, autoplay: false } }).catch(() => {})
      return { ok: true }
    }
    if (message.type === 'next') return advance(tabId)
    throw new Error('Unknown request')
  })()
  work.then(sendResponse).catch(async (error) => {
    if (message.type === 'ended') await notice(tabId, `Autoplay: ${error.message}`)
    sendResponse({ error: error.message })
  })
  return true
})

chrome.tabs.onRemoved.addListener((tabId) => { saveAssignment(tabId, null) })
