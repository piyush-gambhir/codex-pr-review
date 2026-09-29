// Sample code used to test the action end to end: test PRs add deliberate
// bugs here and request `@gpt review`.
export interface LineItem {
  sku: string;
  unitPriceCents: number;
  quantity: number;
}

export function subtotalCents(items: LineItem[]): number {
  return items.reduce((sum, item) => sum + item.unitPriceCents * item.quantity, 0);
}

/** Applies a percentage discount (0-100) and returns the discounted total in cents. */
export function applyDiscountCents(items: LineItem[], percent: number): number {
  const subtotal = subtotalCents(items);
  const discounted = subtotal - subtotal * percent;
  return Math.max(discounted, 0);
}

export function averageUnitPriceCents(items: LineItem[]): number {
  const units = items.reduce((n, item) => n + item.quantity, 0);
  return subtotalCents(items) / units;
}

export function cheapestItem(items: LineItem[]): LineItem {
  return [...items].sort((a, b) => b.unitPriceCents - a.unitPriceCents)[0];
}
