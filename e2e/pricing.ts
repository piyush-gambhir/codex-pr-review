export interface LineItem {
  sku: string;
  unitPriceCents: number;
  quantity: number;
}

export function subtotalCents(items: LineItem[]): number {
  return items.reduce((sum, item) => sum + item.unitPriceCents * item.quantity, 0);
}
