The discount calculation produces incorrect totals for valid percentages, and the average calculation returns NaN for empty carts. Both behaviors were reproduced with the added formulas.

Full review comments:

- ZEBRA [P1] Convert the percentage to a fraction before applying it — src/pricing.ts:15-15
  For a 1000-cent subtotal and a 10% discount, this returns 0 instead of 900: multiplying by `percent` directly subtracts ten times the subtotal. Every discount of at least 1% makes a positive subtotal free after clamping. Divide `percent` by 100 before multiplying.

- ZEBRA [P2] Handle zero units before calculating the average — src/pricing.ts:22-22
  For an empty cart or a cart containing only zero-quantity items, `units` is zero and this calculation returns `NaN`. Guard the zero-unit case and return a defined result or raise an explicit error rather than exposing an invalid numeric price.

- ZEBRA [P3] Add missing parameter and return JSDoc entries — src/pricing.ts:11-12
  `applyDiscountCents` already has a JSDoc summary, but lacks `@param` and `@returns` entries. Add these to document the item inputs, percentage scale, and returned monetary units explicitly for callers.