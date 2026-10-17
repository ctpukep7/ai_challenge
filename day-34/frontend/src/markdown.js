import MarkdownIt from 'markdown-it';

const markdown = new MarkdownIt({html: false, linkify: false, breaks: true});
// An explicit allowlist also rejects relative, encoded and local-file URLs.
markdown.validateLink = url => /^(?:https?:\/\/|#)/i.test(url) && !/[\u0000-\u0020\u007f]/.test(url);
const linkOpen = markdown.renderer.rules.link_open || ((tokens, index, options, env, self) => self.renderToken(tokens, index, options));
markdown.renderer.rules.link_open = (tokens, index, options, env, self) => {
  if (/^https?:\/\//i.test(tokens[index].attrGet('href') || '')) {
    tokens[index].attrSet('target', '_blank');
    tokens[index].attrSet('rel', 'noopener noreferrer');
  }
  return linkOpen(tokens, index, options, env, self);
};
// Model-supplied images must never trigger remote requests or tracking.
markdown.renderer.rules.image = (tokens, index) => markdown.utils.escapeHtml(tokens[index].content);
export function renderMarkdown(text) {return markdown.render(String(text || ''));}
