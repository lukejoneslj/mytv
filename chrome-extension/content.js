;(() => {
  const top = window.top === window
  const origins = top ? [location.origin] : Array.from(location.ancestorOrigins || [])
  const mine = origins.some((origin) => {
    try { return new URL(origin).hostname === '7movies.ws' } catch { return false }
  })
  if (!mine) return

  let enabled = false
  let autoplay = false
  let advancing = false

  function title() {
    const name = document.querySelector('.wp-head-copy h1, h1')?.textContent?.trim()
    const episode = document.querySelector('.wp-head-copy .wp-ep')?.textContent?.trim()
    return [name, episode].filter(Boolean).join(' · ') || document.title.replace(/\s*[—-]\s*7Movies$/, '')
  }

  function ready() {
    chrome.runtime.sendMessage({ type: 'ready', title: top ? title() : '', url: top ? location.href : '' }, (config) => {
      if (chrome.runtime.lastError || !config) return
      enabled = !!config.enabled
      autoplay = !!config.autoplay
      if (top) theater()
      attempt(document.querySelector('video'))
    })
  }

  function theater() {
    let style = document.getElementById('mytv-autoplay-style')
    if (!enabled) { style?.remove(); return }
    if (!style) {
      style = document.createElement('style')
      style.id = 'mytv-autoplay-style'
      ;(document.head || document.documentElement).appendChild(style)
    }
    style.textContent = `
      html, body { background: #000 !important; overflow: hidden !important; }
      .wp-stage { position: fixed !important; inset: 0 !important; z-index: 2147483646 !important;
        width: 100vw !important; height: 100vh !important; max-width: none !important; margin: 0 !important; background: #000 !important; }
      .wp-stage iframe { position: absolute !important; inset: 0 !important; width: 100% !important; height: 100% !important;
        max-width: none !important; max-height: none !important; aspect-ratio: auto !important; border: 0 !important; }
    `
  }

  function attempt(video) {
    if (!enabled || !autoplay || !video || video.duration < 180 || !video.paused) return
    video.play().catch(() => chrome.runtime.sendMessage({ type: 'blocked' }))
  }

  document.addEventListener('loadedmetadata', (event) => attempt(event.target), true)
  document.addEventListener('canplay', (event) => attempt(event.target), true)
  document.addEventListener('playing', (event) => {
    if (event.target?.duration >= 180) chrome.runtime.sendMessage({ type: 'playing' })
  }, true)
  document.addEventListener('ended', (event) => {
    const video = event.target
    if (!enabled || advancing || !video || video.duration < 180) return
    advancing = true
    chrome.runtime.sendMessage({ type: 'ended' }, (response) => {
      if (chrome.runtime.lastError || response?.error) advancing = false
    })
  }, true)

  chrome.runtime.onMessage.addListener((message) => {
    if (message.type === 'configure') {
      enabled = !!message.config.enabled
      autoplay = !!message.config.autoplay
      theater()
    }
    if (message.type === 'notice' && top) {
      let note = document.getElementById('mytv-autoplay-note')
      if (!note) {
        note = document.createElement('div')
        note.id = 'mytv-autoplay-note'
        note.style.cssText = 'position:fixed;left:50%;bottom:24px;transform:translateX(-50%);z-index:2147483647;background:#f3efe6;color:#14130f;padding:12px 18px;border-radius:6px;font:15px sans-serif;max-width:80vw;text-align:center'
        ;(document.body || document.documentElement).appendChild(note)
        setTimeout(() => note.remove(), 12000)
      }
      note.textContent = message.message
    }
  })

  ready()
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', ready, { once: true })
  else ready()
  setTimeout(ready, 1500)
})()
