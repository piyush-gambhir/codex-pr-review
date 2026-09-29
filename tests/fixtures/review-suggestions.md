The patch introduces three pricing bugs and should not be considered correct.

Full review comments:

- [P1] Convert the percentage to a fraction — e2e/pricing.ts:16-16
  For a 1,000-cent subtotal and a 10% discount, this calculates a negative total that is clamped to zero instead of returning 900 cents. Divide the percentage by 100 before applying it.
  ```suggestion
    const discounted = subtotal - subtotal * (percent / 100);
  ```

- [P2] Handle zero units before calculating the average — e2e/pricing.ts:22-22
  An empty array or a cart containing only zero-quantity items produces `0 / 0`, returning `NaN` rather than a usable price. Handle `units === 0` explicitly with a defined fallback or error.

- [P2] Sort prices ascending to return the cheapest item — e2e/pricing.ts:26-26
  For items with different unit prices, the descending comparator puts the most expensive item first, so this function returns the opposite of its intended result.
  ```suggestion
    return [...items].sort((a, b) => a.unitPriceCents - b.unitPriceCents)[0];
  ```
