import { test } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { spawn } from 'node:child_process';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

test('proxy rejects rebinding, foreign origins and duplicate Host before upstream', async () => {
  let forwarded=0;
  const upstream=http.createServer((req,res)=>{forwarded++;res.end('{}');});
  await new Promise(resolve=>upstream.listen(0,'127.0.0.1',resolve));
  const probe=http.createServer();await new Promise(resolve=>probe.listen(0,'127.0.0.1',resolve));
  const port=probe.address().port;await new Promise(resolve=>probe.close(resolve));
  const folder=await mkdtemp(path.join(tmpdir(),'solevia-proxy-'));
  await writeFile(path.join(folder,'index.html'),'<html>Solevia</html>');
  const script=fileURLToPath(new URL('../scripts/frontend-server.mjs',import.meta.url));
  const child=spawn(process.execPath,[script,folder,String(port),String(upstream.address().port)],{stdio:'pipe'});
  const request=(headers={},route='/api/settings/model')=>new Promise((resolve,reject)=>{
    const req=http.request({host:'127.0.0.1',port,path:route,headers},res=>{res.resume();res.on('end',()=>resolve(res.statusCode));});
    req.on('error',reject);req.end();
  });
  try {
    for(let i=0;i<100;i++){try{if(await request({},'/_solevia/health')===200)break;}catch{}await new Promise(r=>setTimeout(r,30));}
    assert.equal(await request({Host:`attacker.example:${port}`}),400);
    assert.equal(await request({Origin:'https://attacker.example'}),403);
    assert.equal(await request({Origin:'null'}),403);
    assert.equal(await request({'Sec-Fetch-Site':'cross-site'}),403);
    assert.equal(await request(['Host',`127.0.0.1:${port}`,'Host',`attacker.example:${port}`]),400);
    assert.equal(forwarded,0);
    assert.equal(await request({Origin:`http://127.0.0.1:${port}`} ),200);
    assert.equal(forwarded,1);
  } finally {
    child.kill();await new Promise(resolve=>child.once('exit',resolve));
    await new Promise(resolve=>upstream.close(resolve));await rm(folder,{recursive:true,force:true});
  }
});
