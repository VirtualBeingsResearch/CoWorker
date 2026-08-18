import assert from 'node:assert/strict';
import test from 'node:test';

import {
  isTargetBubbleRecord,
  shouldShowInteractionContextAction,
} from '../src/admin/interactionNavigation.ts';

test('only the linked Bubble record is targeted for automatic expansion', () => {
  const target = 'bbl_260818120000_audit';

  assert.equal(isTargetBubbleRecord({ id: 'bbl_other' }, target), false);
  assert.equal(
    isTargetBubbleRecord({ id: 'bbl_260818120000', log_id: target }, target),
    true,
  );
  assert.equal(isTargetBubbleRecord({ id: target }, target), true);
  assert.equal(isTargetBubbleRecord({ id: target }, ''), false);
});

test('context navigation is separate and only offered for filtered results', () => {
  const defaults = {
    contextSeq: null,
    type: '',
    query: '',
    seqStart: '',
    seqEnd: '',
    timeStart: '',
    timeEnd: '',
  };

  assert.equal(shouldShowInteractionContextAction(defaults), false);
  assert.equal(shouldShowInteractionContextAction({ ...defaults, query: 'bubble' }), true);
  assert.equal(shouldShowInteractionContextAction({ ...defaults, timeStart: '2026-08-18T00:00:00Z' }), true);
  assert.equal(shouldShowInteractionContextAction({ ...defaults, query: 'bubble', contextSeq: 42 }), false);
});
