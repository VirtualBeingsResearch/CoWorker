export type BubbleRecordIdentity = {
  id?: unknown;
  log_id?: unknown;
};

export function isTargetBubbleRecord(
  bubble: BubbleRecordIdentity,
  targetBubbleId: string,
): boolean {
  return Boolean(targetBubbleId)
    && String(bubble.log_id || bubble.id || '') === targetBubbleId;
}

export type InteractionFilterState = {
  contextSeq: number | null;
  type: string;
  query: string;
  seqStart: string;
  seqEnd: string;
  timeStart: string;
  timeEnd: string;
};

export function shouldShowInteractionContextAction(filters: InteractionFilterState): boolean {
  return filters.contextSeq == null
    && Boolean(filters.type || filters.query || filters.seqStart || filters.seqEnd || filters.timeStart || filters.timeEnd);
}
