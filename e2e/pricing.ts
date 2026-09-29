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
