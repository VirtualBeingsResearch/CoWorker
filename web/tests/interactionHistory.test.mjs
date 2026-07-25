import assert from 'node:assert/strict';
import test from 'node:test';

import { loadInteractionHistoryPage } from '../src/interactionHistory.ts';

test('filtered history skips empty scan windows until a match is found', async () => {
  const requestedCursors = [];
  const pages = new Map([
    [null, { events: [], next_cursor: 'older-1', has_more: true }],
    ['older-1', { events: [], next_cursor: 'older-2', has_more: true }],
    ['older-2', { events: [{ seq: 7 }], next_cursor: 'older-3', has_more: true }],
  ]);

  const page = await loadInteractionHistoryPage({
    cursor: null,
    filtersActive: true,
    fetchPage: async cursor => {
      requestedCursors.push(cursor);
      return pages.get(cursor);
    },
  });

  assert.deepEqual(requestedCursors, [null, 'older-1', 'older-2']);
  assert.deepEqual(page.events, [{ seq: 7 }]);
  assert.equal(page.next_cursor, 'older-3');
});

test('unfiltered history loads only one page', async () => {
  let requests = 0;

  const page = await loadInteractionHistoryPage({
    cursor: null,
    filtersActive: false,
    fetchPage: async () => {
      requests += 1;
      return { events: [], next_cursor: 'older', has_more: true };
    },
  });

  assert.equal(requests, 1);
  assert.equal(page.next_cursor, 'older');
});

test('filtered history reaches the beginning when no record matches', async () => {
  const requestedCursors = [];

  const page = await loadInteractionHistoryPage({
    cursor: null,
    filtersActive: true,
    fetchPage: async cursor => {
      requestedCursors.push(cursor);
      return cursor === null
        ? { events: [], next_cursor: 'oldest', has_more: true }
        : { events: [], next_cursor: null, has_more: false };
    },
  });

  assert.deepEqual(requestedCursors, [null, 'oldest']);
  assert.deepEqual(page.events, []);
  assert.equal(page.has_more, false);
});

test('filtered history stops safely when a cursor repeats', async () => {
  let requests = 0;

  const page = await loadInteractionHistoryPage({
    cursor: 'current',
    filtersActive: true,
    fetchPage: async () => {
      requests += 1;
      return { events: [], next_cursor: 'current', has_more: true };
    },
  });

  assert.equal(requests, 1);
  assert.equal(page.next_cursor, null);
  assert.equal(page.has_more, false);
});
