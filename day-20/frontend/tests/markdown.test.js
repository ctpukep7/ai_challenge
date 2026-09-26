import test from 'node:test'
import assert from 'node:assert/strict'
import { renderMarkdown } from '../src/markdown.js'

test('renders headings, emphasis, paragraphs, lists and quotes', () => {
  const html = renderMarkdown('# План\n\n**Омск** и *поездка*.\n\n- Погода\n- Календарь\n\n> Свободное время')
  for (const expected of ['<h1>План</h1>', '<strong>Омск</strong>', '<em>поездка</em>', '<ul>', '<li>Погода</li>', '<blockquote>']) assert.ok(html.includes(expected))
})

test('renders tables and escapes fenced and inline code', () => {
  const html = renderMarkdown('| День | Погода |\n| --- | --- |\n| Завтра | +10 °C |\n\n```json\n{"text": "<script>alert(1)</script>"}\n```\n\n`<img src=x>`')
  assert.ok(html.includes('<table>'))
  assert.ok(html.includes('class="language-json"'))
  assert.ok(html.includes('&lt;script&gt;'))
  assert.ok(html.includes('<code>&lt;img src=x&gt;</code>'))
  assert.ok(!html.includes('<script>'))
})

test('raw HTML and event handlers are plain escaped text', () => {
  const html = renderMarkdown('<script>alert(1)</script>\n\n<img src="x" onerror="alert(1)">')
  assert.ok(html.includes('&lt;script&gt;'))
  assert.ok(!html.includes('<script'))
  assert.ok(!html.includes('<img'))
})

test('unsafe link schemes never become clickable links', () => {
  for (const url of ['javascript:alert(1)', 'JaVaScRiPt:alert(1)', 'vbscript:alert(1)', 'file:///etc/passwd', 'data:text/html;base64,AAAA', 'data:image/png;base64,AAAA', 'blob:https://example.com/id', 'filesystem:https://example.com/temporary/x', 'shell:run']) {
    assert.ok(!renderMarkdown(`[опасная ссылка](${url})`).includes('<a '), url)
  }
})

test('external links open safely while anchors stay on the current page', () => {
  const html = renderMarkdown('[Источник](https://example.com/a) [Раздел](#section)')
  assert.ok(html.includes('href="https://example.com/a" target="_blank" rel="noopener noreferrer"'))
  assert.ok(html.includes('<a href="#section">Раздел</a>'))
})

test('images including data URLs never load automatically', () => {
  const html = renderMarkdown('![Город <img>](https://example.com/private.png) ![data](data:image/png;base64,AAAA)')
  assert.ok(!html.includes('<img'))
  assert.ok(!html.includes('src='))
  assert.ok(html.includes('[Изображение: Город &lt;img&gt;]'))
})

test('ordinary Markdown line wrapping and blank paragraphs remain readable', () => {
  assert.equal(renderMarkdown('Одна строка\nПродолжение\n\nНовый абзац'), '<p>Одна строка\nПродолжение</p>\n<p>Новый абзац</p>\n')
  assert.equal(renderMarkdown(null), '')
})
