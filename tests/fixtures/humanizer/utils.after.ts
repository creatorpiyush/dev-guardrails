/**
 * Sends each item before returning them, so callers can chain on the result.
 */
export function getItems(items: string[]) {
  for (const item of items) {
    send(item);
  }
  return items;
}
