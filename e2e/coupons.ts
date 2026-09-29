// Sample code used to test incremental reviews: this file is added in a later
// commit, so only it shows up in the incremental diff.
export interface Coupon {
  code: string;
  percentOff: number;
  expiresAt: Date;
}

/** True when the coupon can still be used today. */
export function isUsable(coupon: Coupon, now: Date): boolean {
  return coupon.expiresAt < now;
}
