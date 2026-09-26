import MarkdownIt from 'markdown-it'

const markdown = new MarkdownIt({ html: false, linkify: false, typographer: false })
const defaultValidateLink = markdown.validateLink.bind(markdown)
markdown.validateLink = url => defaultValidateLink(url) && (
  !/^[a-z][a-z0-9+.-]*:/i.test(url) || /^(?:https?|mailto):/i.test(url)
)

const defaultLinkOpen = markdown.renderer.rules.link_open
markdown.renderer.rules.link_open = (tokens, index, options, env, renderer) => {
  const token = tokens[index]
  const href = token.attrGet('href') || ''
  if (/^(?:https?:)?\/\//i.test(href)) {
    token.attrSet('target', '_blank')
    token.attrSet('rel', 'noopener noreferrer')
  }
  return defaultLinkOpen
    ? defaultLinkOpen(tokens, index, options, env, renderer)
    : renderer.renderToken(tokens, index, options)
}
// Markdown images remain text: rendering a chat message never contacts an image host.
markdown.renderer.rules.image = (tokens, index) => {
  const label = tokens[index].content || 'без подписи'
  return `<span class="markdown-image-note">[Изображение: ${markdown.utils.escapeHtml(label)}]</span>`
}

export function renderMarkdown(content) {
  return markdown.render(typeof content === 'string' ? content : '')
}
