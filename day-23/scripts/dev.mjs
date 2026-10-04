import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'

const python = process.env.DAY23_PYTHON || (existsSync('.venv/bin/python') ? '.venv/bin/python' : 'python3.12')
const children = [
  spawn(python, ['-m', 'uvicorn', 'backend.app:app', '--host', '127.0.0.1', '--port', '5022'], { stdio: 'inherit' }),
  spawn('npm', ['--prefix', 'frontend', 'run', 'dev'], { stdio: 'inherit' }),
]
let closing = false
function stop(code = 0) {
  if (closing) return
  closing = true
  for (const child of children) child.kill('SIGTERM')
  process.exitCode = code
}
for (const child of children) {
  child.on('error', error => { console.error(error.message); stop(1) })
  child.on('exit', code => stop(code || 0))
}
process.on('SIGINT', () => stop())
process.on('SIGTERM', () => stop())
