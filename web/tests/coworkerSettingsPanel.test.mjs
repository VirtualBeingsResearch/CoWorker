import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import {
  COWORKER_PEER_ID_PATTERN,
  COWORKER_RESERVED_PEER_IDS,
} from '../src/admin/settings/coworkerPeerId.ts';

const panel = await readFile(
  new URL('../src/admin/settings/panels/CoworkerSettingsPanel.tsx', import.meta.url),
  'utf8',
);

test('accepts coworker self_id shaped peer ids and reserves control', () => {
  assert.match('ava', COWORKER_PEER_ID_PATTERN);
  assert.match('cw_ab12cd34', COWORKER_PEER_ID_PATTERN);
  assert.doesNotMatch('Not Valid', COWORKER_PEER_ID_PATTERN);
  assert.equal(COWORKER_RESERVED_PEER_IDS.has('control'), true);
  assert.match(panel, /COWORKER_RESERVED_PEER_IDS/);
  assert.match(panel, /coworker:control/);
});

test('adds explicit peers by remote self_id instead of generating one', () => {
  assert.match(panel, /对端 self_id/);
  assert.match(panel, /coworker:control/);
  assert.doesNotMatch(panel, /generateTelegramInstanceId/);
  assert.match(panel, /coworker\.peers\.\$\{peerId\}\.token/);
  assert.match(panel, /coworker\.inbound_token/);
});
