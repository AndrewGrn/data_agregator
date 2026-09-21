const test = require('node:test');
const assert = require('node:assert');
const { serializeMessage } = require('../src/serialize');

function fakeMessage(overrides = {}) {
  return Object.assign({
    id: { _serialized: 'true_120363012345678901@g.us_3EB0C767D097B4C7F2A1' },
    from: '380671234567@c.us',
    author: '380671234567@c.us',
    body: 'привет всем',
    type: 'chat',
    timestamp: 1758400000,
    hasMedia: false,
    hasQuotedMsg: false,
  }, overrides);
}

test('produces every field of contract v1', () => {
  const event = serializeMessage(fakeMessage(), {
    accountId: 12,
    chatId: '120363012345678901@g.us',
    authorName: 'Andrii',
    media: null,
  });

  assert.strictEqual(event.v, 1);
  assert.strictEqual(event.account_id, 12);
  assert.strictEqual(event.chat_id, '120363012345678901@g.us');
  assert.strictEqual(event.message_id, 'true_120363012345678901@g.us_3EB0C767D097B4C7F2A1');
  assert.strictEqual(event.timestamp, 1758400000);
  assert.strictEqual(event.author, '380671234567@c.us');
  assert.strictEqual(event.author_name, 'Andrii');
  assert.strictEqual(event.body, 'привет всем');
  assert.strictEqual(event.has_quoted, false);
  assert.strictEqual(event.media, null);
  assert.ok(event.raw);
});

test('carries media metadata but never the bytes', () => {
  const event = serializeMessage(fakeMessage({ hasMedia: true }), {
    accountId: 12,
    chatId: 'c@g.us',
    authorName: null,
    media: { sha256: 'ab', mime: 'image/jpeg', size: 2048, filename: 'p.jpg' },
  });

  assert.strictEqual(event.media.sha256, 'ab');
  assert.strictEqual(event.media.size, 2048);
  assert.strictEqual(JSON.stringify(event).includes('base64'), false);
});

test('marks a quoted message as a reply', () => {
  const event = serializeMessage(
    fakeMessage({ hasQuotedMsg: true }),
    { accountId: 1, chatId: 'c@g.us', authorName: null, media: null, quotedMessageId: 'PREV' },
  );

  assert.strictEqual(event.has_quoted, true);
  assert.strictEqual(event.quoted_message_id, 'PREV');
});
