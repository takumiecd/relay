import test from 'node:test';
import assert from 'node:assert/strict';
import {InputPump, KeyboardController} from '../../relay/web/keyboard.mjs';
const flush = () => new Promise(resolve => setImmediate(resolve));
function fixture() {
  const packets = [], gates = [];
  let keyboard;
  const pump = new InputPump(release => {
    packets.push({release, keys: [...keyboard.held]});
    return new Promise(resolve => gates.push(resolve));
  });
  keyboard = new KeyboardController({context:()=>({mode:'HUMAN',owned:true}),
    stop(){},mode(){},claim(){},hint(){},key(){},
    change:release=>pump.request(!!release,true)});
  const event = code => ({code,target:{tagName:'BODY'},preventDefault(){}});
  return {packets,gates,pump,keyboard,down:code=>keyboard.down(event(code)),up:code=>keyboard.up(event(code))};
}
test('Space plus motion sends on the key edge without a heartbeat', async()=>{
  const f=fixture();f.down('Space');f.down('KeyF');await flush();
  assert.deepEqual(f.packets[0],{release:false,keys:['Space','KeyF']});
  f.gates.shift()();await flush();f.gates.shift()?.();await flush();
});
test('a changed intent flushes immediately on reply and uses the latest keys',async()=>{
  const f=fixture();f.down('Space');await flush();f.down('KeyF');f.up('KeyF');f.down('KeyD');
  assert.equal(f.packets.length,1);f.gates.shift()();await flush();
  assert.deepEqual(f.packets[1],{release:false,keys:['Space','KeyD']});f.gates.shift()();await flush();
});
test('release bypasses an in-flight hold and a subsequent press survives',async()=>{
  const f=fixture();f.down('Space');await flush();f.up('Space');await flush();
  assert.equal(f.packets[1].release,true);f.down('Space');f.down('KeyF');
  f.gates.shift()();f.gates.shift()();await flush();
  assert.deepEqual(f.packets[2],{release:false,keys:['Space','KeyF']});f.gates.shift()();await flush();
});
test('busy heartbeats do not queue past motion or retry a failed packet',async()=>{
  let calls=0, reject;const pump=new InputPump(()=>{calls++;return new Promise((_,r)=>reject=r);});
  pump.request();await flush();for(let i=0;i<10;i++)pump.request();reject(new Error('offline'));await flush();
  assert.equal(calls,1);assert.equal(pump.inFlight,0);
});
test('releasing a movement key while Space is held immediately updates intent',async()=>{
  const f=fixture();f.down('Space');f.down('KeyF');await flush();f.gates.shift()();await flush();
  f.gates.shift()?.();await flush();f.up('KeyF');await flush();
  assert.deepEqual(f.packets.at(-1),{release:false,keys:['Space']});f.gates.shift()();await flush();
});
