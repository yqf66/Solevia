// This is the network enforcement point, including browser redirects, workers
// and WebSockets. Resolve once, validate every answer, connect to that exact IP.
import http from 'node:http';
import https from 'node:https';
import net from 'node:net';
import tls from 'node:tls';
import { once } from 'node:events';
import { resolvePublicTarget } from './network-policy.mjs';

const authority = (address,port)=>`${address.includes(':') ? `[${address}]` : address}:${port}`;

async function tunnel(address, port, proxyServer) {
  if (!proxyServer) {
    const socket=net.connect({host:address,port});
    socket.setTimeout(20000,()=>socket.destroy(new Error('EGRESS_TIMEOUT')));
    await once(socket,'connect');
    return socket;
  }
  const proxy=new URL(proxyServer);
  if (!['http:','https:'].includes(proxy.protocol)) throw new Error('UNSUPPORTED_EGRESS_PROXY');
  const options={host:proxy.hostname,port:Number(proxy.port)||(proxy.protocol==='https:'?443:80)};
  const socket=proxy.protocol==='https:'?tls.connect({...options,servername:proxy.hostname}):net.connect(options);
  socket.setTimeout(20000,()=>socket.destroy(new Error('EGRESS_TIMEOUT')));
  await once(socket,proxy.protocol==='https:'?'secureConnect':'connect');
  return await new Promise((resolve,reject)=>{
    let header=Buffer.alloc(0);
    const fail=()=>reject(new Error('EGRESS_TUNNEL_FAILED'));
    socket.once('error',fail);
    socket.once('close',fail);
    const onData=chunk=>{
      header=Buffer.concat([header,chunk]);
      if (header.length>16384) {socket.destroy();return;}
      const end=header.indexOf('\r\n\r\n');
      if(end<0)return;
      socket.off('data',onData);socket.off('error',fail);socket.off('close',fail);
      if(!/^HTTP\/1\.[01] 200\b/.test(header.toString('latin1'))) {socket.destroy();reject(new Error('EGRESS_TUNNEL_FAILED'));return;}
      socket.pause();
      if(header.length>end+4)socket.unshift(header.subarray(end+4));
      resolve(socket);
    };
    socket.on('data',onData);
    const auth=proxy.username||proxy.password?'Proxy-Authorization: Basic '+Buffer.from(
      `${decodeURIComponent(proxy.username)}:${decodeURIComponent(proxy.password)}`).toString('base64')+'\r\n':'';
    const target=authority(address,port);
    socket.write(`CONNECT ${target} HTTP/1.1\r\nHost: ${target}\r\n${auth}\r\n`);
  });
}

export async function createEgressProxy({proxyServer='',resolver}={}) {
  const sockets=new Set();
  const track=socket=>{sockets.add(socket);socket.once('close',()=>sockets.delete(socket));
    socket.on('error',()=>{});socket.setTimeout(20000,()=>socket.destroy());return socket;};
  const server=http.createServer(async(req,res)=>{
    let outgoing;
    try {
      const {url,addresses}=await resolvePublicTarget(req.url,resolver);
      if(url.protocol!=='http:')throw new Error('HTTP_PROXY_SCHEME');
      const socket=track(await tunnel(addresses[0].address,Number(url.port)||80,proxyServer));
      if(res.destroyed){socket.destroy();return;}
      const agent=new http.Agent({keepAlive:false});
      agent.createConnection=()=>socket;
      const headers={...req.headers,host:url.host,connection:'close'};
      delete headers['proxy-authorization'];delete headers['proxy-connection'];delete headers.upgrade;
      outgoing=http.request({host:url.hostname,port:Number(url.port)||80,method:req.method,
        path:url.pathname+url.search,headers,agent},response=>{
        res.writeHead(response.statusCode,response.headers);
        let bytes=0;
        response.on('data',chunk=>{bytes+=chunk.length;if(bytes>32*1024*1024)outgoing.destroy();});
        response.on('error',()=>res.destroy());response.pipe(res);
      });
      outgoing.on('error',()=>{if(!res.headersSent)res.writeHead(502);res.end();});
      req.on('aborted',()=>outgoing.destroy());res.on('close',()=>outgoing.destroy());
      req.pipe(outgoing);socket.resume();
    } catch {if(!res.headersSent)res.writeHead(403);res.end('PUBLIC_NETWORK_TARGET_REQUIRED');}
  });
  server.on('connect',async(req,client,head)=>{
    let remote;
    try {
      const {url,addresses}=await resolvePublicTarget(`https://${req.url}`,resolver);
      // CONNECT has no URL path/userinfo. Restrict tunnels to web TLS ports.
      if(url.pathname!=='/' || url.search || url.hash || ![443,8443].includes(Number(url.port)||443))throw new Error('CONNECT_TARGET_DENIED');
      remote=track(await tunnel(addresses[0].address,Number(url.port)||443,proxyServer));
      if(client.destroyed){remote.destroy();return;}
      client.write('HTTP/1.1 200 Connection Established\r\n\r\n');
      if(head.length)remote.write(head);
      let bytes=0;
      remote.on('data',chunk=>{bytes+=chunk.length;if(bytes>64*1024*1024)remote.destroy();});
      remote.on('close',()=>client.destroy());client.on('close',()=>remote.destroy());
      client.pipe(remote);remote.pipe(client);remote.resume();
    } catch {remote?.destroy();client.end('HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n');}
  });
  server.on('connection',socket=>{
    if(sockets.size>=256){socket.destroy();return;}track(socket);
  });
  server.on('clientError',(_error,socket)=>socket.destroy());
  server.headersTimeout=10000;server.requestTimeout=20000;
  server.listen(0,'127.0.0.1');await once(server,'listening');
  return {url:`http://127.0.0.1:${server.address().port}`,
    async close(){for(const socket of sockets)socket.destroy();await new Promise(resolve=>server.close(resolve));}};
}

export async function resolvePublicRedirect(value, {proxyServer='',timeoutMs=5000}={}) {
  const sockets=new Set();
  let stopped=false;
  let timer;
  const follow=async()=>{
    let current=value;
    for(let hop=0;hop<=5;hop++) {
      const {url,addresses}=await resolvePublicTarget(current);
      if(stopped)throw new Error('REDIRECT_TIMEOUT');
      const socket=await tunnel(addresses[0].address,Number(url.port)||(url.protocol==='https:'?443:80),proxyServer);
      sockets.add(socket);
      if(stopped){socket.destroy();throw new Error('REDIRECT_TIMEOUT');}
      const secure=url.protocol==='https:';
      const agent=secure?new https.Agent():new http.Agent();
      agent.createConnection=()=>secure?tls.connect({socket,servername:url.hostname}):socket;
      const response=await new Promise((resolve,reject)=>{
        const request=(secure?https:http).request(url,{method:'HEAD',agent,headers:{'user-agent':'Mozilla/5.0'}},resolve);
        request.on('error',reject);request.end();socket.resume();
      });
      const location=response.statusCode>=300&&response.statusCode<400?response.headers.location:null;
      response.destroy();agent.destroy();socket.destroy();sockets.delete(socket);
      if(!location)return url.toString();
      current=new URL(location,url).toString();
    }
    throw new Error('REDIRECT_LIMIT');
  };
  try {
    return await Promise.race([follow(),new Promise((_,reject)=>{
      timer=setTimeout(()=>{stopped=true;for(const socket of sockets)socket.destroy();reject(new Error('REDIRECT_TIMEOUT'));},timeoutMs);
    })]);
  } finally {clearTimeout(timer);stopped=true;for(const socket of sockets)socket.destroy();}
}
