import test from 'node:test';
import assert from 'node:assert/strict';
import {graphLayout, stepAtX, routeSegments, visiblePoint} from '../../capx/extensions/hybrid/graph.mjs';
const branches = [
  {id:'0001',parent:null,parent_step:null,head:113},
  {id:'0002',parent:'0001',parent_step:80,head:92},
  {id:'0003',parent:'0002',parent_step:85,head:99},
  {id:'0004',parent:'0003',parent_step:70,head:75},
];
const history = {branches,branch:'0003',cursor:87,adopted:{branch:'0002',end_step:90}};
test('nested adoption follows the shared prefix and only its own repaired future',()=>{
  assert.deepEqual(routeSegments(branches,'0003',99),[
    {branch:'0001',start:0,end:80},{branch:'0002',start:80,end:85},{branch:'0003',start:85,end:99}]);
  assert.deepEqual(routeSegments(branches,'0004',75),[{branch:'0001',start:0,end:70},{branch:'0004',start:70,end:75}]);
});
test('HEAD on inherited history belongs to the ancestor lane',()=>{
  assert.deepEqual(visiblePoint(branches,'0003',78),{branch:'0001',step:78});
  assert.deepEqual(visiblePoint(branches,'0003',83),{branch:'0002',step:83});
  assert.deepEqual(visiblePoint(branches,'0003',87),{branch:'0003',step:87});
});
test('click coordinates recover exact steps and clamp outside the recorded branch',()=>{
  const layout=graphLayout(history,740);
  assert.equal(stepAtX(layout,'0002',layout.x(87)),87);
  assert.equal(stepAtX(layout,'0002',-999),80);
  assert.equal(stepAtX(layout,'0002',99999),92);
  assert.equal(stepAtX(layout,'missing',200),null);
  assert.ok(layout.rows.every(row=>row.nodes.every(node=>node.step>=row.start)));
  assert.ok(layout.rows[0].nodes.some(node=>node.step===70));
});
test('long paths bound dot count while preserving forks, HEAD and adopted endpoint',()=>{
  const layout=graphLayout({branches:[{id:'a',parent:null,parent_step:null,head:10000},
    {id:'b',parent:'a',parent_step:6789,head:8000}],branch:'a',cursor:4321,adopted:{branch:'b',end_step:7999}},740);
  assert.ok(layout.rows[0].nodes.length<60);
  assert.ok(layout.rows[0].nodes.some(n=>n.step===6789));
  assert.ok(layout.rows[0].nodes.some(n=>n.step===4321));
  assert.ok(layout.rows[1].nodes.some(n=>n.step===7999));
  assert.equal(stepAtX(layout,'a',layout.x(7643)),7643);
});
