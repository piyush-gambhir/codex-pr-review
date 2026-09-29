The discount calculation uses the wrong percentage scale, and the average calculation returns NaN for empty carts.

Full review comments:

- [P1] Convert the percentage to a fraction before applying it — /home/runner/work/repo/repo/src/pricing.ts:15-15
  For a 1,000-cent subtotal and `percent = 10`, this returns 0 instead of 900. Because the documented input range is 0–100, divide `percent` by 100 before multiplying; otherwise every discount of 1% or more makes a positive subtotal free.

- [P2] Handle zero units before calculating the average — /home/runner/work/repo/repo/src/pricing.ts:22-22
  For an empty cart, or a cart containing only zero-quantity items, both the subtotal and unit count are zero, so this returns `NaN`. Handle `units === 0` explicitly with a defined fallback or error rather than allowing an invalid price to propagate.