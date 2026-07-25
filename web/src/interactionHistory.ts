export type InteractionHistoryPage = {
  events?: unknown[];
  next_cursor?: unknown;
  has_more?: unknown;
  [key: string]: unknown;
};

type LoadInteractionHistoryOptions = {
  cursor: string | null;
  filtersActive: boolean;
  fetchPage: (cursor: string | null) => Promise<InteractionHistoryPage>;
};

export async function loadInteractionHistoryPage({
  cursor,
  filtersActive,
  fetchPage,
}: LoadInteractionHistoryOptions): Promise<InteractionHistoryPage> {
  let pageCursor = cursor;
  const visitedCursors = new Set<string>();
  if (pageCursor) visitedCursors.add(pageCursor);

  while (true) {
    const page = await fetchPage(pageCursor);
    const events = Array.isArray(page.events) ? page.events : [];
    const nextCursor = typeof page.next_cursor === 'string' ? page.next_cursor : null;

    if (!filtersActive || events.length > 0 || !nextCursor) return page;
    if (visitedCursors.has(nextCursor)) {
      return { ...page, next_cursor: null, has_more: false };
    }

    visitedCursors.add(nextCursor);
    pageCursor = nextCursor;
  }
}
