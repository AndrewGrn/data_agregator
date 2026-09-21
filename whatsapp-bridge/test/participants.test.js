'use strict';

const test = require('node:test');
const assert = require('node:assert');
const { serializeParticipants } = require('../src/serialize');

function fakeChat(participants) {
  return { participants };
}

test('reshapes participants into plain id/is_admin pairs', () => {
  const result = serializeParticipants(fakeChat([
    { id: { _serialized: '380671234567@c.us' }, isAdmin: true, isSuperAdmin: false },
    { id: { _serialized: '380679999999@c.us' }, isAdmin: false, isSuperAdmin: false },
  ]));

  assert.deepStrictEqual(result, {
    participants: [
      { id: '380671234567@c.us', is_admin: true },
      { id: '380679999999@c.us', is_admin: false },
    ],
  });
});

test('super admins count as admins', () => {
  const result = serializeParticipants(fakeChat([
    { id: { _serialized: 'a@c.us' }, isAdmin: false, isSuperAdmin: true },
  ]));

  assert.strictEqual(result.participants[0].is_admin, true);
});

test('a chat with no participants yields an empty list', () => {
  assert.deepStrictEqual(serializeParticipants({}), { participants: [] });
});

test('a missing serialized id becomes null rather than throwing', () => {
  const result = serializeParticipants(fakeChat([{ id: null, isAdmin: false }]));

  assert.strictEqual(result.participants[0].id, null);
});
