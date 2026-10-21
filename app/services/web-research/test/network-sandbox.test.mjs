import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import net from 'node:net';
import { once } from 'node:events';
import { createEgressProxy } from '../src/egress-proxy.mjs';
import { publicAddress, publicHttpUrl, resolvePublicTarget } from '../src/network-policy.mjs';
import { workerEnvironment } from '../src/worker-environment.mjs';

const listen=async server=>{server.listen(0,'127.0.0.1');await once(server,'listening');return server.address().port;};
const close=server=>new Promise(resolve=>{server.closeAllConnections?.();server.close(resolve);});
const request=(proxy,url)=>new Promise((resolve,reject)=>{
  const req=http.get(proxy,{path:url},res=>{let body='';res.on('data',chunk=>body+=chunk);res.on('end',()=>resolve({status:res.statusCode,headers:res.headers,body}));});
  req.on('error',reject);
});

test('URL policy rejects alternate IPv4, mapped IPv6, credentials and special ranges',()=>{
  for(const value of ['http://2130706433/','http://0177.0.0.1/','http://0x7f000001/',
    'http://[::ffff:127.0.0.1]/','http://[::]/','http://[2002:7f00:1::]/',
    'http://[2001:db8::1]/','http://localhost./','http://x.local/',
    'http://' + 'user:password' + '@example.com/','http://169.254.169.254/latest/meta-data/']) {
    assert.equal(publicHttpUrl(value),null,value);
  }
  assert.equal(publicAddress('8.8.8.8'),true);
  assert.equal(publicAddress('2606:4700:4700::1111'),true);
});

test('mixed DNS answers and private aliases are denied; credentials never inherited',async()=>{
  await assert.rejects(resolvePublicTarget('https://public.example/',async()=>[
    {address:'8.8.8.8',family:4},{address:'127.0.0.1',family:4}]),/PRIVATE_NETWORK_DENIED/);
  const env=workerEnvironment({SystemRoot:'C:/Windows',PATH:'bin',OPENAI_API_KEY:'secret',
    DATABASE_URL:'private',NODE_OPTIONS:'--require attacker',PYTHONPATH:'attacker'});
  assert.deepEqual(env,{SystemRoot:'C:/Windows',PATH:'bin',PYTHON_DOTENV_DISABLED:'1'});
});

test('installed browser sandbox adapter resolves the shared environment policy',async()=>{
  const adapter=await import('../node_modules/open-websearch/build/utils/playwrightClient.js');
  assert.equal(typeof adapter.openPlaywrightBrowser,'function');
});

test('real proxy pins checked IP and blocks redirect, private DNS and CONNECT before connection',async()=>{
  const seen=[];
  const sockets=new Set();
  const origin=http.createServer((req,res)=>{
    seen.push(req.url);
    if(req.url==='/redirect'){res.writeHead(302,{location:'http://127.0.0.1:8000/private'});res.end();}
    else res.end('Public article content');
  });
  const originPort=await listen(origin);
  const tunnelTargets=[];
  const upstream=http.createServer();
  upstream.on('connection',socket=>{sockets.add(socket);socket.on('close',()=>sockets.delete(socket));});
  upstream.on('connect',(req,client,head)=>{
    tunnelTargets.push(req.url);
    const remote=net.connect(originPort,'127.0.0.1',()=>{
      client.write('HTTP/1.1 200 Connection Established\r\n\r\n');
      if(head.length)remote.write(head);client.pipe(remote);remote.pipe(client);
    });
    sockets.add(remote);remote.on('error',()=>client.destroy());client.on('error',()=>remote.destroy());
    client.on('close',()=>remote.destroy());remote.on('close',()=>{sockets.delete(remote);client.destroy();});
  });
  const upstreamPort=await listen(upstream);
  let lookups=0;
  const egress=await createEgressProxy({proxyServer:`http://127.0.0.1:${upstreamPort}`,
    resolver:async host=>{lookups++;return [{address:host==='private.example'?'127.0.0.1':'93.184.216.34',family:4}];}});
  try {
    assert.equal((await request(egress.url,'http://public.example/article')).body,'Public article content');
    assert.equal((await request(egress.url,'http://127.0.0.1:8000/private')).status,403);
    assert.equal((await request(egress.url,'http://private.example/private')).status,403);
    const redirected=await request(egress.url,'http://public.example/redirect');
    assert.equal(redirected.status,302);
    assert.equal((await request(egress.url,redirected.headers.location)).status,403);
    const connectStatus=await new Promise((resolve,reject)=>{
      const req=http.request(egress.url,{method:'CONNECT',path:'[::ffff:127.0.0.1]:443'});
      req.on('connect',(res,socket)=>{socket.destroy();resolve(res.statusCode);});req.on('error',reject);req.end();
    });
    assert.equal(connectStatus,403);
    assert.deepEqual(seen,['/article','/redirect']);
    assert.deepEqual(tunnelTargets,['93.184.216.34:80','93.184.216.34:80']);
    assert.equal(lookups,3,'one DNS lookup per actual candidate, with no second resolution at connect');
  } finally {await egress.close();for(const socket of sockets)socket.destroy();await close(upstream);await close(origin);}
});
