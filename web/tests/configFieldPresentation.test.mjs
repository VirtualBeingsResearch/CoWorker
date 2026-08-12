import assert from 'node:assert/strict';
import test from 'node:test';

import { configFieldPresentation } from '../src/admin/settings/configFieldPresentation.ts';

test('uses the same bounded number metadata for every configuration surface', () => {
  assert.deepEqual(configFieldPresentation('api.port'), {
    editor: 'default',
    minimum: 1,
    maximum: 65_535,
    step: 1,
  });
  assert.deepEqual(configFieldPresentation('memory.compress_ratio'), {
    editor: 'default',
    minimum: 0,
    maximum: 1,
    step: 0.01,
  });
});

test('describes active and passive idle timing without duplicating renderer logic', () => {
  assert.match(
    configFieldPresentation('agent.idle_sleep_seconds').hint,
    /主动模式/,
  );
  assert.match(
    configFieldPresentation('agent.idle_sleep_seconds', { passiveMode: true }).hint,
    /Passive 模式/,
  );
});

test('selects structured editors for list-like settings', () => {
  assert.equal(configFieldPresentation('llm.fallbacks').editor, 'fallback-list');
  assert.equal(configFieldPresentation('api.cors_origins').editor, 'cors-list');
  assert.equal(
    configFieldPresentation('agent.bubble_handoff_transparency_participant_matches').editor,
    'participant-list',
  );
  assert.equal(
    configFieldPresentation('agent.bubble_handoff_transparency_stream_transports').editor,
    'transport-list',
  );
});
