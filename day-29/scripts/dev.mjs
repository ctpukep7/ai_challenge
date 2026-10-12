import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const production = process.argv.includes('--production');
const children = new Set();
let stopping = false;

function getJSON(port, route, timeout = 1500) {
  return new Promise((resolve, reject) => {
    const request = http.get({ hostname: '127.0.0.1', port, path: route }, response => {
      let body = '';
      response.setEncoding('utf8');
      response.on('data', chunk => {
        body += chunk;
        if (body.length > 65536) request.destroy(new Error('Слишком большой ответ сервера.'));
      });
      response.on('end', () => {
        try {
          if (response.statusCode !== 200) throw new Error('Сервер вернул ошибку.');
          resolve(JSON.parse(body));
        } catch (error) { reject(error); }
      });
      response.on('error', reject);
    });
    request.setTimeout(timeout, () => request.destroy(new Error('Сервер не ответил.')));
    request.on('error', reject);
  });
}

function ensureFree(port) {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', () => reject(new Error(`Порт ${port} занят. Завершите его приложение или используйте уже открытый чат.`)));
    server.listen({ host: '127.0.0.1', port, exclusive: true }, () => server.close(resolve));
  });
}

function start(command, args, label, cwd = root) {
  if (stopping) throw new Error('Запуск остановлен.');
  const child = spawn(command, args, { cwd, stdio: 'inherit', detached: process.platform !== 'win32' });
  child.label = label;
  children.add(child);
  child.once('error', error => {
    console.error(`Не удалось запустить ${label}: ${error.message}`);
    void stop(1);
  });
  child.once('exit', (code, signal) => {
    if (!stopping) {
      console.error(`${label} завершился (${signal || code}). Останавливаем собственные процессы.`);
      void stop(code === 0 ? 0 : 1);
    }
  });
  return child;
}

function signalChild(child, signal) {
  if (!child.pid) return;
  try {
    if (process.platform === 'win32') {
      if (child.exitCode === null && child.signalCode === null) child.kill(signal);
    }
    else process.kill(-child.pid, signal);
  } catch (error) {
    if (error.code !== 'ESRCH') console.error(`Не удалось завершить ${child.label}: ${error.message}`);
  }
}

async function stop(code = 0) {
  if (stopping) return;
  stopping = true;
  const owned = [...children];
  const exited = Promise.all(owned.map(child => new Promise(resolve => {
    if (child.exitCode !== null || child.signalCode !== null) resolve();
    else { child.once('exit', resolve); child.once('error', resolve); }
  })));
  for (const child of owned) signalChild(child, 'SIGTERM');
  let timer;
  await Promise.race([exited, new Promise(resolve => { timer = setTimeout(resolve, 5000); })]);
  clearTimeout(timer);
  for (const child of owned) signalChild(child, 'SIGKILL');
  process.exitCode = code;
}

process.on('SIGINT', () => void stop());
process.on('SIGTERM', () => void stop());

async function ensureOllama() {
  try {
    const version = await getJSON(11434, '/api/version');
    if (typeof version.version !== 'string' || !version.version) throw new Error('Некорректная версия.');
    console.log(`Используем работающий Ollama ${version.version}; при остановке чата он останется запущен.`);
    return;
  } catch {
    await ensureFree(11434);
  }
  const script = path.resolve(root, '../day-26/run-server.sh');
  if (!existsSync(script)) throw new Error('Не найден сервер Day26. Сначала установите локальную модель по README Day26.');
  const child = start('/bin/bash', [script], 'Ollama', path.dirname(script));
  console.log('Явно запускаем локальный Ollama из Day26. Новые веса не скачиваются.');
  const deadline = Date.now() + 30000;
  while (!stopping && Date.now() < deadline) {
    if (child.exitCode !== null || child.signalCode !== null) throw new Error('Ollama завершился до готовности.');
    try {
      const result = await getJSON(11434, '/api/version', 1000);
      if (typeof result.version === 'string') return;
    } catch { /* Startup only: generation requests never retry. */ }
    await new Promise(resolve => setTimeout(resolve, 200));
  }
  throw new Error('Ollama не запустился за 30 секунд. Проверьте установку Day26.');
}

async function main() {
  const python = path.resolve(root, '../day-21/.venv/bin/python');
  const vite = path.join(root, 'frontend/node_modules/vite/bin/vite.js');
  if (!existsSync(python)) throw new Error('Не установлены Python-зависимости: выполните команды из README Day29.');
  if (production && !existsSync(path.join(root, 'frontend/dist/index.html'))) throw new Error('Сначала соберите интерфейс: npm run build.');
  if (!production && !existsSync(vite)) throw new Error('Не установлены frontend-зависимости: создайте symlink frontend/node_modules на ../day-27/frontend/node_modules.');
  await ensureFree(5028);
  if (!production) await ensureFree(5195);
  if (stopping) return;
  await ensureOllama();
  if (stopping) return;
  start(python, ['-m', 'uvicorn', 'backend.app:app', '--host', '127.0.0.1', '--port', '5028'], 'API');
  if (!production) start(process.execPath, [vite, '--host', '127.0.0.1', '--port', '5195', '--strictPort'], 'Vite', path.join(root, 'frontend'));
  console.log(`\nЛокальный чат: http://127.0.0.1:${production ? 5028 : 5195}\nCtrl+C завершает только процессы этого запуска.\n`);
}

try { await main(); }
catch (error) {
  if (!stopping) console.error(error.message);
  await stop(1);
}
