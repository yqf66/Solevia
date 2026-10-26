import { lookup } from 'node:dns/promises';
import { BlockList, isIP } from 'node:net';

const denied = new BlockList();
for (const [address, prefix] of [['0.0.0.0',8],['10.0.0.0',8],['100.64.0.0',10],
  ['127.0.0.0',8],['169.254.0.0',16],['172.16.0.0',12],['192.0.0.0',24],
  ['192.0.2.0',24],['192.88.99.0',24],['192.168.0.0',16],['198.18.0.0',15],
  ['198.51.100.0',24],['203.0.113.0',24],['224.0.0.0',3]]) denied.addSubnet(address,prefix,'ipv4');
const globalV6 = new BlockList();
globalV6.addSubnet('2000::',3,'ipv6');
denied.addSubnet('2001::',23,'ipv6');
denied.addSubnet('2001:db8::',32,'ipv6');
denied.addSubnet('2002::',16,'ipv6');

export function publicAddress(address) {
  const family = isIP(address);
  if (family === 4) return !denied.check(address,'ipv4');
  // Also denies mapped IPv4, unspecified/link-local, multicast and scoped IPs.
  return family === 6 && !address.includes('%') && globalV6.check(address,'ipv6') && !denied.check(address,'ipv6');
}

export function publicHttpUrl(value) {
  try {
    const url = new URL(String(value));
    const host = url.hostname.replace(/^\[|\]$/g,'').toLowerCase().replace(/\.$/,'');
    if (!['http:','https:'].includes(url.protocol) || url.username || url.password ||
      host === 'localhost' || host.endsWith('.localhost') || host.endsWith('.local') ||
      (isIP(host) && !publicAddress(host))) return null;
    return url.toString();
  } catch { return null; }
}

export async function resolvePublicTarget(value, resolver = lookup) {
  const validated = publicHttpUrl(value);
  if (!validated) throw new Error('PUBLIC_NETWORK_TARGET_REQUIRED');
  const url = new URL(validated);
  const host = url.hostname.replace(/^\[|\]$/g,'');
  let timer;
  try {
    const addresses = isIP(host) ? [{address:host,family:isIP(host)}] : await Promise.race([
      resolver(host,{all:true,verbatim:true}),
      new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('DNS_TIMEOUT')),5000);}),
    ]);
    if (!addresses.length || addresses.some(({address})=>!publicAddress(address))) throw new Error('PRIVATE_NETWORK_DENIED');
    return { url, addresses };
  } finally { clearTimeout(timer); }
}
