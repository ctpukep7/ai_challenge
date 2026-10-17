import {spawn} from 'node:child_process';
const children=[spawn('../.assistant-venv/bin/python',['-m','uvicorn','backend.app:app','--host','127.0.0.1','--port','5032'],{stdio:'inherit'}),spawn('npm',['--prefix','frontend','run','dev'],{stdio:'inherit'})];
let stopping=false; function stop(code=0){if(stopping)return;stopping=true;for(const p of children)p.kill('SIGTERM');setTimeout(()=>process.exit(code),300).unref()}
for(const p of children){p.on('error',()=>stop(1));p.on('exit',c=>stop(c??1))} process.on('SIGINT',()=>stop());process.on('SIGTERM',()=>stop());
