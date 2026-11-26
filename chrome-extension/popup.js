const $ = (id) => document.getElementById(id)
let tab

function send(message) {
  return new Promise((resolve) => chrome.runtime.sendMessage(message, resolve))
}

function status(message, bad = false) {
  $('status').textContent = message || ''
  $('status').className = bad ? 'bad' : 'ok'
}

async function load() {
  const response = await send({ type: 'popupState', tabId: tab.id })
  if (response?.error) { status(response.error, true); return }
  $('channel').replaceChildren(...response.channels.map((c) => new Option(`${c.number} · ${c.name}`, c.id)))
  $('server').value = response.server
  const linked = response.linked
  if (linked) $('channel').value = linked.channel
  if (response.config) $('collection').value = response.config.collection
  $('link').hidden = !!linked
  $('next').hidden = !linked
  $('unlink').hidden = !linked
  $('current').hidden = !response.config?.current
  $('current').textContent = response.config?.current ? `Now linked: ${response.config.current.title}` : ''
  if (linked) status(`Autoplay is on for channel ${response.channels.find((c) => c.id === linked.channel)?.number}.`)
}

async function run(button, message, done) {
  button.disabled = true
  status('Working…')
  const response = await send(message)
  button.disabled = false
  if (response?.error) { status(response.error, true); return }
  status(done)
  await load()
}

chrome.tabs.query({ active: true, currentWindow: true }, ([active]) => {
  tab = active
  let supported = false
  try { supported = new URL(tab.url).hostname === '7movies.ws' } catch { /* not a web page */ }
  if (!supported) {
    for (const id of ['channel', 'collection', 'link', 'server', 'saveServer']) $(id).disabled = true
    status('Open a 7Movies watch tab, then click MyTV Autoplay again.', true)
    return
  }
  load()
})

$('link').addEventListener('click', () => run($('link'), {
  type: 'link', tabId: tab.id, channel: +$('channel').value, collection: $('collection').value,
}, 'Tab linked. Keep sharing it on the Stream page.'))
$('next').addEventListener('click', () => run($('next'), { type: 'next', tabId: tab.id }, 'Next title selected.'))
$('unlink').addEventListener('click', () => run($('unlink'), { type: 'unlink', tabId: tab.id }, 'Autoplay disabled.'))
$('saveServer').addEventListener('click', () => run($('saveServer'), { type: 'setServer', server: $('server').value }, 'Server address saved.'))
