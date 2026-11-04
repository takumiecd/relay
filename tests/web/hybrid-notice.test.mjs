import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const source = readFileSync(new URL('../../relay/web/console.js', import.meta.url), 'utf8');
const body = source.slice(source.indexOf('function executionNotice('), source.indexOf('\nlet state ='));
const notice = new Function(body + '\nreturn executionNotice;')();

test('joint limits display the actionable cause, rather than a generic calculation failure', () => {
  const reason = '関節7が回転限界です。Spaceを離し、反対方向へ回してください。';
  const value = notice({manual_rejection: {message: '計算できませんでした', details: reason}});
  assert.equal(value.message, reason);
  assert.equal(value.details, '');
  assert.equal(value.error, false);
});

test('kinematics deadline and tracking refusal show the actual reason', () => {
  for (const reason of ['逆運動学の計算時間を超えました。', '手先の追従誤差が大きいため停止しました。']) {
    assert.equal(notice({manual_rejection: {message: 'generic', details: reason}}).message, reason);
  }
});

test('existing backend refusals retain the generic guidance and diagnostic detail', () => {
  assert.deepEqual(notice({manual_rejection: {message: 'try another direction', details: 'MoveIt IK failed'}}),
    {message: 'try another direction', details: 'MoveIt IK failed', error: false});
});
