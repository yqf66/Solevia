import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { pipeline } from 'node:stream';

const root = path.resolve(process.argv[2]);
const port = Number(process.argv[3]);
const backend = Number(process.argv[4]);
const mime = {'.html':'text/html; charset=utf-8','.js':'text/javascript; charset=utf-8','.css':'text/css; charset=utf-8','.svg':'image/svg+xml','.png':'image/png','.webp':'image/webp','.ico':'image/x-icon','.woff2':'font/woff2','.json':'application/json'};
const server = http.createServer((req,res) => {
  // Check before proxying: replacing Host upstream must never hide rebinding.
  const hosts = new Set([`127.0.0.1:${port}`, `localhost:${port}`]);
  const origins = new Set([...hosts].map(host => `http://${host}`));
  const sensitive = ['host','origin','x-solevia-token'];
  const counts = {};
  for(let i=0;i<req.rawHeaders.length;i+=2){const key=req.rawHeaders[i].toLowerCase();counts[key]=(counts[key]||0)+1;}
  if (!hosts.has(req.headers.host) || sensitive.some(key => counts[key]>1)) {res.writeHead(400);res.end('Invalid Host');return;}
  if ((req.headers.origin !== undefined && !origins.has(req.headers.origin)) ||
      (req.headers['sec-fetch-site'] && !['same-origin','none'].includes(req.headers['sec-fetch-site']))) {
    res.writeHead(403);res.end('Untrusted request');return;
  }
  res.setHeader('Cross-Origin-Resource-Policy','same-origin');
  res.setHeader('X-Frame-Options','DENY');
  res.setHeader('Referrer-Policy','no-referrer');
  if (req.url === '/_solevia/health') {res.writeHead(200,{'Content-Type':'application/json'});res.end('{"service":"solevia-frontend"}');return;}
  if (req.url === '/api' || req.url.startsWith('/api/')) {
    const upstream = http.request({hostname:'127.0.0.1',port:backend,path:req.url,method:req.method,headers:{...req.headers,host:`127.0.0.1:${backend}`}}, response => {
      res.writeHead(response.statusCode,response.headers);pipeline(response,res,()=>{});
    });
    upstream.setTimeout(900000,()=>upstream.destroy());
    upstream.on('error',()=>{if(!res.headersSent)res.writeHead(502);res.end('Backend is reconnecting.');});
    req.on('aborted',()=>upstream.destroy());pipeline(req,upstream,()=>{});return;
  }
  if (!['GET','HEAD'].includes(req.method)) {res.writeHead(405);res.end();return;}
  let pathname;
  try {pathname = decodeURIComponent(new URL(req.url,'http://localhost').pathname);} catch {res.writeHead(400);res.end();return;}
  if (pathname.includes('\0') || pathname.includes('\\') || pathname.split('/').some(x=>x==='..'||x.startsWith('.'))) {res.writeHead(403);res.end();return;}
  let file = path.resolve(root,'.'+pathname);
  if (!file.startsWith(root+path.sep) && file !== root) {res.writeHead(403);res.end();return;}
  if (!fs.existsSync(file) || !fs.statSync(file).isFile()) file=path.join(root,'index.html');
  if (!fs.existsSync(file)) {res.writeHead(503);res.end('Run Install.cmd to build Solevia.');return;}
  res.writeHead(200,{'Content-Type':mime[path.extname(file)]||'application/octet-stream','Cache-Control':path.extname(file)==='.html'?'no-store':'public, max-age=3600','X-Content-Type-Options':'nosniff'});
  if(req.method==='HEAD')res.end();else pipeline(fs.createReadStream(file),res,()=>{});
});
server.listen(port,'127.0.0.1');
for (const signal of ['SIGINT','SIGTERM']) process.on(signal,()=>server.close(()=>process.exit(0)));
