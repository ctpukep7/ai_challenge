import test from 'node:test';
import assert from 'node:assert/strict';
import {renderDiff} from './diff.js';

// Python difflib.unified_diff format used by the backend, without diff --git.
const modified = '--- a/day-34/generated/guide.md\n+++ b/day-34/generated/guide.md\n@@ -1,3 +1,3 @@\n # Памятка\n-API возвращает старый ответ\n+API возвращает новый ответ\n Общая строка\n';

test('renders backend patch with line numbers, Cyrillic, red/green classes and inline differences', () => {
  const result = renderDiff(modified);
  assert.equal(result.error, '');
  for (const text of ['d2h-code-linenumber', 'd2h-ins', 'd2h-del', 'Памятка', 'Общая строка', '<del>стар</del>', '<ins>нов</ins>']) assert.ok(result.html.includes(text), text);
  assert.match(result.html, /line-num[12]">2</);
});

test('side-by-side mode produces two sides; invalid mode defaults to unified', () => {
  assert.match(renderDiff(modified, 'side-by-side').html, /d2h-files-diff/);
  assert.equal((renderDiff(modified, 'side-by-side').html.match(/class="d2h-file-side-diff"/g) || []).length, 2);
  assert.doesNotMatch(renderDiff(modified).html, /d2h-file-side-diff/);
  assert.equal(renderDiff(modified, '<script>').html, renderDiff(modified).html);
});

test('creation and deletion patches retain every changed line', () => {
  const created = renderDiff('--- a/day-34/generated/new.md\n+++ b/day-34/generated/new.md\n@@ -0,0 +1,2 @@\n+# Новый\n+Текст\n');
  const deleted = renderDiff('--- a/day-34/generated/old.md\n+++ b/day-34/generated/old.md\n@@ -1,2 +0,0 @@\n-# Старый\n-Текст\n');
  assert.equal(created.error, ''); assert.equal(deleted.error, '');
  assert.match(created.html, /Новый/); assert.match(created.html, /Текст/); assert.match(created.html, /d2h-ins/);
  assert.match(deleted.html, /Старый/); assert.match(deleted.html, /Текст/); assert.match(deleted.html, /d2h-del/);
});

test('HTML in content, hunk headers and filenames stays escaped in both modes', () => {
  const patch = '--- a/<img src=x onerror=alert(1)>.md\n+++ b/<script>alert(2)</script>.md\n@@ -1 +1 @@ <svg onload=alert(3)>\n-<img src=x onerror=alert(4)>\n+<script>alert(5)</script><a href="javascript:alert(6)">x</a>\n';
  for (const mode of ['line-by-line', 'side-by-side']) {
    const result = renderDiff(patch, mode);
    assert.equal(result.error, '');
    assert.doesNotMatch(result.html, /<(?:script|img|iframe|svg\s+onload|a\s)/i);
    assert.doesNotMatch(result.html, /<[^>]*\s(?:onerror|onload|onclick)\s*=/i);
    assert.match(result.html, /&lt;script&gt;/); assert.match(result.html, /&lt;img/);
  }
});

test('empty, malformed and renderer errors fall back safely', () => {
  assert.deepEqual(renderDiff(''), {html: '', error: ''});
  assert.ok(renderDiff('not a patch').error);
  assert.ok(renderDiff(modified, 'line-by-line', () => {throw new Error('render failed')}).error);
  assert.ok(renderDiff(modified, 'line-by-line', () => '').error);
});

test('long lines are preserved beyond word highlighting bound', () => {
  const longLine = 'x'.repeat(3000) + 'конец';
  assert.ok(renderDiff(`--- a/file.md\n+++ b/file.md\n@@ -1 +1 @@\n-old\n+${longLine}\n`).html.includes(longLine));
});

test('combined backend patch renders every file and final lines in both modes',()=>{
 const second='--- a/day-34/generated/second.md\n+++ b/day-34/generated/second.md\n@@ -0,0 +1,120 @@\n'+Array.from({length:120},(_,i)=>'+Строка второго файла '+(i+1)).join('\n')+'\n';
 for(const mode of ['line-by-line','side-by-side']){
  const result=renderDiff(modified+second,mode);
  assert.equal(result.error,'');assert.match(result.html,/guide.md/);assert.match(result.html,/second.md/);
  assert.match(result.html,/Строка второго файла 120/);
  assert.equal((result.html.match(/class="d2h-file-wrapper"/g)||[]).length,2);
 }
});
