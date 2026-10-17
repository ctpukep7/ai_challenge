import test from 'node:test';
import assert from 'node:assert/strict';
import {renderMarkdown} from './markdown.js';

test('renders headings, lists, code, quotations and tables',()=>{
 const html=renderMarkdown('# Итог\n\n- **Файл** и `код`\n\n> Цитата\n\n```js\nconst x=1;\n```\n\n| Файл | Роль |\n| --- | --- |\n| app | API |');
 for(const tag of ['<h1>','<ul>','<strong>','<code>','<blockquote>','<pre>','<table>'])assert.ok(html.includes(tag),tag);
});
test('raw HTML is escaped without executable elements',()=>{
 const html=renderMarkdown('<script>alert(1)</script>\n<img src=x onerror=alert(1)>');
 assert.ok(html.includes('&lt;script&gt;'));assert.doesNotMatch(html,/<script|<img|<iframe/i);
});
test('only http, https and fragment links are active',()=>{
 const html=renderMarkdown('[web](https://example.com) [anchor](#part) [relative](../private) [file](file:///etc/passwd) [js](javascript:alert%281%29) [data](data:text/html,abc) [vb](vbscript:msgbox) [encoded](%6aavascript:alert) [entity](jav&#x61;script:alert)');
 assert.equal((html.match(/<a /g)||[]).length,2);assert.ok(html.includes('rel="noopener noreferrer"'));assert.ok(html.includes('href="#part"'));
});
test('images render escaped descriptions without fetching remote URLs',()=>{
 const html=renderMarkdown('![Картинка](https://tracker.example/pixel) ![<img onerror=x>](https://example.com/a.png)');
 assert.doesNotMatch(html,/<img|src=|tracker\.example/);assert.ok(html.includes('Картинка'));assert.ok(html.includes('&lt;img onerror=x&gt;'));
});
