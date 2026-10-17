import {html, parse} from 'diff2html';

// Only the library's escaped, built-in templates enter v-html. Raw patch text
// stays Vue text interpolation in the fallback/details and is never injected.
export function renderDiff(patch, mode = 'line-by-line', renderer = html) {
  const text = String(patch || '');
  if (!text.trim()) return {html: '', error: ''};
  try {
    const files = parse(text);
    if (!files.some(file => file.blocks.some(block => block.lines.length))) {
      throw new Error('No renderable diff lines');
    }
    const rendered = renderer(files, {
      outputFormat: mode === 'side-by-side' ? 'side-by-side' : 'line-by-line',
      drawFileList: false,
      colorScheme: 'dark',
      matching: 'lines',
      diffStyle: 'word',
      matchingMaxComparisons: 2500,
      maxLineLengthHighlight: 2000,
      renderNothingWhenEmpty: true,
    });
    if (!rendered) throw new Error('Empty rendered diff');
    return {html: rendered, error: ''};
  } catch {
    return {html: '', error: 'Не удалось построить визуальное сравнение. Ниже доступен исходный diff.'};
  }
}
