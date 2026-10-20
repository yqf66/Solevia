// Explicit live-browser acceptance: no model calls, no user plan mutations.
import assert from 'node:assert/strict';
import http from 'node:http';
import net from 'node:net';
import { existsSync, mkdirSync, writeFileSync } from 'node:fs';
import { once } from 'node:events';
import { readRenderedPage, closeBrowserReader } from '../src/page-reader.mjs';
import { readCrawl4AI } from '../src/crawl4ai-reader.mjs';
import { publicAddress } from '../src/network-policy.mjs';
import '../src/server.mjs'; // Same local service configuration; never print it.

const started=Date.now();
const report={checks:[],publicReads:[]};
const sockets=new Set();
const listen=async server=>{server.on('connection',socket=>{sockets.add(socket);socket.on('close',()=>sockets.delete(socket));});server.listen(0,'127.0.0.1');await once(server,'listening');return server.address().port;};
let privateHits=0;
const privateServer=http.createServer((_req,res)=>{privateHits++;res.end('private');});
privateServer.on('upgrade',(_req,socket)=>{privateHits++;socket.destroy();});
const privatePort=await listen(privateServer);
const requested=[];
const article='English practice: read a passage, record unfamiliar vocabulary, and review incorrect answers. '.repeat(8);
const origin=http.createServer((req,res)=>{
  requested.push(req.url);
  if(req.url==='/redirect'){res.writeHead(302,{location:`http://127.0.0.1:${privatePort}/redirect-leak`});res.end();return;}
  res.setHeader('content-type','text/html');
  res.end(`<html><title>Sandbox article</title><body><article>${article}</article><script>
    fetch('http://127.0.0.1:${privatePort}/fetch-leak').catch(()=>{});
    fetch('http://[::ffff:127.0.0.1]:${privatePort}/mapped-leak').catch(()=>{});
    fetch('/redirect').catch(()=>{});
    try {new WebSocket('ws://127.0.0.1:${privatePort}/socket-leak');}catch{}
    navigator.serviceWorker?.register('/sw.js').catch(()=>{});
    window.open('http://127.0.0.1:${privatePort}/popup-leak');
  </script></body></html>`);
});
const originPort=await listen(origin);
const upstream=http.createServer();
upstream.on('connect',(req,client,head)=>{
  const target=new URL('http://'+req.url);
  assert.equal(publicAddress(target.hostname.replace(/^\[|\]$/g,'')),true,'pinned public IP required');
  const remote=net.connect(originPort,'127.0.0.1',()=>{
    client.write('HTTP/1.1 200 Connection Established\r\n\r\n');if(head.length)remote.write(head);client.pipe(remote);remote.pipe(client);
  });
  sockets.add(remote);remote.on('error',()=>client.destroy());client.on('error',()=>remote.destroy());
  client.on('close',()=>remote.destroy());remote.on('close',()=>{sockets.delete(remote);client.destroy();});
});
const upstreamPort=await listen(upstream);
const executablePath=[process.env.PLAYWRIGHT_EXECUTABLE_PATH,
  `${process.env.PROGRAMFILES}\Google\Chrome\Application\chrome.exe`,
  `${process.env['PROGRAMFILES(X86)']}\Microsoft\Edge\Application\msedge.exe`].find(path=>path&&existsSync(path));
try {
  for(const reader of ['legacy','crawl4ai']) {
    const before=Date.now();const offset=requested.length;
    const request={url:'http://example.com/sandbox-article',maxChars:12000,proxyServer:`http://127.0.0.1:${upstreamPort}`};
    const result=reader==='legacy'?await readRenderedPage({...request,executablePath}):await readCrawl4AI(request);
    assert.match(result.content,/English practice/);
    assert.equal(privateHits,0,'private server received a browser request');
    assert.ok(requested.slice(offset).includes('/redirect'),'redirect attack was exercised');
    assert.ok(!requested.slice(offset).includes('/sw.js'),'service worker registration was blocked');
    report.checks.push({reader,passed:true,elapsedMs:Date.now()-before,privateHits,requests:requested.slice(offset),chars:result.content.length});
    await closeBrowserReader();
  }
} finally {
  await closeBrowserReader();for(const socket of sockets)socket.destroy();
  for(const server of [upstream,origin,privateServer])await new Promise(resolve=>server.close(resolve));
}

const proxyServer=(process.env.WEB_RESEARCH_USE_PROXY==='true'||process.env.USE_PROXY==='true')
  ?(process.env.WEB_RESEARCH_PROXY_URL||process.env.PROXY_URL||''):'';
try {
  for(const reader of ['legacy','crawl4ai']) {
    const before=Date.now();
    const request={url:'https://docs.python.org/3/library/asyncio-task.html#asyncio.TaskGroup',maxChars:12000,focus:'TaskGroup',proxyServer};
    const result=reader==='legacy'?await readRenderedPage({...request,executablePath}):await readCrawl4AI(request);
    assert.match(result.content,/TaskGroup/);
    report.publicReads.push({reader,url:request.url,passed:true,elapsedMs:Date.now()-before,chars:result.content.length,
      retrievalMethod:result.retrievalMethod,evidenceArchive:result.evidenceArchive||null});
  }
} finally {await closeBrowserReader();}
report.elapsedMs=Date.now()-started;
const folder=new URL('../../../.dev-logs/sandbox-acceptance/',import.meta.url);
mkdirSync(folder,{recursive:true});
writeFileSync(new URL('browser-report.json',folder),JSON.stringify(report,null,2));
console.log(JSON.stringify(report,null,2));
