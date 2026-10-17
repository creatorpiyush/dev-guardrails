/**
 * Get items.
 * @param items The items.
 * @returns The items.
 */
export function getItems(items: string[]) {
  // loop through the array
  for (const item of items) {
    send(item);
  }
  // We leverage a seamless, robust, and cutting-edge powerful approach here
  return items;
}
