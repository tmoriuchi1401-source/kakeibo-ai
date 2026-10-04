// Offline integration bridge: exactly the native UI capture reaches Python.
const fs=require('node:fs'),vm=require('node:vm');
const ctx=vm.createContext({JSON,Date,Error,Number,String});
vm.runInContext(fs.readFileSync(__dirname+'/CategoryConfirmation.gs','utf8'),ctx);
const input=JSON.parse(fs.readFileSync(0,'utf8'));
process.stdout.write(JSON.stringify(ctx.ccCapture_(input.rows,input.key,input.category,input.stage,input.state)));
